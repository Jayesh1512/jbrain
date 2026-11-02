//! Derived, bounded retrieval indexes. Canonical OKF bytes are never rewritten.
use crate::{
    error::{Error, Result},
    okf::{self, Concept},
    storage::Index,
};
use pulldown_cmark::{Event, Parser, Tag, TagEnd};
use rusqlite::{Connection, OptionalExtension, params, params_from_iter};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::BTreeSet, time::Instant};

pub fn schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS retrieval_docs(
        concept_id TEXT PRIMARY KEY REFERENCES concepts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,period TEXT NOT NULL,date TEXT NOT NULL,basis TEXT NOT NULL,
        status TEXT NOT NULL,stale_after TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS retrieval_scope ON retrieval_docs(period,kind,date,concept_id);
        CREATE INDEX IF NOT EXISTS retrieval_date ON retrieval_docs(date,concept_id);
        CREATE TABLE IF NOT EXISTS retrieval_entities(
        concept_id TEXT REFERENCES concepts(id) ON DELETE CASCADE,entity TEXT,name TEXT,role TEXT,
        PRIMARY KEY(concept_id,entity,role));
        CREATE INDEX IF NOT EXISTS retrieval_entity ON retrieval_entities(entity,concept_id);
        CREATE TABLE IF NOT EXISTS retrieval_aliases(
        alias TEXT,entity TEXT,name TEXT,source TEXT REFERENCES concepts(id) ON DELETE CASCADE,
        initial TEXT,chars INTEGER,PRIMARY KEY(alias,entity,source));
        CREATE INDEX IF NOT EXISTS retrieval_alias_source ON retrieval_aliases(source);
        CREATE INDEX IF NOT EXISTS retrieval_alias_fuzzy ON retrieval_aliases(initial,chars,alias);
        CREATE TABLE IF NOT EXISTS retrieval_facts(
        concept_id TEXT REFERENCES concepts(id) ON DELETE CASCADE,metric TEXT,value TEXT,
        basis TEXT,quote TEXT,pointer TEXT,PRIMARY KEY(concept_id,metric));
        CREATE INDEX IF NOT EXISTS retrieval_metric ON retrieval_facts(metric,concept_id);",
    )?;
    Ok(())
}

fn invalid(message: &str) -> Error {
    Error::new(400, "INVALID_QUERY", message)
}
fn cut(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}
fn words(value: &str) -> Vec<String> {
    value
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .map(str::to_lowercase)
        .collect()
}
fn norm(value: &str) -> String {
    words(value).join(" ")
}
fn field<'a>(c: &'a Concept, name: &str) -> &'a str {
    c.metadata.get(name).and_then(Value::as_str).unwrap_or("")
}
fn canonical_metric(value: &str) -> String {
    match norm(value).as_str() {
        "win" | "wins" | "won" | "victories" => "won".into(),
        "draw" | "draws" | "drawn" | "drew" | "stalemates" => "drawn".into(),
        "loss" | "losses" | "lost" | "defeats" => "lost".into(),
        "goals for" | "goals scored" => "goals_for".into(),
        "goals against" | "goals conceded" => "goals_against".into(),
        "goal difference" => "goal_difference".into(),
        other => other.replace(' ', "_"),
    }
}

fn infer_kind(c: &Concept) -> String {
    let explicit = field(c, "document_kind");
    if !explicit.is_empty() {
        return cut(explicit, 64);
    }
    if !field(c, "match_date").is_empty() {
        return "Match".into();
    }
    if c.metadata.contains_key("computed_statistics") && !field(c, "season").is_empty() {
        return "ClubSeasonSummary".into();
    }
    // Legacy structured importer adapter; custom producer types remain supported.
    if field(c, "dataset") == "epl-results-v1" {
        if !field(c, "season").is_empty() {
            return "SeasonSummary".into();
        }
        if c.id.contains("/clubs/") {
            return "Club".into();
        }
        if c.id.contains("/head-to-head/") {
            return "HeadToHead".into();
        }
        if c.id.ends_with("/overview") {
            return "Coverage".into();
        }
        if c.id.ends_with("/records") {
            return "Records".into();
        }
    }
    cut(&c.kind, 64)
}

fn add_entity(
    conn: &Connection,
    c: &Concept,
    key: &str,
    name: &str,
    role: &str,
    aliases: &[String],
) -> Result<()> {
    let key = cut(&norm(key), 160);
    let name = cut(name, 160);
    if key.is_empty() || name.is_empty() {
        return Ok(());
    }
    conn.execute(
        "INSERT OR IGNORE INTO retrieval_entities VALUES(?,?,?,?)",
        params![c.id, key, name, role],
    )?;
    for alias in std::iter::once(name.clone()).chain(aliases.iter().take(64).cloned()) {
        let alias = norm(&cut(&alias, 160));
        if alias.is_empty() {
            continue;
        }
        let initial = alias.chars().next().unwrap().to_string();
        conn.execute(
            "INSERT OR IGNORE INTO retrieval_aliases VALUES(?,?,?,?,?,?)",
            params![
                alias,
                key,
                name,
                c.id,
                initial,
                alias.chars().count() as i64
            ],
        )?;
    }
    Ok(())
}

fn add_fact(
    conn: &Connection,
    c: &Concept,
    metric: &str,
    value: &Value,
    basis: &str,
    quote: &str,
    pointer: &str,
) -> Result<()> {
    let metric = canonical_metric(metric);
    let text = value.to_string();
    if metric.is_empty() || metric.len() > 64 || text.len() > 1024 || value.is_null() {
        return Ok(());
    }
    conn.execute(
        "INSERT OR REPLACE INTO retrieval_facts VALUES(?,?,?,?,?,?)",
        params![c.id, metric, text, basis, cut(quote, 1600), pointer],
    )?;
    Ok(())
}

