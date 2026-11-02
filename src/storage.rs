use crate::{
    error::{Error, Result},
    okf::{self, Concept},
};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::UNIX_EPOCH,
};

pub fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WriteInput {
    pub request_id: String,
    pub actor: String,
    pub action: String,
    pub id: String,
    pub expected_revision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub markdown: Option<String>,
}

pub fn validate_write(value: Value) -> Result<WriteInput> {
    if value.get("expectedRevision").is_none() {
        return Err(Error::new(
            400,
            "INVALID_WRITE",
            "expectedRevision is required; use null only for creation.",
        ));
    }
    let input: WriteInput = serde_json::from_value(value)
        .map_err(|e| Error::new(400, "INVALID_WRITE", e.to_string()))?;
    okf::validate_id(&input.id)?;
    let valid_id = !input.request_id.is_empty()
        && input.request_id.len() <= 128
        && input
            .request_id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(&c))
        && input.request_id.as_bytes()[0].is_ascii_alphanumeric();
    let valid_actor = !input.actor.is_empty()
        && input.actor.len() <= 800
        && !input.actor.chars().any(char::is_control);
    let valid_revision = input.expected_revision.as_ref().is_none_or(|r| {
        r.len() == 64
            && r.bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    });
    let valid_action = match input.action.as_str() {
        "put" => input.markdown.is_some(),
        "delete" => input.markdown.is_none() && input.expected_revision.is_some(),
        _ => false,
    };
    if !valid_id || !valid_actor || !valid_revision || !valid_action {
        return Err(Error::new(
            400,
            "INVALID_WRITE",
            "Invalid request ID, actor, action or revision precondition.",
        ));
    }
    if input
        .markdown
        .as_ref()
        .is_some_and(|m| m.len() > okf::MAX_CONCEPT)
    {
        return Err(Error::new(
            422,
            "INVALID_OKF",
            "Concepts are limited to 1 MB.",
        ));
    }
    Ok(input)
}

fn fingerprint(input: &WriteInput) -> String {
    let data = json!([
        input.request_id,
        input.actor,
        input.action,
        input.id,
        input.expected_revision,
        input.markdown
    ]);
    format!("{:x}", Sha256::digest(data.to_string().as_bytes()))
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Operation {
    pub sequence: i64,
    pub request_id: String,
    pub state: String,
    pub input: WriteInput,
    pub status: Option<u16>,
    pub result: Value,
    pub accepted_at: String,
    pub completed_at: Option<String>,
}

fn connection(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path)?;
    conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=3000; PRAGMA cache_size=-256; PRAGMA mmap_size=0; PRAGMA temp_store=FILE; PRAGMA wal_autocheckpoint=128; PRAGMA journal_size_limit=1048576;")?;
    conn.set_prepared_statement_cache_capacity(16);
    Ok(conn)
}

