use crate::{
    error::{Error, Result},
    okf,
    storage::{self, Index, Operation, Operations},
};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, oneshot};

pub struct Config {
    pub data_dir: PathBuf,
    pub history: bool,
    pub reindex: bool,
    pub max_pending: usize,
    pub max_pending_bytes: usize,
}

struct Shared {
    bundle: PathBuf,
    operations: Mutex<Operations>,
    index: Mutex<Index>,
    failed: AtomicBool,
    closing: AtomicBool,
    history: History,
    _owner: File,
    _legacy_owner: Mutex<Option<rusqlite::Connection>>,
}

enum Work {
    Apply(String, oneshot::Sender<Result<Value>>, OwnedSemaphorePermit),
    Stop,
}

pub struct Brain {
    pub ask: crate::ask::Workflow,
    shared: Arc<Shared>,
    sender: mpsc::Sender<Work>,
    worker: Mutex<Option<thread::JoinHandle<()>>>,
    waiters: Arc<Semaphore>,
    max_pending: usize,
    max_pending_bytes: usize,
}

impl Brain {
    pub fn open(config: Config) -> Result<Arc<Self>> {
        let ask = crate::ask::Workflow::from_env()?;
        fs::create_dir_all(&config.data_dir)?;
        let data_dir = fs::canonicalize(&config.data_dir)?;
        let owner = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(data_dir.join("owner.lock"))?;
        owner.try_lock().map_err(|_| {
            Error::new(
                503,
                "OWNER_RUNNING",
                "This brain already has an owning service. Connect agents through HTTP or MCP.",
            )
        })?;
        let legacy_owner = if data_dir.join("owner.sqlite").exists() {
            let conn = rusqlite::Connection::open(data_dir.join("owner.sqlite"))?;
            conn.execute_batch("PRAGMA busy_timeout=0; BEGIN EXCLUSIVE;")
                .map_err(|_| {
                    Error::new(
                        503,
                        "OWNER_RUNNING",
                        "The previous Bun service is still running. Stop it before starting Rust.",
                    )
                })?;
            Some(conn)
        } else {
            None
        };
        let bundle = data_dir.join("knowledge");
        storage::ensure_dir(&bundle)?;
        let operations = Operations::open(&data_dir)?;
        let mut index = Index::open(&data_dir)?;
        let history = History::new(&bundle, config.history)?;
        // Walk one file at a time; unchanged documents retain their parsed index.
        index.reconcile(&bundle, config.reindex)?;
        let shared = Arc::new(Shared {
            bundle,
            operations: Mutex::new(operations),
            index: Mutex::new(index),
            failed: AtomicBool::new(false),
            closing: AtomicBool::new(false),
            history,
            _owner: owner,
            _legacy_owner: Mutex::new(legacy_owner),
        });
        loop {
            let next = shared.operations.lock().unwrap().next_pending()?;
            let Some(id) = next else { break };
            shared.apply(&id)?;
        }
        shared.history.snapshot()?;
        let (sender, receiver) = mpsc::channel();
        let state = shared.clone();
        let worker = thread::Builder::new()
            .name("jbrain-writer".into())
            .stack_size(512 * 1024)
            .spawn(move || {
                while let Ok(work) = receiver.recv() {
                    let Work::Apply(id, reply, _permit) = work else {
                        break;
                    };
                    let result = if state.failed.load(Ordering::Acquire) {
                        Err(unavailable())
                    } else {
                        state.apply(&id)
                    };
                    if let Err(error) = &result {
                        state.failed.store(true, Ordering::Release);
                        eprintln!("Storage paused; accepted requests are retained: {error}");
                    }
                    let _ = reply.send(result.map_err(|_| unavailable()));
                }
            })?;
        Ok(Arc::new(Self {
            ask,
            shared,
            sender,
            worker: Mutex::new(Some(worker)),
            waiters: Arc::new(Semaphore::new(128)),
            max_pending: config.max_pending,
            max_pending_bytes: config.max_pending_bytes,
        }))
    }

