use crate::error::{Error, Result};
use pulldown_cmark::{Event, Parser, Tag};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub const MAX_CONCEPT: usize = 1_000_000;

pub fn validate_id(id: &str) -> Result<()> {
    let valid = id.len() <= 240
        && !id.ends_with(".md")
        && id.split('/').all(|part| {
            let first = part.bytes().next();
            matches!(first, Some(b'a'..=b'z' | b'0'..=b'9'))
                && part
                    .bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(&c))
        });
    if !valid || matches!(id.rsplit('/').next(), Some("index" | "log")) {
        return Err(Error::new(
            400,
            "INVALID_ID",
            "Use a lowercase bundle-relative concept ID without .md; index and log are reserved.",
        ));
    }
    Ok(())
}

pub fn revision(content: &str) -> String {
    format!("{:x}", Sha256::digest(content.as_bytes()))
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Concept {
    pub id: String,
    pub revision: String,
    pub markdown: String,
    pub metadata: Map<String, Value>,
    pub title: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub description: String,
    pub tags: Vec<String>,
    pub aliases: Vec<String>,
    pub body: String,
    pub links: Vec<String>,
}

fn invalid(message: &str) -> Error {
    Error::new(422, "INVALID_OKF", message)
}

pub fn parse(id: &str, markdown: &str) -> Result<Concept> {
    validate_id(id)?;
    if markdown.len() > MAX_CONCEPT || markdown.contains('\0') {
        return Err(invalid(
            "Concepts must be at most 1 MB and contain no NUL bytes.",
        ));
    }
    let text = markdown.strip_prefix('\u{feff}').unwrap_or(markdown);
    let Some(header_end) = text.find('\n') else {
        return Err(invalid("Missing YAML frontmatter."));
    };
    if text[..header_end].trim_end_matches('\r') != "---" {
        return Err(invalid("Missing YAML frontmatter."));
    }
    let start = header_end + 1;
    let mut offset = start;
    let mut end = None;
    for line in text[start..].split_inclusive('\n') {
        if line.trim_end_matches(['\n', '\r']) == "---" {
            end = Some((offset, offset + line.len()));
            break;
        }
        offset += line.len();
    }
    let Some((yaml_end, body_start)) = end else {
        return Err(invalid("Frontmatter requires a closing ---."));
    };
    let mut options = serde_saphyr::Options::default();
    options.budget = serde_saphyr::budget! { max_depth:32,max_nodes:8192,max_events:32768,max_aliases:64,max_anchors:64,max_total_scalar_bytes:1_000_000,max_recorded_anchor_bytes:1_000_000 };
    options.alias_limits = serde_saphyr::alias_limits! {max_replay_stack_depth:16,max_total_replayed_events:8192,max_alias_expansions_per_anchor:64};
    let metadata: Map<String, Value> =
        serde_saphyr::from_str_with_options(&text[start..yaml_end], options).map_err(|_| {
            invalid("Frontmatter must be a valid, non-cyclic YAML mapping with unique keys.")
        })?;
    let kind = metadata
        .get("type")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| invalid("OKF requires a nonempty type string."))?
        .to_owned();
    for field in ["title", "description", "resource"] {
        if metadata.get(field).is_some_and(|value| !value.is_string()) {
            return Err(invalid(
                "title, description and resource must be strings when present.",
            ));
        }
    }
    if metadata.get("tags").is_some_and(|value| {
        !value
            .as_array()
            .is_some_and(|values| values.iter().all(Value::is_string))
    }) {
        return Err(invalid("tags must be an array of strings."));
    }
    if metadata.get("sources").is_some_and(|value| {
        !value.as_array().is_some_and(|values| {
            values.iter().all(|source| {
                source
                    .get("resource")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.trim().is_empty())
            })
        })
    }) {
        return Err(invalid("Each sources entry must have a resource string."));
    }
    let body = &text[body_start..];
    let mut links = BTreeSet::new();
    // A pull parser walks links without building a whole-document Markdown AST.
    for event in Parser::new(body) {
        if let Event::Start(Tag::Link { dest_url, .. }) = event
            && let Some(target) = link_target(id, &dest_url)
        {
            links.insert(target);
        }
    }
    let strings = |key: &str| {
        metadata
            .get(key)
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default()
    };
    Ok(Concept {
        id: id.into(),
        revision: revision(markdown),
        markdown: markdown.into(),
        title: metadata
            .get("title")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(id.rsplit('/').next().unwrap())
            .into(),
        description: metadata
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("")
            .into(),
        tags: strings("tags"),
        aliases: strings("aliases"),
        kind,
        body: body.into(),
        links: links.into_iter().collect(),
        metadata,
    })
}

pub(crate) fn link_target(id: &str, url: &str) -> Option<String> {
    if url.contains(':') || url.starts_with("//") {
        return None;
    }
    let raw = url.split(['?', '#']).next()?;
    let decoded = percent_encoding::percent_decode_str(raw)
        .decode_utf8()
        .ok()?;
    let path = decoded.strip_suffix(".md")?;
    let mut parts = if path.starts_with('/') {
        Vec::new()
    } else {
        id.split('/')
            .rev()
            .skip(1)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect()
    };
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop()?;
            }
            value => parts.push(value),
        }
    }
    let target = parts.join("/");
    validate_id(&target).ok()?;
    Some(target)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preserves_okf_and_follows_commonmark_links() {
        let text = "---\ntype: Custom\ncustom: { retained: true }\n---\n[A](./target.md#x) and [B][ref].\n\n[ref]: /other/link.md\n";
        let concept = parse("notes/source", text).unwrap();
        assert_eq!(concept.markdown, text);
        assert_eq!(concept.metadata["custom"]["retained"], true);
        assert_eq!(concept.links, ["notes/target", "other/link"]);
    }
    #[test]
    fn rejects_invalid_metadata_and_paths() {
        for id in [
            "../escape",
            "/absolute",
            "Uppercase",
            ".git/config",
            "index",
            "a//b",
            "name.md",
        ] {
            assert!(validate_id(id).is_err());
        }
        for text in [
            "no header",
            "---\ntitle: Missing\n---\n",
            "---\ntype: Note\ntype: Other\n---\n",
            "---\ntype: Note\nsources: [{}]\n---\n",
        ] {
            assert!(parse("valid", text).is_err(), "{text}");
        }
    }
}