pub struct Operations {
    pub conn: Connection,
}
impl Operations {
    pub fn open(dir: &Path) -> Result<Self> {
        let conn = connection(&dir.join("operations.sqlite"))?;
        conn.execute_batch("CREATE TABLE IF NOT EXISTS operations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL, input TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','prepared','applied','rejected')),
            status INTEGER, result TEXT, accepted_at TEXT NOT NULL, completed_at TEXT);
            CREATE INDEX IF NOT EXISTS pending_operations ON operations(sequence) WHERE state IN ('queued','prepared');")?;
        Ok(Self { conn })
    }
    pub fn get(&self, id: &str) -> Result<Option<Operation>> {
        let row = self.conn.prepare_cached("SELECT sequence,request_id,state,input,status,result,accepted_at,completed_at FROM operations WHERE request_id=?")?
            .query_row([id], |row| Ok((row.get::<_,i64>(0)?, row.get::<_,String>(1)?, row.get::<_,String>(2)?, row.get::<_,String>(3)?, row.get::<_,Option<u16>>(4)?, row.get::<_,Option<String>>(5)?, row.get::<_,String>(6)?, row.get::<_,Option<String>>(7)?))).optional()?;
        row.map(
            |(sequence, request_id, state, input, status, result, accepted_at, completed_at)| {
                Ok(Operation {
                    sequence,
                    request_id,
                    state,
                    input: serde_json::from_str(&input)?,
                    status,
                    result: result
                        .map(|s| serde_json::from_str(&s))
                        .transpose()?
                        .unwrap_or(Value::Null),
                    accepted_at,
                    completed_at,
                })
            },
        )
        .transpose()
    }
    pub fn accept(&self, input: &WriteInput, max_pending: usize) -> Result<(Operation, bool)> {
        if let Some(existing) = self.get(&input.request_id)? {
            let stored: String = self.conn.query_row(
                "SELECT fingerprint FROM operations WHERE request_id=?",
                [&input.request_id],
                |r| r.get(0),
            )?;
            if stored != fingerprint(input) {
                return Err(Error::new(
                    409,
                    "REQUEST_ID_REUSED",
                    "This ID belongs to different input. Use a new request ID.",
                ));
            }
            return Ok((existing, false));
        }
        if self.pending_count()? >= max_pending {
            return Err(Error::new(
                503,
                "QUEUE_FULL",
                "Write queue is full. Retry with the same ID and payload.",
            ));
        }
        self.conn.prepare_cached("INSERT INTO operations(request_id,fingerprint,input,state,accepted_at) VALUES(?,?,?,'queued',?)")?
            .execute(params![input.request_id,fingerprint(input),serde_json::to_string(input)?,now()])?;
        Ok((self.get(&input.request_id)?.unwrap(), true))
    }
    pub fn prepare(&self, id: &str) -> Result<()> {
        self.conn
            .prepare_cached(
                "UPDATE operations SET state='prepared' WHERE request_id=? AND state='queued'",
            )?
            .execute([id])?;
        Ok(())
    }
    pub fn finish(&self, id: &str, status: u16, result: &Value) -> Result<()> {
        self.conn
            .prepare_cached(
                "UPDATE operations SET state=?,status=?,result=?,completed_at=? WHERE request_id=?",
            )?
            .execute(params![
                if status == 200 { "applied" } else { "rejected" },
                status,
                result.to_string(),
                now(),
                id
            ])?;
        Ok(())
    }
    pub fn pending_count(&self) -> Result<usize> {
        Ok(self.conn.query_row(
            "SELECT count(*) FROM operations WHERE state IN ('queued','prepared')",
            [],
            |r| r.get::<_, i64>(0),
        )? as usize)
    }
    pub fn next_pending(&self) -> Result<Option<String>> {
        Ok(self.conn.query_row("SELECT request_id FROM operations WHERE state IN ('queued','prepared') ORDER BY sequence LIMIT 1",[],|r|r.get(0)).optional()?)
    }
}

const STOPWORDS: &[&str] = &[
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does", "did", "what",
    "which", "who", "whom", "whose", "when", "where", "why", "how", "many", "much", "of", "in",
    "on", "at", "to", "for", "from", "by", "with", "and", "or", "that", "this", "these", "those",
    "it", "its", "has", "have", "had", "can", "could", "should", "would", "will", "me", "my", "i",
    "you", "your", "about", "tell", "show", "give", "find",
];