    pub fn check(&self) -> Result<()> {
        if self.shared.failed.load(Ordering::Acquire) || self.shared.closing.load(Ordering::Acquire)
        {
            return Err(unavailable());
        }
        Ok(())
    }
    pub fn health(&self) -> Result<Value> {
        Ok(
            json!({"status":if self.shared.failed.load(Ordering::Acquire){"recovery_required"}else if self.shared.closing.load(Ordering::Acquire){"closing"}else{"ready"},"concepts":self.shared.index.lock().unwrap().count()?,"pendingWrites":self.shared.operations.lock().unwrap().pending_count()?,"runtime":"rust","history":if self.shared.history.enabled(){"git"}else{"journal"}}),
        )
    }
    pub fn get(&self, id: &str) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().get(id)
    }
    pub fn list(&self, limit: usize, offset: usize) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().list(limit, offset)
    }
    pub fn search(&self, query: &str, limit: usize) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().search(query, limit)
    }
    pub fn links(&self, id: &str) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().links(id)
    }
    pub fn query(&self, request: crate::retrieval::QueryRequest) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().query(request)
    }
    pub fn evidence(&self, tool: &str, args: Value) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().evidence(tool, args)
    }
    pub fn evidence_snapshot(&self, tool: &str, args: Value) -> Result<(Value, u64)> {
        self.check()?;
        let index = self.shared.index.lock().unwrap();
        Ok((index.evidence(tool, args)?, index.generation))
    }
    pub fn generation(&self) -> Result<u64> {
        self.check()?;
        Ok(self.shared.index.lock().unwrap().generation)
    }
    pub fn check_revisions(
        &self,
        revisions: &std::collections::BTreeMap<String, String>,
    ) -> Result<bool> {
        self.check()?;
        let index = self.shared.index.lock().unwrap();
        for (id, revision) in revisions {
            if index.revision(id)?.as_ref() != Some(revision) {
                return Ok(false);
            }
        }
        Ok(true)
    }
    pub fn browse(&self, request: crate::retrieval::BrowseRequest) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().browse(request)
    }
    pub fn neighbors(&self, request: crate::retrieval::NeighborRequest) -> Result<Value> {
        self.check()?;
        self.shared.index.lock().unwrap().neighbors(request)
    }
    pub fn operation(&self, id: &str) -> Result<Value> {
        let operation = self
            .shared
            .operations
            .lock()
            .unwrap()
            .get(id)?
            .ok_or_else(|| Error::new(404, "NOT_FOUND", "Request ID not found."))?;
        Ok(serde_json::to_value(operation)?)
    }
    pub async fn write(&self, value: Value) -> Result<(u16, Value)> {
        self.check()?;
        let permit = self.waiters.clone().try_acquire_owned().map_err(|_| {
            Error::new(
                503,
                "QUEUE_FULL",
                "Too many waiting requests. Retry the same ID and payload.",
            )
        })?;
        let input = storage::validate_write(value)?;
        let operation = {
            let operations = self.shared.operations.lock().unwrap();
            if operations.get(&input.request_id)?.is_none() {
                let bytes:i64=operations.conn.query_row("SELECT coalesce(sum(length(CAST(input AS BLOB))),0) FROM operations WHERE state IN ('queued','prepared')",[],|r|r.get(0))?;
                if bytes as usize + serde_json::to_string(&input)?.len() > self.max_pending_bytes {
                    return Err(Error::new(
                        503,
                        "QUEUE_FULL",
                        "Pending write byte budget is full. Retry the same ID and payload.",
                    ));
                }
            }
            operations.accept(&input, self.max_pending)?.0
        };
        drop(input);
        if let Some(status) = operation.status {
            return Ok((status, operation.result));
        }
        let (reply, receiver) = oneshot::channel();
        self.sender
            .send(Work::Apply(operation.request_id, reply, permit))
            .map_err(|_| unavailable())?;
        drop(operation.input);
        // Work is owned by the writer, so disconnecting this waiter never cancels it.
        let result = receiver.await.map_err(|_| unavailable())??;
        let status = result
            .get("error")
            .and_then(|e| e.get("code"))
            .map_or(200, |code| {
                if code == "REVISION_CONFLICT" {
                    409
                } else {
                    422
                }
            });
        Ok((status, result))
    }
    pub fn stop_accepting(&self) {
        self.shared.closing.store(true, Ordering::Release);
    }
    pub fn shutdown(&self) {
        self.stop_accepting();
        let _ = self.sender.send(Work::Stop);
        if let Some(worker) = self.worker.lock().unwrap().take() {
            let _ = worker.join();
        }
    }
}