pub fn index_concept(conn: &Connection, c: &Concept) -> Result<()> {
    let kind = infer_kind(c);
    let period = cut(
        if field(c, "period").is_empty() {
            field(c, "season")
        } else {
            field(c, "period")
        },
        64,
    );
    let date = cut(
        if field(c, "date").is_empty() {
            field(c, "match_date")
        } else {
            field(c, "date")
        },
        32,
    );
    let mut basis = cut(field(c, "basis"), 64);
    if basis.is_empty() {
        basis = cut(field(c, "points_basis"), 64);
    }
    if basis.is_empty()
        && field(c, "dataset") == "epl-results-v1"
        && c.body.contains("BEFORE disciplinary deductions")
    {
        basis = "earned_before_adjustments".into();
    }
    let status = if field(c, "status").is_empty() {
        "stable"
    } else {
        field(c, "status")
    };
    conn.execute(
        "INSERT INTO retrieval_docs VALUES(?,?,?,?,?,?,?)",
        params![
            c.id,
            kind,
            period,
            date,
            basis,
            cut(status, 32),
            cut(field(c, "stale_after"), 64)
        ],
    )?;
    let declared = field(c, "entity_id");
    let entity_name = [
        field(c, "entity_name"),
        field(c, "entity"),
        field(c, "name"),
    ]
    .into_iter()
    .find(|s| !s.is_empty());
    if !declared.is_empty() || entity_name.is_some() {
        let name = entity_name.unwrap_or(&c.title);
        add_entity(
            conn,
            c,
            if declared.is_empty() { name } else { declared },
            name,
            "entity",
            &c.aliases,
        )?;
    } else if ["Person", "Company", "Entity", "Club", "Project"]
        .iter()
        .any(|t| kind.eq_ignore_ascii_case(t))
    {
        let name = c
            .title
            .strip_suffix(" Premier League club results history")
            .unwrap_or(&c.title);
        add_entity(conn, c, name, name, "entity", &c.aliases)?;
    }
    for key in ["club", "home_team", "away_team"] {
        let value = field(c, key);
        if !value.is_empty() {
            add_entity(conn, c, value, value, key, &[])?;
        }
    }
    if let Some(values) = c.metadata.get("entities").and_then(Value::as_array) {
        for value in values.iter().take(16).filter_map(Value::as_str) {
            add_entity(conn, c, value, value, "entity", &[])?;
        }
    }
    for key in ["computed_statistics", "facts"] {
        if let Some(facts) = c.metadata.get(key).and_then(Value::as_object) {
            for (metric, value) in facts.iter().take(64) {
                let (value, fact_basis) = if let Some(v) = value.get("value") {
                    (
                        v,
                        value.get("basis").and_then(Value::as_str).unwrap_or(&basis),
                    )
                } else {
                    (value, basis.as_str())
                };
                let quote = c
                    .body
                    .lines()
                    .find(|l| l.contains("played ") && l.contains("Points earned"))
                    .unwrap_or("");
                add_fact(
                    conn,
                    c,
                    metric,
                    value,
                    fact_basis,
                    quote,
                    &format!("/metadata/{key}/{metric}"),
                )?;
            }
        }
    }
    // Extract exact label/value statements emitted by the legacy CSV importer.
    if field(c, "dataset") == "epl-results-v1" && kind == "Match" {
        for line in c.body.lines() {
            for (label, metric) in [
                ("Full-time score: ", "score"),
                ("Half-time score: ", "half_time"),
                ("Referee: ", "referee"),
                ("Shots: ", "shots"),
                ("Shots on target: ", "shots_on_target"),
                ("Corners: ", "corners"),
                ("Yellow cards: ", "yellow_cards"),
                ("Red cards: ", "red_cards"),
            ] {
                if let Some(value) = line.find(label).map(|at| &line[at + label.len()..]) {
                    let value = value
                        .split(". ")
                        .next()
                        .unwrap_or(value)
                        .trim_end_matches('.');
                    add_fact(
                        conn,
                        c,
                        metric,
                        &json!(value),
                        "",
                        &format!("{label}{value}."),
                        "/body",
                    )?;
                }
            }
        }
    }
    let mut heading = String::new();
    let mut in_heading = false;
    let mut pending: Option<(String, String)> = None;
    for event in Parser::new(&c.body) {
        match event {
            Event::Start(Tag::Heading { .. }) => {
                heading.clear();
                in_heading = true;
            }
            Event::End(TagEnd::Heading(_)) => in_heading = false,
            Event::Start(Tag::Link { dest_url, .. }) => {
                pending = okf::link_target(&c.id, &dest_url).map(|t| (t, String::new()));
            }
            Event::Text(text) | Event::Code(text) => {
                if in_heading && heading.len() < 512 {
                    heading.push_str(&cut(&text, 128));
                }
                if let Some((_, label)) = &mut pending
                    && label.len() < 512
                {
                    label.push_str(&cut(&text, 128));
                }
            }
            Event::End(TagEnd::Link) => {
                if let Some((target, label)) = pending.take() {
                    conn.execute("UPDATE links SET label=?,heading=? WHERE source=? AND target=? AND label=''",params![cut(&label,256),cut(&heading,256),c.id,target])?;
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn default_limit() -> usize {
    8
}
fn default_chars() -> usize {
    16_000
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueryRequest {
    pub question: String,
    #[serde(default = "default_limit")]
    pub limit: usize,
    #[serde(default = "default_chars")]
    pub max_chars: usize,
    #[serde(default)]
    pub plan: Option<QueryPlan>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QueryPlan {
    pub clauses: Vec<Clause>,
}
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Clause {
    pub text: String,
    pub entities: Vec<String>,
    pub period: Option<String>,
    pub date: Option<String>,
    pub metrics: Vec<String>,
    pub kind: Option<String>,
    pub basis: Option<String>,
    pub terms: Vec<String>,
}
#[derive(Debug, Clone, Serialize)]
struct Mention {
    key: String,
    name: String,
    mention: String,
    corrected: bool,
    #[serde(skip)]
    start: usize,
    #[serde(skip)]
    end: usize,
}

fn stop(word: &str) -> bool {
    matches!(
        word,
        "a" | "an"
            | "the"
            | "who"
            | "what"
            | "which"
            | "where"
            | "when"
            | "why"
            | "how"
            | "many"
            | "much"
            | "did"
            | "does"
            | "do"
            | "is"
            | "are"
            | "was"
            | "were"
            | "has"
            | "have"
            | "had"
            | "in"
            | "on"
            | "at"
            | "of"
            | "for"
            | "to"
            | "by"
            | "from"
            | "during"
            | "it"
            | "its"
            | "their"
            | "s"
            | "me"
            | "tell"
            | "please"
            | "can"
            | "could"
            | "would"
            | "should"
            | "record"
            | "recorded"
            | "finish"
            | "finished"
            | "total"
            | "league"
            | "premier"
            | "english"
            | "season"
            | "campaign"
            | "each"
            | "side"
            | "team"
            | "with"
            | "and"
            | "versus"
            | "vs"
            | "compare"
            | "between"
    )
}

fn edit_distance(a: &str, b: &str) -> usize {
    let a: Vec<_> = a.chars().collect();
    let b: Vec<_> = b.chars().collect();
    if a.len().abs_diff(b.len()) > 2 {
        return 3;
    }
    if a.len() > 64 || b.len() > 64 {
        return 3;
    }
    let mut previous2 = vec![0; b.len() + 1];
    let mut previous: Vec<usize> = (0..=b.len()).collect();
    for i in 1..=a.len() {
        let mut current = vec![i; b.len() + 1];
        for j in 1..=b.len() {
            current[j] = (previous[j] + 1)
                .min(current[j - 1] + 1)
                .min(previous[j - 1] + usize::from(a[i - 1] != b[j - 1]));
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                current[j] = current[j].min(previous2[j - 2] + 1);
            }
        }
        previous2 = previous;
        previous = current;
    }
    previous[b.len()]
}

impl Index {
    fn entity_mentions(&self, tokens: &[String]) -> Result<(Vec<Mention>, Vec<String>)> {
        let mut phrases = BTreeSet::new();
        for start in 0..tokens.len() {
            for len in 1..=8.min(tokens.len() - start) {
                phrases.insert(tokens[start..start + len].join(" "));
            }
        }
        let phrases: Vec<_> = phrases.into_iter().take(700).collect();
        let sql = format!(
            "SELECT alias,entity,min(name) FROM retrieval_aliases WHERE alias IN ({}) GROUP BY alias,entity ORDER BY length(alias) DESC,alias LIMIT 64",
            vec!["?"; phrases.len()].join(",")
        );
        let aliases = self
            .conn
            .prepare(&sql)?
            .query_map(params_from_iter(&phrases), |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut candidates = Vec::new();
        let mut warnings = Vec::new();
        let mut ambiguous = BTreeSet::new();
        for (alias, _, _) in &aliases {
            if aliases.iter().filter(|(a, _, _)| a == alias).count() > 1 {
                ambiguous.insert(alias.clone());
            }
        }
        for (alias, key, name) in aliases {
            if ambiguous.contains(&alias) {
                continue;
            }
            let n = alias.split_whitespace().count();
            for start in 0..tokens.len().saturating_sub(n - 1) {
                if tokens[start..start + n].join(" ") == alias {
                    candidates.push(Mention {
                        key: key.clone(),
                        name: name.clone(),
                        mention: alias.clone(),
                        corrected: false,
                        start,
                        end: start + n,
                    });
                }
            }
        }
        for alias in &ambiguous {
            warnings.push(format!("Ambiguous entity alias: {alias}"));
        }
        candidates.sort_by_key(|m| (std::cmp::Reverse(m.end - m.start), m.start));
        let mut selected: Vec<Mention> = Vec::new();
        for m in candidates {
            if !selected.iter().any(|s| m.start < s.end && s.start < m.end) {
                selected.push(m);
            }
        }
        let mut budget = 32;
        // Correct only a bounded alias vocabulary; never scan full document text.
        for start in 0..tokens.len() {
            if selected.iter().any(|m| start >= m.start && start < m.end)
                || stop(&tokens[start])
                || tokens[start].chars().all(char::is_numeric)
            {
                continue;
            }
            let mut found = Vec::new();
            for len in (1..=4.min(tokens.len() - start)).rev() {
                if budget == 0 {
                    break;
                }
                if (start..start + len).any(|i| selected.iter().any(|m| i >= m.start && i < m.end))
                {
                    continue;
                }
                let phrase = tokens[start..start + len].join(" ");
                let chars = phrase.chars().count();
                if !(5..=64).contains(&chars) {
                    continue;
                }
                budget -= 1;
                let initial = phrase.chars().next().unwrap().to_string();
                let rows=self.conn.prepare_cached("SELECT alias,entity,min(name) FROM retrieval_aliases WHERE initial=? AND chars BETWEEN ? AND ? GROUP BY alias,entity ORDER BY alias LIMIT 64")?.query_map(params![initial,chars.saturating_sub(2) as i64,(chars+2) as i64],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
                for (alias, key, name) in rows {
                    if ambiguous.contains(&alias) {
                        continue;
                    }
                    let distance = edit_distance(&phrase, &alias);
                    if distance <= if chars <= 6 { 1 } else { 2 } {
                        found.push((
                            distance,
                            std::cmp::Reverse(len),
                            Mention {
                                key,
                                name,
                                mention: phrase.clone(),
                                corrected: true,
                                start,
                                end: start + len,
                            },
                        ));
                    }
                }
            }
            found.sort_by_key(|(d, l, _)| (*d, *l));
            if let Some((distance, len, best)) = found.first() {
                if found
                    .iter()
                    .any(|(d, l, m)| d == distance && l == len && m.key != best.key)
                {
                    warnings.push(format!("Ambiguous spelling: {}", best.mention));
                } else {
                    selected.push(best.clone());
                }
            }
        }
        selected.sort_by_key(|m| m.start);
        selected.truncate(8);
        Ok((selected, warnings))
    }

    fn resolve_entity(&self, name: &str) -> Result<Vec<(String, String)>> {
        let key = norm(name);
        Ok(self.conn.prepare_cached("SELECT entity,min(name) FROM retrieval_aliases WHERE alias=? OR entity=? GROUP BY entity LIMIT 9")?.query_map(params![key,key],|r|Ok((r.get(0)?,r.get(1)?)))?.collect::<rusqlite::Result<Vec<_>>>()?)
    }
}

fn periods(tokens: &[String]) -> Vec<(usize, String)> {
    let mut result = Vec::new();
    for (i, pair) in tokens.windows(2).enumerate() {
        if pair[0].len() != 4 {
            continue;
        }
        if let (Ok(year), Ok(next)) = (pair[0].parse::<u32>(), pair[1].parse::<u32>())
            && (1800..=2200).contains(&year)
            && ((pair[1].len() == 2 && next == (year + 1) % 100)
                || (pair[1].len() == 4 && next == year + 1))
        {
            result.push((i, format!("{year}/{:02}", (year + 1) % 100)));
        }
    }
    result
}

fn match_date(tokens: &[String]) -> Option<String> {
    let months = [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ];
    for three in tokens.windows(3) {
        let nums: Vec<_> = three.iter().map(|s| s.parse::<u32>().ok()).collect();
        let parts = if three[0].len() == 4 {
            nums[0]
                .zip(nums[1])
                .zip(nums[2])
                .map(|((y, m), d)| (y, m, d))
        } else {
            nums[0].zip(nums[2]).and_then(|(d, y)| {
                nums[1]
                    .or_else(|| {
                        months
                            .iter()
                            .position(|m| *m == three[1])
                            .map(|i| i as u32 + 1)
                    })
                    .map(|m| (y, m, d))
            })
        };
        if let Some((y, m, d)) = parts
            && (1800..=2200).contains(&y)
            && let Some(date) = chrono::NaiveDate::from_ymd_opt(y as i32, m, d)
        {
            return Some(date.to_string());
        }
    }
    None
}

fn metric_for(text: &str, is_match: bool) -> Vec<String> {
    let s = format!(" {} ", norm(text));
    let has = |needle: &str| s.contains(needle);
    let metric = if has("referee") || has("refereed") {
        Some("referee")
    } else if has("yellow card") {
        Some("yellow_cards")
    } else if has("red card") {
        Some("red_cards")
    } else if has("corner") {
        Some("corners")
    } else if has("shots on target") {
        Some("shots_on_target")
    } else if has("shot") {
        Some("shots")
    } else if has("half time") {
        Some("half_time")
    } else if is_match && (has("score") || has("result") || has("finish") || has("won")) {
        Some("score")
    } else if has("top scorer") || has("leading scorer") {
        Some("top_scorer")
    } else if has("manager") || has("managed") {
        Some("manager")
    } else if has("point") {
        Some("points")
    } else if has("goal difference") {
        Some("goal_difference")
    } else if has("concede") || has("conceded") || has("net breached") || has("net against") {
        Some("goals_against")
    } else if has("goal") || has("find the net") {
        Some("goals_for")
    } else if has("draw") || has("drew") || has("stalemate") {
        Some("drawn")
    } else if has("defeat") || has("lost") || has("lose") {
        Some("lost")
    } else if has(" win") || has("won") || has("victor") {
        Some("won")
    } else if has(" play") || (has("how many") && (has("matches") || has("games"))) {
        Some("played")
    } else {
        None
    };
    let mut metrics: Vec<String> = metric.map(|m| vec![m.into()]).unwrap_or_default();
    // Keep independently requested measures, rather than silently accepting only one.
    if has(" and ") || has(" both ") || has(" versus ") {
        for (needles, metric) in [
            (vec![" win", " won ", "victor"], "won"),
            (vec!["draw", "drew", "stalemate"], "drawn"),
            (vec!["defeat", "lost", "lose"], "lost"),
            (vec!["point"], "points"),
            (vec!["goals scored", "goals for"], "goals_for"),
            (vec!["concede", "goals against"], "goals_against"),
            (vec!["yellow card"], "yellow_cards"),
            (vec!["red card"], "red_cards"),
        ] {
            if needles.iter().any(|n| has(n)) && !metrics.iter().any(|m| m == metric) {
                metrics.push(metric.into());
            }
        }
    }
    metrics
}

fn basis_for(text: &str) -> Option<String> {
    let s = norm(text);
    if (s.contains("after") || s.contains("official")) && s.contains("deduction") {
        Some("official_after_deductions".into())
    } else if s.contains("before") && s.contains("deduction") {
        Some("earned_before_adjustments".into())
    } else {
        None
    }
}

fn validate_plan(plan: &QueryPlan) -> Result<()> {
    if plan.clauses.is_empty() || plan.clauses.len() > 4 {
        return Err(invalid("A plan requires 1 to 4 clauses."));
    }
    for clause in &plan.clauses {
        if clause.entities.len() > 4
            || clause.metrics.len() > 4
            || clause.terms.len() > 32
            || clause.text.len() > 2048
        {
            return Err(invalid("Query plan exceeds its bounds."));
        }
        for value in clause
            .entities
            .iter()
            .chain(&clause.metrics)
            .chain(&clause.terms)
            .chain(clause.period.iter())
            .chain(clause.date.iter())
            .chain(clause.kind.iter())
            .chain(clause.basis.iter())
        {
            if value.is_empty() || value.len() > 160 || value.chars().any(char::is_control) {
                return Err(invalid("Invalid plan field."));
            }
        }
        if let Some(date) = &clause.date
            && chrono::NaiveDate::parse_from_str(date, "%Y-%m-%d")
                .ok()
                .is_none_or(|d| d.to_string() != *date)
        {
            return Err(invalid("Dates must be YYYY-MM-DD."));
        }
    }
    Ok(())
}

impl Index {
    fn automatic_plan(&self, question: &str) -> Result<(QueryPlan, Vec<Mention>, Vec<String>)> {
        let tokens: Vec<_> = words(question).into_iter().take(96).collect();
        let (mentions, mut warnings) = self.entity_mentions(&tokens)?;
        let periods = periods(&tokens);
        let date = match_date(&tokens);
        let mut clauses = Vec::new();
        if date.is_some() {
            clauses.push(Clause {
                text: question.into(),
                entities: mentions.iter().map(|m| m.key.clone()).collect(),
                date,
                period: periods.first().map(|p| p.1.clone()),
                metrics: metric_for(question, true),
                kind: None,
                basis: basis_for(question),
                terms: vec![],
            });
        } else if !periods.is_empty() {
            if periods.len() > 4 {
                return Err(invalid("At most four periods can be queried at once."));
            }
            for (n, (position, period)) in periods.iter().enumerate() {
                let nearest = mentions
                    .iter()
                    .filter(|m| m.start <= *position)
                    .max_by_key(|m| m.start)
                    .or_else(|| mentions.first());
                let start = if n == 0 { 0 } else { periods[n - 1].0 + 2 };
                let end = mentions
                    .iter()
                    .filter(|m| m.start > *position + 1)
                    .map(|m| m.start)
                    .min()
                    .unwrap_or(tokens.len())
                    .min(periods.get(n + 1).map(|p| p.0).unwrap_or(tokens.len()));
                // Use the local clause for different entity/metric comparisons, and inherit the metric for repeated periods.
                let local_start = nearest
                    .map(|m| m.start.max(start))
                    .unwrap_or(start)
                    .min(*position);
                let local = tokens[local_start..end].join(" ");
                let mut metrics = metric_for(&local, false);
                if metrics.is_empty()
                    && norm(&local)
                        .split_whitespace()
                        .any(|w| matches!(w, "score" | "scored"))
                    && norm(question).contains("goal")
                {
                    metrics = vec!["goals_for".into()];
                }
                if metrics.is_empty() {
                    metrics = metric_for(question, false);
                }
                clauses.push(Clause {
                    text: cut(&local, 2048),
                    entities: nearest.map(|m| vec![m.key.clone()]).unwrap_or_default(),
                    period: Some(period.clone()),
                    metrics,
                    kind: None,
                    basis: basis_for(question),
                    ..Default::default()
                });
            }
        } else {
            let mut metrics = metric_for(question, false);
            if metrics.is_empty() {
                let registry=self.conn.prepare_cached("SELECT DISTINCT metric FROM retrieval_facts ORDER BY length(metric) DESC LIMIT 128")?.query_map([],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
                for metric in registry {
                    if words(&metric).iter().all(|w| tokens.contains(w)) {
                        metrics.push(metric);
                        break;
                    }
                }
            }
            let terms = tokens
                .iter()
                .enumerate()
                .filter(|(i, t)| !stop(t) && !mentions.iter().any(|m| *i >= m.start && *i < m.end))
                .map(|(_, t)| t.clone())
                .take(24)
                .collect();
            clauses.push(Clause {
                text: question.into(),
                entities: mentions.iter().map(|m| m.key.clone()).collect(),
                metrics,
                basis: basis_for(question),
                terms,
                ..Default::default()
            });
        }
        if periods.len() == 1
            && date_is_absent(&clauses)
            && mentions.len() > 1
            && tokens
                .iter()
                .any(|t| matches!(t.as_str(), "compare" | "versus" | "vs" | "and" | "with"))
        {
            if mentions.len() > 4 {
                return Err(invalid(
                    "Split comparisons with more than four entities into multiple requests.",
                ));
            }
            let template = clauses[0].clone();
            clauses = mentions
                .iter()
                .take(4)
                .map(|m| {
                    let mut c = template.clone();
                    c.entities = vec![m.key.clone()];
                    c
                })
                .collect();
        }
        for c in &mut clauses {
            c.entities.sort();
            c.entities.dedup();
            if c.entities.is_empty() && c.metrics.is_empty() {
                warnings.push("Entity or requested fact could not be resolved; inspect candidates or supply a plan.".into());
            }
            if c.terms.is_empty() && c.metrics.is_empty() {
                c.terms = tokens
                    .iter()
                    .filter(|t| !stop(t))
                    .take(24)
                    .cloned()
                    .collect();
            }
        }
        warnings.sort();
        warnings.dedup();
        Ok((QueryPlan { clauses }, mentions, warnings))
    }
}

#[derive(Debug, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct BrowseRequest {
    pub prefix: String,
    pub kind: String,
    pub period: String,
    pub limit: Option<usize>,
    pub offset: usize,
}
#[derive(Debug, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct NeighborRequest {
    pub id: String,
    pub direction: String,
    pub kind: String,
    pub period: String,
    pub entity: String,
    pub limit: Option<usize>,
}
fn bounded(value: &str) -> Result<()> {
    if value.len() > 256 || value.chars().any(char::is_control) {
        return Err(invalid("Filter exceeds its bounds."));
    }
    Ok(())
}
fn scope(clause: &Clause, sql: &mut String, values: &mut Vec<String>) {
    for (column, value) in [
        ("d.period", &clause.period),
        ("d.date", &clause.date),
        ("d.kind", &clause.kind),
    ] {
        if let Some(value) = value {
            sql.push_str(&format!(" AND {column}=?"));
            values.push(value.clone());
        }
    }
    for entity in &clause.entities {
        sql.push_str(" AND EXISTS(SELECT 1 FROM retrieval_entities e WHERE e.concept_id=d.concept_id AND e.entity=?)");
        values.push(entity.clone());
    }
}
impl Index {
    pub fn browse(&self, request: BrowseRequest) -> Result<Value> {
        let limit = request.limit.unwrap_or(30);
        if !(1..=100).contains(&limit) || request.offset > 1_000_000 {
            return Err(invalid("Invalid browse bounds."));
        }
        for value in [&request.prefix, &request.kind, &request.period] {
            bounded(value)?;
        }
        let mut sql = String::from(
            "SELECT c.id,c.revision,c.title,c.description,d.kind,d.period,d.date FROM retrieval_docs d JOIN concepts c ON c.id=d.concept_id WHERE substr(c.id,1,length(?))=?",
        );
        let mut values = vec![request.prefix.clone(), request.prefix];
        for (col, value) in [("d.kind", request.kind), ("d.period", request.period)] {
            if !value.is_empty() {
                sql.push_str(&format!(" AND {col}=?"));
                values.push(value);
            }
        }
        sql.push_str(" ORDER BY c.id LIMIT ? OFFSET ?");
        values.push((limit + 1).to_string());
        values.push(request.offset.to_string());
        let mut rows=self.conn.prepare(&sql)?.query_map(params_from_iter(&values),|r|Ok(json!({"id":r.get::<_,String>(0)?,"revision":r.get::<_,String>(1)?,"title":r.get::<_,String>(2)?,"description":r.get::<_,String>(3)?,"kind":r.get::<_,String>(4)?,"period":r.get::<_,String>(5)?,"date":r.get::<_,String>(6)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
        let more = rows.len() > limit;
        rows.truncate(limit);
        Ok(
            json!({"concepts":rows,"next_offset":if more{Some(request.offset+limit)}else{None},"synthetic_index":true}),
        )
    }
    pub fn neighbors(&self, request: NeighborRequest) -> Result<Value> {
        okf::validate_id(&request.id)?;
        let limit = request.limit.unwrap_or(12);
        if !(1..=64).contains(&limit) {
            return Err(invalid("Neighbor limit must be 1 to 64."));
        }
        for value in [&request.kind, &request.period, &request.entity] {
            bounded(value)?;
        }
        let incoming = match request.direction.as_str() {
            "" | "outgoing" => false,
            "incoming" => true,
            _ => return Err(invalid("Direction must be outgoing or incoming.")),
        };
        let (target, seed) = if incoming {
            ("source", "target")
        } else {
            ("target", "source")
        };
        let mut sql = format!(
            "SELECT c.id,c.revision,c.title,d.kind,d.period,l.label,l.heading FROM links l JOIN concepts c ON c.id=l.{target} JOIN retrieval_docs d ON d.concept_id=c.id WHERE l.{seed}=?"
        );
        let mut values = vec![request.id];
        let mut clause = Clause {
            kind: (!request.kind.is_empty()).then_some(request.kind),
            period: (!request.period.is_empty()).then_some(request.period),
            ..Default::default()
        };
        if !request.entity.is_empty() {
            let matches = self.resolve_entity(&request.entity)?;
            if matches.len() != 1 {
                return Err(invalid("Neighbor entity is unknown or ambiguous."));
            }
            clause.entities.push(matches[0].0.clone());
        }
        scope(&clause, &mut sql, &mut values);
        sql.push_str(" ORDER BY c.id LIMIT ?");
        values.push((limit + 1).to_string());
        let mut rows=self.conn.prepare(&sql)?.query_map(params_from_iter(&values),|r|Ok(json!({"id":r.get::<_,String>(0)?,"revision":r.get::<_,String>(1)?,"title":r.get::<_,String>(2)?,"kind":r.get::<_,String>(3)?,"period":r.get::<_,String>(4)?,"label":r.get::<_,String>(5)?,"heading":r.get::<_,String>(6)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
        let more = rows.len() > limit;
        rows.truncate(limit);
        Ok(
            json!({"neighbors":rows,"has_more":more,"direction":if incoming{"incoming"}else{"outgoing"}}),
        )
    }
    fn fact_evidence(&self, id: &str, clause: &Clause) -> Result<Vec<Value>> {
        let mut result = Vec::new();
        for metric in &clause.metrics {
            let fact=self.conn.prepare_cached("SELECT value,basis,quote,pointer FROM retrieval_facts WHERE concept_id=? AND metric=?")?.query_row(params![id,metric],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?))).optional()?;
            if let Some((value, basis, quote, pointer)) = fact {
                result.push(json!({"metric":metric,"value":serde_json::from_str::<Value>(&value)?,"basis":basis,"quote":quote,"pointer":pointer}));
            }
        }
        Ok(result)
    }
    fn candidates(&self, clause: &Clause, lexical: bool) -> Result<Vec<String>> {
        let mut sql = String::from(
            "SELECT d.concept_id FROM retrieval_docs d JOIN concepts c ON c.id=d.concept_id",
        );
        if lexical {
            sql.push_str(" JOIN concept_fts ON concept_fts.rowid=c.record_id");
        }
        sql.push_str(" WHERE 1=1");
        let mut values = Vec::new();
        scope(clause, &mut sql, &mut values);
        if lexical {
            let terms: Vec<_> = clause
                .terms
                .iter()
                .flat_map(|t| words(t))
                .filter(|w| !stop(w))
                .take(24)
                .map(|t| format!("\"{t}\"*"))
                .collect();
            if terms.is_empty() {
                return Ok(vec![]);
            }
            sql.push_str(" AND concept_fts MATCH ?");
            values.push(terms.join(" OR "));
        } else {
            if clause.metrics.is_empty() {
                return Ok(vec![]);
            }
            for metric in &clause.metrics {
                sql.push_str(" AND EXISTS(SELECT 1 FROM retrieval_facts f WHERE f.concept_id=d.concept_id AND f.metric=?");
                values.push(metric.clone());
                if let Some(basis) = &clause.basis {
                    sql.push_str(" AND f.basis=?");
                    values.push(basis.clone());
                }
                sql.push(')');
            }
        }
        if lexical {
            sql.push_str(" ORDER BY bm25(concept_fts,0,8,4,3,6,1),d.concept_id LIMIT 20");
        } else {
            sql.push_str(" ORDER BY d.concept_id LIMIT 20");
        }
        Ok(self
            .conn
            .prepare(&sql)?
            .query_map(params_from_iter(&values), |r| r.get(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn query(&self, request: QueryRequest) -> Result<Value> {
        let started = Instant::now();
        if words(&request.question).is_empty()
            || request.question.len() > 4096
            || words(&request.question).len() > 96
        {
            return Err(invalid(
                "Question must contain 1 to 96 words and at most 4096 bytes.",
            ));
        }
        if !(1..=16).contains(&request.limit) || !(2048..=64000).contains(&request.max_chars) {
            return Err(invalid("Use limit 1 to 16 and max_chars 2048 to 64000."));
        }
        let supplied = request.plan.is_some();
        let (mut plan, mentions, mut warnings) = if let Some(plan) = request.plan {
            (plan, vec![], vec![])
        } else {
            self.automatic_plan(&request.question)?
        };
        validate_plan(&plan)?;
        if request.limit < plan.clauses.len() {
            return Err(invalid(
                "Result limit must reserve at least one result for every clause.",
            ));
        }
        let mut unresolved = BTreeSet::new();
        for (n, clause) in plan.clauses.iter_mut().enumerate() {
            for entity in &mut clause.entities {
                let matches = self.resolve_entity(entity)?;
                if matches.len() == 1 {
                    *entity = matches[0].0.clone();
                } else {
                    unresolved.insert(n);
                    warnings.push(format!("Clause {n}: unknown or ambiguous entity: {entity}"));
                }
            }
            clause.metrics = clause.metrics.iter().map(|m| canonical_metric(m)).collect();
        }
        // These operators require interpretation beyond this deterministic planner.
        let qt = words(&request.question);
        let distinct_dates: BTreeSet<_> = qt.windows(3).filter_map(match_date).collect();
        let complex = !supplied
            && (distinct_dates.len() > 1
                || qt.iter().any(|t| {
                    matches!(
                        t.as_str(),
                        "not"
                            | "except"
                            | "excluding"
                            | "without"
                            | "never"
                            | "latest"
                            | "current"
                            | "today"
                    )
                })
                || (qt.iter().any(|t| matches!(t.as_str(), "before" | "after"))
                    && !qt.iter().any(|t| t.starts_with("deduction"))));
        if complex {
            warnings.push("Unresolved negation, time comparison or freshness requirement; verify candidates or supply a complete plan.".into());
        }
        let ambiguous = warnings.iter().any(|w| w.starts_with("Ambiguous"));
        let mut pools = Vec::new();
        let mut supports = Vec::new();
        let mut expanded = 0usize;
        for (n, clause) in plan.clauses.iter().enumerate() {
            if unresolved.contains(&n) || ambiguous {
                pools.push(vec![]);
                supports.push(vec![]);
                continue;
            }
            let facts = self.candidates(clause, false)?;
            // Broad fact matches are candidates until an entity and a time/kind scope identify the requested fact.
            let scoped = !clause.entities.is_empty()
                && (clause.period.is_some() || clause.date.is_some() || clause.kind.is_some());
            let mut conflicts = false;
            let mut signatures = BTreeSet::new();
            for id in &facts {
                let signature = self
                    .fact_evidence(id, clause)?
                    .iter()
                    .map(|f| format!("{}:{}:{}", f["metric"], f["value"], f["basis"]))
                    .collect::<Vec<_>>()
                    .join("|");
                signatures.insert(signature);
                let (status, stale): (String, String) = self.conn.query_row(
                    "SELECT status,stale_after FROM retrieval_docs WHERE concept_id=?",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )?;
                if matches!(status.as_str(), "draft" | "deprecated")
                    || chrono::DateTime::parse_from_rfc3339(&stale)
                        .is_ok_and(|d| d < chrono::Utc::now())
                {
                    conflicts = true;
                }
            }
            if signatures.len() > 1 || facts.len() >= 20 {
                conflicts = true;
                warnings.push(format!(
                    "Clause {n}: conflicting or truncated fact candidates; inspect sources."
                ));
            }
            let supported = if scoped && !complex && !conflicts {
                facts.clone()
            } else {
                vec![]
            };
            let mut pool = facts;
            if pool.is_empty() {
                pool = self.candidates(clause, true)?;
            }
            // One-hop expansion is scoped before SQL LIMIT, never a full adjacency read.
            if supported.is_empty() && !pool.is_empty() {
                let seeds: Vec<_> = pool.iter().take(2).cloned().collect();
                for seed in seeds {
                    let mut sql = String::from(
                        "SELECT d.concept_id FROM links l JOIN retrieval_docs d ON d.concept_id=l.target WHERE l.source=?",
                    );
                    let mut values = vec![seed];
                    scope(clause, &mut sql, &mut values);
                    sql.push_str(" ORDER BY d.concept_id LIMIT 4");
                    let neighbors = self
                        .conn
                        .prepare(&sql)?
                        .query_map(params_from_iter(&values), |r| r.get::<_, String>(0))?
                        .collect::<rusqlite::Result<Vec<_>>>()?;
                    for id in neighbors {
                        if !pool.contains(&id) && pool.len() < 20 {
                            pool.push(id);
                            expanded += 1;
                        }
                    }
                }
            }
            pools.push(pool);
            supports.push(supported);
        }
        // Reserve each clause's strongest candidate before filling any remaining slots.
        let mut selected = Vec::<String>::new();
        for pool in &pools {
            if let Some(id) = pool.first()
                && !selected.contains(id)
            {
                selected.push(id.clone());
            }
        }
        for rank in 1..20 {
            for pool in &pools {
                if selected.len() < request.limit
                    && let Some(id) = pool.get(rank)
                    && !selected.contains(id)
                {
                    selected.push(id.clone());
                }
            }
        }
        let mut results = Vec::new();
        let per_doc = (request.max_chars / selected.len().max(1)).min(4000);
        for id in selected {
            let doc = self.get(&id)?;
            let meta = &doc["metadata"];
            let mut facts = Vec::new();
            let mut clauses = Vec::new();
            for (n, clause) in plan.clauses.iter().enumerate() {
                if pools[n].contains(&id) {
                    clauses.push(n);
                    for fact in self.fact_evidence(&id, clause)? {
                        if !facts.contains(&fact) {
                            facts.push(fact);
                        }
                    }
                }
            }
            let mut excerpt = facts
                .iter()
                .filter_map(|f| f["quote"].as_str())
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join("\n");
            if excerpt.is_empty() {
                excerpt = best_excerpt(doc["body"].as_str().unwrap_or(""), &plan, per_doc);
            }
            // Keep source qualifications adjacent to facts, especially derived points.
            if let Some(line) = doc["body"]
                .as_str()
                .unwrap_or("")
                .lines()
                .find(|l| l.contains("BEFORE disciplinary deductions"))
            {
                excerpt.push('\n');
                excerpt.push_str(line);
            }
            let (kind,period,date,status,stale):(String,String,String,String,String)=self.conn.query_row("SELECT kind,period,date,status,stale_after FROM retrieval_docs WHERE concept_id=?",[&id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
            let expired =
                chrono::DateTime::parse_from_rfc3339(&stale).is_ok_and(|d| d < chrono::Utc::now());
            if expired || status == "draft" || status == "deprecated" {
                warnings.push(format!(
                    "Evidence {id} has status {status}, stale_after={stale}; inspect provenance."
                ));
            }
            results.push(json!({"id":id,"revision":doc["revision"],"title":doc["title"],"kind":kind,"period":period,"date":date,"status":status,"stale_after":stale,"sources":meta["sources"].as_array().map(|v|v.iter().take(8).cloned().collect::<Vec<_>>()).unwrap_or_default(),"clauses":clauses,"facts":facts,"excerpt":cut(&excerpt,per_doc)}));
        }
        let coverage:Vec<_>=plan.clauses.iter().enumerate().map(|(n,c)|{
            let ids:Vec<_>=results.iter().filter_map(|r|r["id"].as_str()).filter(|id|supports[n].iter().any(|s|s==id)).collect();
            json!({"clause":n,"metrics":c.metrics,"support":if !ids.is_empty(){"structured_fact"}else if !pools[n].is_empty(){"candidate_only"}else{"missing"},"evidence_ids":ids})
        }).collect();
        let plan_satisfied = coverage.iter().all(|c| c["support"] == "structured_fact");
        let status = if plan_satisfied {
            "needs_verification"
        } else if coverage.iter().any(|c| c["support"] == "structured_fact") {
            "partial"
        } else if results.is_empty() {
            "unsupported"
        } else {
            "needs_verification"
        };
        warnings.sort();
        warnings.dedup();
        Ok(
            json!({"question":request.question,"planner":if supplied{"caller_supplied"}else{"deterministic"},"plan":plan,"entities":mentions,"status":status,"complete":false,"answer_supported":false,"plan_satisfied":plan_satisfied,"verification_required":true,"coverage":coverage,"results":results,"warnings":warnings,"budget":{"max_chars":request.max_chars,"excerpt_chars":results.iter().filter_map(|r|r["excerpt"].as_str()).map(|s|s.chars().count()).sum::<usize>(),"max_candidates_per_clause":20,"graph_hops":1,"expanded":expanded},"elapsed_ms":started.elapsed().as_secs_f64()*1000.0,"model_calls":0}),
        )
    }
}
fn best_excerpt(body: &str, plan: &QueryPlan, budget: usize) -> String {
    let terms: Vec<_> = plan
        .clauses
        .iter()
        .flat_map(|c| c.terms.iter())
        .flat_map(|t| words(t))
        .collect();
    let lines: Vec<_> = body.lines().collect();
    let best = lines
        .iter()
        .enumerate()
        .max_by_key(|(_, line)| {
            let text = norm(line);
            terms.iter().filter(|t| text.contains(t.as_str())).count()
        })
        .map(|(i, _)| i)
        .unwrap_or(0);
    // Include the nearest heading and adjacent table header where available.
    if lines.is_empty() {
        return String::new();
    }
    let heading = lines[..=best.min(lines.len().saturating_sub(1))]
        .iter()
        .rev()
        .find(|l| l.starts_with('#'))
        .copied()
        .unwrap_or("");
    let start = best.saturating_sub(2);
    cut(
        &format!(
            "{heading}\n{}",
            lines[start..lines.len().min(best + 6)].join("\n")
        ),
        budget,
    )
}

fn date_is_absent(clauses: &[Clause]) -> bool {
    clauses.iter().all(|c| c.date.is_none())
}