pub struct Index {
    pub conn: Connection,
    pub generation: u64,
}
impl Index {
    pub fn open(dir: &Path) -> Result<Self> {
        let conn = connection(&dir.join("index.sqlite"))?;
        let version: i64 = conn.pragma_query_value(None, "user_version", |r| r.get(0))?;
        if version != 5 {
            // Only derived data is replaced. Canonical files and operations are untouched.
            conn.execute_batch("BEGIN IMMEDIATE; DROP TABLE IF EXISTS evidence_fts; DROP TABLE IF EXISTS evidence_parts; DROP TABLE IF EXISTS evidence_fields; DROP TABLE IF EXISTS retrieval_facts; DROP TABLE IF EXISTS retrieval_aliases; DROP TABLE IF EXISTS retrieval_entities; DROP TABLE IF EXISTS retrieval_docs; DROP TABLE IF EXISTS concept_fts; DROP TABLE IF EXISTS links; DROP TABLE IF EXISTS concepts; PRAGMA user_version=5; COMMIT;")?;
        }
        conn.execute_batch("CREATE TABLE IF NOT EXISTS concepts(record_id INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE, revision TEXT NOT NULL, document TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL, description TEXT NOT NULL, tags TEXT NOT NULL, file_size INTEGER NOT NULL, file_mtime TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS concept_fts USING fts5(id UNINDEXED,title,description,tags,aliases,body,tokenize='unicode61 remove_diacritics 2');
            CREATE TABLE IF NOT EXISTS links(source TEXT NOT NULL,target TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',heading TEXT NOT NULL DEFAULT '',PRIMARY KEY(source,target)); CREATE INDEX IF NOT EXISTS links_target ON links(target);")?;
        crate::retrieval::schema(&conn)?;
        crate::evidence::schema(&conn)?;
        Ok(Self {
            conn,
            generation: 0,
        })
    }
    pub fn revision(&self, id: &str) -> Result<Option<String>> {
        Ok(self
            .conn
            .prepare_cached("SELECT revision FROM concepts WHERE id=?")?
            .query_row([id], |r| r.get(0))
            .optional()?)
    }
    pub fn get(&self, id: &str) -> Result<Value> {
        okf::validate_id(id)?;
        let raw: Option<String> = self
            .conn
            .prepare_cached("SELECT document FROM concepts WHERE id=?")?
            .query_row([id], |r| r.get(0))
            .optional()?;
        raw.map(|text| serde_json::from_str(&text).map_err(Into::into))
            .unwrap_or_else(|| Err(Error::new(404, "NOT_FOUND", "Concept not found.")))
    }
    fn remove(&self, id: &str) -> Result<()> {
        self.conn.execute("DELETE FROM evidence_fts WHERE rowid IN(SELECT part_id FROM evidence_parts WHERE concept_id=?)",[id])?;
        self.conn
            .prepare_cached(
                "DELETE FROM concept_fts WHERE rowid=(SELECT record_id FROM concepts WHERE id=?)",
            )?
            .execute([id])?;
        self.conn
            .prepare_cached("DELETE FROM links WHERE source=?")?
            .execute([id])?;
        self.conn
            .prepare_cached("DELETE FROM concepts WHERE id=?")?
            .execute([id])?;
        Ok(())
    }
    fn insert(&self, c: &Concept, path: &Path) -> Result<()> {
        let (size, mtime) = stamp(path)?;
        self.conn
            .prepare_cached("INSERT INTO concepts(id,revision,document,title,type,description,tags,file_size,file_mtime) VALUES(?,?,?,?,?,?,?,?,?)")?
            .execute(params![
                c.id,
                c.revision,
                serde_json::to_string(c)?,
                c.title,
                c.kind,
                c.description,
                json!(c.tags).to_string(),
                size as i64,
                mtime
            ])?;
        self.conn.prepare_cached("INSERT INTO concept_fts(rowid,id,title,description,tags,aliases,body) VALUES(?,?,?,?,?,?,?)")?.execute(params![self.conn.last_insert_rowid(),c.id,c.title,c.description,c.tags.join(" "),c.aliases.join(" "),c.body])?;
        for target in &c.links {
            self.conn
                .prepare_cached("INSERT INTO links(source,target) VALUES(?,?)")?
                .execute(params![c.id, target])?;
        }
        crate::retrieval::index_concept(&self.conn, c)?;
        crate::evidence::index(&self.conn, c)?;
        Ok(())
    }
    pub fn publish(&mut self, id: &str, concept: Option<&Concept>, path: &Path) -> Result<()> {
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = self
            .remove(id)
            .and_then(|_| concept.map_or(Ok(()), |c| self.insert(c, path)));
        match result {
            Ok(()) => self.conn.execute_batch("COMMIT")?,
            Err(e) => {
                self.conn.execute_batch("ROLLBACK")?;
                return Err(e);
            }
        }
        self.generation = self.generation.wrapping_add(1);
        Ok(())
    }
    pub fn reconcile(&mut self, bundle: &Path, force: bool) -> Result<()> {
        self.conn
            .execute_batch("CREATE TEMP TABLE scan_seen(id TEXT PRIMARY KEY); BEGIN IMMEDIATE;")?;
        let result = (|| {
            visit_files(bundle, bundle, &mut |id, path| {
                let (size, mtime) = stamp(path)?;
                let same: bool = self.conn.query_row("SELECT EXISTS(SELECT 1 FROM concepts WHERE id=? AND file_size=? AND file_mtime=?)",params![id,size as i64,mtime],|r|r.get(0))?;
                if force || !same {
                    let markdown = read_file(path)?.ok_or_else(|| {
                        Error::new(
                            503,
                            "EXTERNAL_EDIT",
                            "A concept disappeared during startup.",
                        )
                    })?;
                    let concept = okf::parse(id, &markdown)?;
                    self.remove(id)?;
                    self.insert(&concept, path)?;
                }
                self.conn.execute("INSERT INTO scan_seen VALUES(?)", [id])?;
                Ok(())
            })?;
            self.conn.execute_batch("DELETE FROM concept_fts WHERE id NOT IN (SELECT id FROM scan_seen); DELETE FROM links WHERE source NOT IN (SELECT id FROM scan_seen); DELETE FROM concepts WHERE id NOT IN (SELECT id FROM scan_seen);")?;
            Ok(())
        })();
        match result {
            Ok(()) => self.conn.execute_batch("COMMIT; DROP TABLE scan_seen;")?,
            Err(e) => {
                self.conn.execute_batch("ROLLBACK; DROP TABLE scan_seen;")?;
                return Err(e);
            }
        }
        Ok(())
    }
    pub fn count(&self) -> Result<usize> {
        Ok(self
            .conn
            .query_row("SELECT count(*) FROM concepts", [], |r| r.get::<_, i64>(0))?
            as usize)
    }
    pub fn list(&self, limit: usize, offset: usize) -> Result<Value> {
        let mut statement=self.conn.prepare_cached("SELECT id,revision,title,type,description,tags FROM concepts ORDER BY id LIMIT ? OFFSET ?")?;
        let rows=statement.query_map(params![limit as i64,offset as i64],|r|Ok(json!({"id":r.get::<_,String>(0)?,"revision":r.get::<_,String>(1)?,"title":r.get::<_,String>(2)?,"type":r.get::<_,String>(3)?,"description":r.get::<_,String>(4)?,"tags":serde_json::from_str::<Value>(&r.get::<_,String>(5)?).unwrap_or(Value::Null)})))?;
        Ok(json!({"concepts":rows.collect::<rusqlite::Result<Vec<_>>>()?}))
    }
    pub fn search(&self, query: &str, limit: usize) -> Result<Value> {
        let all: Vec<_> = query
            .split(|c: char| !c.is_alphanumeric() && c != '_')
            .filter(|s| !s.is_empty())
            .take(32)
            .collect();
        if all.is_empty() {
            return Err(Error::new(
                400,
                "INVALID_QUERY",
                "Search needs at least one word.",
            ));
        }
        // Drop question filler so "How many points did Liverpool earn" can match; keep
        // the original words when nothing else is left.
        let content: Vec<_> = all
            .iter()
            .copied()
            .filter(|t| !STOPWORDS.contains(&t.to_lowercase().as_str()))
            .collect();
        let tokens = if content.is_empty() { all } else { content };
        let mut results = self.run_search(&tokens, " AND ", limit)?;
        // ponytail: OR fallback only fills a short AND result; add typo/synonym handling if misses persist.
        if results.len() < limit.min(3) && tokens.len() > 1 {
            for hit in self.run_search(&tokens, " OR ", limit)? {
                if results.len() >= limit {
                    break;
                }
                if !results.iter().any(|r| r["id"] == hit["id"]) {
                    results.push(hit);
                }
            }
        }
        Ok(json!({"results":results}))
    }
    fn run_search(&self, tokens: &[&str], joiner: &str, limit: usize) -> Result<Vec<Value>> {
        let query = tokens
            .iter()
            .map(|t| format!("\"{t}\"*"))
            .collect::<Vec<_>>()
            .join(joiner);
        let mut statement=self.conn.prepare_cached("SELECT c.id,c.revision,c.title,c.type,c.description,snippet(concept_fts,5,'','',' … ',32),bm25(concept_fts,0,8,4,3,6,1) AS score FROM concept_fts JOIN concepts c ON c.record_id=concept_fts.rowid WHERE concept_fts MATCH ? ORDER BY score,c.id LIMIT ?")?;
        let rows=statement.query_map(params![query,limit as i64],|r|Ok(json!({"id":r.get::<_,String>(0)?,"revision":r.get::<_,String>(1)?,"title":r.get::<_,String>(2)?,"type":r.get::<_,String>(3)?,"description":r.get::<_,String>(4)?,"snippet":r.get::<_,String>(5)?,"score":r.get::<_,f64>(6)?})))?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn links(&self, id: &str) -> Result<Value> {
        okf::validate_id(id)?;
        let mut outgoing=self.conn.prepare_cached("SELECT l.target,c.title,c.revision,CASE WHEN c.id IS NULL THEN 0 ELSE 1 END FROM links l LEFT JOIN concepts c ON c.id=l.target WHERE l.source=? ORDER BY l.target")?;
        let outgoing=outgoing.query_map([id],|r|Ok(json!({"id":r.get::<_,String>(0)?,"title":r.get::<_,Option<String>>(1)?,"revision":r.get::<_,Option<String>>(2)?,"resolved":r.get::<_,i64>(3)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
        let mut incoming=self.conn.prepare_cached("SELECT c.id,c.title,c.revision FROM links l JOIN concepts c ON c.id=l.source WHERE l.target=? ORDER BY c.id")?;
        let incoming=incoming.query_map([id],|r|Ok(json!({"id":r.get::<_,String>(0)?,"title":r.get::<_,String>(1)?,"revision":r.get::<_,String>(2)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(json!({"outgoing":outgoing,"incoming":incoming}))
    }
}

pub fn stamp(path: &Path) -> Result<(u64, String)> {
    let m = fs::metadata(path)?;
    Ok((
        m.len(),
        m.modified()?
            .duration_since(UNIX_EPOCH)
            .map_err(|e| Error::new(503, "STORAGE_ERROR", e.to_string()))?
            .as_nanos()
            .to_string(),
    ))
}

pub fn concept_path(bundle: &Path, id: &str) -> Result<PathBuf> {
    okf::validate_id(id)?;
    let path = bundle.join(format!("{id}.md"));
    let mut cursor = bundle.to_path_buf();
    for component in path.strip_prefix(bundle).unwrap().components() {
        cursor.push(component);
        if fs::symlink_metadata(&cursor).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err(Error::new(
                503,
                "UNSAFE_STORAGE",
                "Symlinks inside the bundle are not supported.",
            ));
        }
    }
    Ok(path)
}
pub fn read_file(path: &Path) -> Result<Option<String>> {
    let file = match File::open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let size = file.metadata()?.len();
    let oversized = || Error::new(422, "INVALID_OKF", "Concept files are limited to 1 MB.");
    if size > okf::MAX_CONCEPT as u64 {
        return Err(oversized());
    }
    let mut text = String::with_capacity(size as usize);
    // Bound the read too, in case a file grows after the metadata check.
    file.take((okf::MAX_CONCEPT + 1) as u64)
        .read_to_string(&mut text)?;
    if text.len() > okf::MAX_CONCEPT {
        return Err(oversized());
    }
    Ok(Some(text))
}
pub fn sync_dir(path: &Path) -> Result<()> {
    File::open(path)?.sync_all()?;
    Ok(())
}
pub fn ensure_dir(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(m) if m.is_dir() && !m.file_type().is_symlink() => Ok(()),
        Ok(_) => Err(Error::new(
            503,
            "UNSAFE_STORAGE",
            "Storage directories cannot be symlinks.",
        )),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let parent = path.parent().unwrap();
            ensure_dir(parent)?;
            fs::create_dir(path)?;
            sync_dir(parent)
        }
        Err(e) => Err(e.into()),
    }
}
pub fn write_file(path: &Path, markdown: &str, sequence: i64) -> Result<()> {
    ensure_dir(path.parent().unwrap())?;
    let nonce = std::time::SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let tmp = path.parent().unwrap().join(format!(
        ".jbrain-{}-{sequence}-{nonce}.tmp",
        std::process::id()
    ));
    let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
    file.write_all(markdown.as_bytes())?;
    file.sync_all()?;
    drop(file);
    fs::rename(&tmp, path)?;
    sync_dir(path.parent().unwrap())
}
fn visit_files(
    root: &Path,
    dir: &Path,
    visit: &mut impl FnMut(&str, &Path) -> Result<()>,
) -> Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with('.') {
            continue;
        }
        let kind = entry.file_type()?;
        let path = entry.path();
        if kind.is_symlink() {
            return Err(Error::new(
                503,
                "UNSAFE_STORAGE",
                "Bundle contains a symlink.",
            ));
        }
        if kind.is_dir() {
            visit_files(root, &path, visit)?;
        } else if name.ends_with(".md") && name != "index.md" && name != "log.md" {
            let relative = path.strip_prefix(root).unwrap().to_string_lossy();
            let id = relative.strip_suffix(".md").unwrap();
            okf::validate_id(id)?;
            visit(id, &path)?;
        }
    }
    Ok(())
}
