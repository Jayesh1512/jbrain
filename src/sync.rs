//! Optional background push of the knowledge repository to a Git remote.
//! Pushes never block or fail a write: failures are retried with backoff and shown in `/health`.
use crate::error::{Error, Result};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    process::Command,
    sync::{Arc, Condvar, Mutex},
    thread,
    time::Duration,
};

pub struct Pusher {
    bundle: PathBuf,
    remote: String,
    token: Option<String>,
    state: Mutex<State>,
    wake: Condvar,
}

struct State {
    dirty: bool,
    pushed: Option<String>,
    error: Option<String>,
}

impl Pusher {
    /// Points `origin` at `remote` and starts the push thread, which first catches up on any
    /// unpushed history. The token is sent as an HTTP header for https remotes only; it is never
    /// written to the repository's configuration or passed on a command line.
    pub fn start(bundle: PathBuf, remote: String, token: Option<String>) -> Result<Arc<Self>> {
        if remote.starts_with('-') || remote.contains(char::is_whitespace) {
            return Err(Error::new(
                400,
                "CONFIG_ERROR",
                "JBRAIN_GIT_REMOTE must be a Git URL or path.",
            ));
        }
        let pusher = Arc::new(Self {
            bundle,
            remote,
            token,
            state: Mutex::new(State {
                dirty: true,
                pushed: None,
                error: None,
            }),
            wake: Condvar::new(),
        });
        let (_, code) = pusher.git(&["remote", "get-url", "origin"], false)?;
        let verb = if code == 0 { "set-url" } else { "add" };
        pusher.git(&["remote", verb, "origin", "--", &pusher.remote], false)?;
        let worker = pusher.clone();
        thread::Builder::new()
            .name("jbrain-push".into())
            .spawn(move || worker.run())?;
        Ok(pusher)
    }

    /// Request a push after a commit; bursts of writes coalesce into one push.
    pub fn notify(&self) {
        self.state.lock().unwrap().dirty = true;
        self.wake.notify_one();
    }

    pub fn status(&self) -> Value {
        let s = self.state.lock().unwrap();
        json!({"remote":redact(&self.remote),"branch":"main","pending":s.dirty,"pushedCommit":s.pushed,"lastError":s.error})
    }

    fn run(&self) {
        let mut delay = Duration::from_secs(2);
        loop {
            {
                let mut s = self.state.lock().unwrap();
                while !s.dirty {
                    s = self.wake.wait(s).unwrap();
                }
                s.dirty = false;
            }
            match self.push() {
                Ok(head) => {
                    let mut s = self.state.lock().unwrap();
                    if head.is_some() {
                        s.pushed = head;
                    }
                    s.error = None;
                    delay = Duration::from_secs(2);
                }
                Err(error) => {
                    let message = self.scrub(error.message);
                    eprintln!("Git push failed; will retry: {message}");
                    let mut s = self.state.lock().unwrap();
                    s.error = Some(message);
                    s.dirty = true;
                    drop(s);
                    thread::sleep(delay);
                    delay = (delay * 2).min(Duration::from_secs(300));
                }
            }
        }
    }

    fn push(&self) -> Result<Option<String>> {
        let (head, code) = self.git(&["rev-parse", "--verify", "HEAD"], false)?;
        if code != 0 {
            return Ok(None); // nothing committed yet
        }
        self.git(&["push", "--quiet", "origin", "HEAD:refs/heads/main"], true)?;
        Ok(Some(head))
    }

    /// Runs git. For a push (`push == true`) a nonzero exit is an error and credentials are
    /// attached; otherwise the exit code is returned.
    fn git(&self, args: &[&str], push: bool) -> Result<(String, i32)> {
        let mut command = Command::new("git");
        command
            .args([
                "--literal-pathspecs",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
            ])
            .arg(&self.bundle)
            .args(args)
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_HTTP_LOW_SPEED_LIMIT", "1000")
            .env("GIT_HTTP_LOW_SPEED_TIME", "60")
            .env(
                "GIT_SSH_COMMAND",
                "ssh -o BatchMode=yes -o ConnectTimeout=15",
            );
        if let (true, Some(token), Some(origin)) = (push, &self.token, https_origin(&self.remote)) {
            command
                .env("GIT_CONFIG_COUNT", "1")
                .env("GIT_CONFIG_KEY_0", format!("http.{origin}.extraHeader"))
                .env(
                    "GIT_CONFIG_VALUE_0",
                    format!(
                        "Authorization: Basic {}",
                        base64(format!("x-access-token:{token}").as_bytes())
                    ),
                );
        }
        let output = command.output()?;
        let code = output.status.code().unwrap_or(-1);
        let text = |b: &[u8]| String::from_utf8_lossy(b).trim().to_owned();
        if code != 0 && push {
            return Err(Error::new(503, "GIT_PUSH_ERROR", text(&output.stderr)));
        }
        Ok((text(&output.stdout), code))
    }

    fn scrub(&self, message: String) -> String {
        let message = self
            .token
            .as_ref()
            .map_or(message.clone(), |t| message.replace(t, "***"));
        message.replace(&self.remote, &redact(&self.remote))
    }
}

/// `https://host/` for an https remote, used to scope the auth header to that host only.
fn https_origin(remote: &str) -> Option<String> {
    let rest = remote.strip_prefix("https://")?;
    let host = rest.split('/').next()?.rsplit('@').next()?;
    Some(format!("https://{host}/"))
}

/// Remove any `user:password@` from a URL before it is logged or reported.
fn redact(remote: &str) -> String {
    match remote.split_once("://") {
        Some((scheme, rest)) => {
            let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
            let host = authority.rsplit('@').next().unwrap_or(authority);
            format!("{scheme}://{host}/{path}")
        }
        None => remote.to_owned(),
    }
}

fn base64(bytes: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |a, (i, b)| a | (*b as u32) << (16 - 8 * i));
        for i in 0..4 {
            out.push(if i <= chunk.len() {
                T[(n >> (18 - 6 * i) & 63) as usize] as char
            } else {
                '='
            });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_and_redacts() {
        assert_eq!(base64(b"x-access-token:abc"), "eC1hY2Nlc3MtdG9rZW46YWJj");
        assert_eq!(base64(b"ab"), "YWI=");
        assert_eq!(base64(b"a"), "YQ==");
        assert_eq!(
            redact("https://u:p@github.com/o/r.git"),
            "https://github.com/o/r.git"
        );
        assert_eq!(redact("/tmp/bare.git"), "/tmp/bare.git");
        assert_eq!(
            https_origin("https://u:p@github.com/o/r.git").as_deref(),
            Some("https://github.com/")
        );
        assert_eq!(https_origin("git@github.com:o/r.git"), None);
    }
}