fn unavailable() -> Error {
    Error::new(
        503,
        "RECOVERY_REQUIRED",
        "Service is unavailable. Accepted work is durable; restart and retry with the same ID and payload.",
    )
}

impl Shared {
    fn apply(&self, id: &str) -> Result<Value> {
        let mut op = self
            .operations
            .lock()
            .unwrap()
            .get(id)?
            .ok_or_else(|| Error::new(503, "STORAGE_ERROR", "Journal request missing."))?;
        if op.status.is_some() {
            return Ok(op.result);
        }
        checkpoint("accepted", &op, &self.bundle);
        let input = &op.input;
        // Invalid new knowledge is a terminal request error, not a storage outage.
        let concept = if input.action == "put" {
            match okf::parse(&input.id, input.markdown.as_deref().unwrap()) {
                Ok(c) => Some(c),
                Err(error) => {
                    self.operations
                        .lock()
                        .unwrap()
                        .finish(id, error.status, &error.body())?;
                    return Ok(error.body());
                }
            }
        } else {
            None
        };
        let path = storage::concept_path(&self.bundle, &input.id)?;
        if op.state == "queued" {
            let current = self.index.lock().unwrap().revision(&input.id)?;
            if current != input.expected_revision {
                let mut error = Error::new(
                    409,
                    "REVISION_CONFLICT",
                    "Concept changed. Read, merge, and submit a new request ID with the latest revision.",
                );
                error.details = json!({"id":input.id,"expectedRevision":input.expected_revision,"currentRevision":current});
                self.operations
                    .lock()
                    .unwrap()
                    .finish(id, 409, &error.body())?;
                return Ok(error.body());
            }
            let disk = storage::read_file(&path)?;
            if disk.as_deref().map(okf::revision) != current {
                return Err(Error::new(
                    503,
                    "EXTERNAL_EDIT",
                    "Knowledge changed outside the service. Restart to import offline edits.",
                ));
            }
            self.operations.lock().unwrap().prepare(id)?;
            op.state = "prepared".into();
            checkpoint("prepared", &op, &self.bundle);
        }
        let input = &op.input;
        let disk = storage::read_file(&path)?;
        let disk_revision = disk.as_deref().map(okf::revision);
        let new_revision = concept.as_ref().map(|c| c.revision.clone());
        if disk_revision != input.expected_revision && disk_revision != new_revision {
            return Err(Error::new(
                503,
                "EXTERNAL_EDIT",
                "Cannot replay a prepared write over unexpected file contents.",
            ));
        }
        if let Some(c) = &concept {
            storage::write_file(&path, &c.markdown, op.sequence)?;
        } else if disk.is_some() {
            fs::remove_file(&path)?;
            storage::sync_dir(path.parent().unwrap())?;
        }
        checkpoint("file", &op, &self.bundle);
        let git_commit = self
            .history
            .commit(&input.id, &input.request_id, &input.actor)?;
        checkpoint("git", &op, &self.bundle);
        self.index
            .lock()
            .unwrap()
            .publish(&input.id, concept.as_ref(), &path)?;
        checkpoint("index", &op, &self.bundle);
        let result = json!({"requestId":id,"sequence":op.sequence,"id":input.id,"action":input.action,"actor":input.actor,"revision":new_revision,"gitCommit":git_commit});
        self.operations.lock().unwrap().finish(id, 200, &result)?;
        Ok(result)
    }
}

fn checkpoint(_phase: &str, _op: &Operation, _bundle: &Path) {
    // Failure injection is compiled out of release builds and has no HTTP control.
    #[cfg(debug_assertions)]
    if _op.request_id == "crash-request"
        && std::env::var("JBRAIN_TEST_PAUSE_PHASE").ok().as_deref() == Some(_phase)
    {
        println!("{}", json!({"event":"checkpoint","phase":_phase}));
        let resume = _bundle.parent().unwrap().join(".resume");
        while !resume.exists() {
            thread::sleep(std::time::Duration::from_millis(5));
        }
        let _ = fs::remove_file(resume);
    }
}

struct History {
    bundle: Option<PathBuf>,
}
impl History {
    fn new(bundle: &Path, enabled: bool) -> Result<Self> {
        let history = Self {
            bundle: enabled.then(|| bundle.to_path_buf()),
        };
        if enabled {
            if !bundle.join(".git").exists() {
                history.run(&["init", "--quiet"], &[0])?;
            }
            if fs::symlink_metadata(bundle.join(".git"))?
                .file_type()
                .is_symlink()
            {
                return Err(Error::new(
                    503,
                    "UNSAFE_STORAGE",
                    "Git metadata cannot be a symlink.",
                ));
            }
            let root = history.run(&["rev-parse", "--show-toplevel"], &[0])?.0;
            if fs::canonicalize(root.trim())? != fs::canonicalize(bundle)? {
                return Err(Error::new(
                    503,
                    "UNSAFE_STORAGE",
                    "Knowledge needs its own Git repository.",
                ));
            }
        }
        Ok(history)
    }
    fn enabled(&self) -> bool {
        self.bundle.is_some()
    }
    fn run(&self, args: &[&str], allowed: &[i32]) -> Result<(String, i32)> {
        let output = Command::new("git")
            .args([
                "--literal-pathspecs",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=JBrain",
                "-c",
                "user.email=jbrain@localhost",
                "-C",
            ])
            .arg(self.bundle.as_ref().unwrap())
            .args(args)
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .output()?;
        let code = output.status.code().unwrap_or(-1);
        if !allowed.contains(&code) {
            return Err(Error::new(
                503,
                "GIT_ERROR",
                String::from_utf8_lossy(&output.stderr).trim(),
            ));
        }
        Ok((
            String::from_utf8_lossy(&output.stdout).trim().to_owned(),
            code,
        ))
    }
    fn commit(&self, id: &str, request_id: &str, actor: &str) -> Result<Option<String>> {
        let Some(bundle) = &self.bundle else {
            return Ok(None);
        };
        let file = format!("{id}.md");
        if bundle.join(&file).exists()
            || self
                .run(&["ls-files", "--error-unmatch", "--", &file], &[0, 1])?
                .1
                == 0
        {
            self.run(&["add", "--all", "--", &file], &[0])?;
        }
        if self.run(&["diff", "--cached", "--quiet"], &[0, 1])?.1 == 1 {
            self.run(
                &[
                    "commit",
                    "--quiet",
                    "-m",
                    &format!("Update {id}"),
                    "-m",
                    &format!("Request: {request_id}\nActor: {actor}"),
                ],
                &[0],
            )?;
        }
        let (head, code) = self.run(&["rev-parse", "--verify", "HEAD"], &[0, 128])?;
        Ok((code == 0).then_some(head))
    }
    fn snapshot(&self) -> Result<()> {
        if !self.enabled() {
            return Ok(());
        }
        self.run(&["add", "--all"], &[0])?;
        if self.run(&["diff", "--cached", "--quiet"], &[0, 1])?.1 == 1 {
            self.run(
                &["commit", "--quiet", "-m", "Import existing OKF bundle"],
                &[0],
            )?;
        }
        Ok(())
    }
}
