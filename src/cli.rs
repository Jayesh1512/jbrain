use crate::{
    api,
    brain::{Brain, Config},
    error::{Error, Result},
    mcp::{Backend, Mcp},
};
use rmcp::ServiceExt;
use serde_json::{Value, json};
use std::{collections::HashMap, env, fs, path::PathBuf};

pub fn encode(value: &str) -> String {
    percent_encoding::utf8_percent_encode(value, percent_encoding::NON_ALPHANUMERIC).to_string()
}

#[derive(Clone)]
pub struct Client {
    url: String,
    token: Option<String>,
    agent: ureq::Agent,
}
impl Client {
    pub fn from_env(url: Option<String>) -> Self {
        Self {
            url: url.unwrap_or_else(|| {
                env::var("JBRAIN_URL").unwrap_or_else(|_| "http://127.0.0.1:7331".into())
            }),
            token: env::var("JBRAIN_TOKEN").ok().filter(|s| !s.is_empty()),
            agent: ureq::AgentBuilder::new().build(),
        }
    }
    pub fn request(&self, path: &str, payload: Option<Value>) -> Result<Value> {
        let url = format!("{}{path}", self.url.trim_end_matches('/'));
        let mut req = self
            .agent
            .request(if payload.is_some() { "POST" } else { "GET" }, &url);
        if let Some(token) = &self.token {
            req = req.set("Authorization", &format!("Bearer {token}"));
        }
        let response = match payload {
            Some(value) => req.send_json(value),
            None => req.call(),
        };
        match response {
            Ok(response) => response.into_json().map_err(Into::into),
            Err(ureq::Error::Status(status, response)) => {
                let value: Value = response.into_json()?;
                let mut error = Error::new(
                    status,
                    value["error"]["code"].as_str().unwrap_or("SERVICE_ERROR"),
                    value["error"]["message"]
                        .as_str()
                        .unwrap_or("Service request failed."),
                );
                error.details = value["error"]["details"].clone();
                Err(error)
            }
            Err(error) => Err(Error::new(503, "SERVICE_UNREACHABLE", error.to_string())),
        }
    }
}

pub async fn run() -> Result<()> {
    let args: Vec<String> = env::args().skip(1).collect();
    let command = args.first().map(String::as_str).unwrap_or("serve");
    if matches!(command, "--help" | "-h" | "help") {
        print_help();
        return Ok(());
    }
    if command == "serve" {
        return serve(args.iter().any(|a| a == "--reindex")).await;
    }
    let (positionals, options) = arguments(&args[1..])?;
    let client = Client::from_env(options.get("url").cloned());
    if command == "mcp" {
        let running = Mcp {
            backend: Backend::Remote(client),
        }
        .serve(rmcp::transport::stdio())
        .await
        .map_err(|e| Error::new(503, "MCP_ERROR", e.to_string()))?;
        running
            .waiting()
            .await
            .map_err(|e| Error::new(503, "MCP_ERROR", e.to_string()))?;
        return Ok(());
    }
    let argument = || {
        positionals
            .first()
            .map(String::as_str)
            .ok_or_else(|| Error::new(400, "CLI_ARGUMENT", "A concept ID or query is required."))
    };
    let option = |name: &str| {
        options
            .get(name)
            .cloned()
            .ok_or_else(|| Error::new(400, "CLI_ARGUMENT", format!("--{name} is required.")))
    };
    let result = match command {
        "health" => client.request("/health", None)?,
        "get" => client.request(&format!("/v1/concept?id={}", encode(argument()?)), None)?,
        "links" => client.request(&format!("/v1/links?id={}", encode(argument()?)), None)?,
        "list" => client.request(
            &format!(
                "/v1/concepts?limit={}&offset={}",
                options.get("limit").map(String::as_str).unwrap_or("50"),
                options.get("offset").map(String::as_str).unwrap_or("0")
            ),
            None,
        )?,
        "search" => client.request(
            &format!(
                "/v1/search?q={}&limit={}",
                encode(argument()?),
                options.get("limit").map(String::as_str).unwrap_or("20")
            ),
            None,
        )?,
        "query" => {
            let mut input = json!({"question":argument()?});
            for (flag, key) in [("limit", "limit"), ("chars", "max_chars")] {
                if let Some(value) = options.get(flag) {
                    input[key] = json!(value.parse::<usize>().map_err(|_| Error::new(
                        400,
                        "CLI_ARGUMENT",
                        "Invalid numeric option."
                    ))?);
                }
            }
            if let Some(path) = options.get("plan") {
                input["plan"] = serde_json::from_str(&fs::read_to_string(path)?)?;
            }
            client.request("/v1/query", Some(input))?
        }
        "ask" => client.request("/v1/ask",Some(json!({"question":argument()?})))?,
        "evidence" => client.request("/v1/evidence",Some(json!({"tool":argument()?,"arguments":serde_json::from_str::<Value>(&fs::read_to_string(option("file")?)?)?})))?,
        "browse" | "neighbors" => {
            let mut fields = Vec::new();
            if command == "neighbors" {
                fields.push(format!("id={}", encode(argument()?)));
            }
            for key in [
                "prefix",
                "kind",
                "period",
                "entity",
                "direction",
                "limit",
                "offset",
            ] {
                if let Some(value) = options.get(key) {
                    fields.push(format!("{key}={}", encode(value)));
                }
            }
            client.request(&format!("/v1/{command}?{}", fields.join("&")), None)?
        }
        "operation" => client.request(&format!("/v1/operations/{}", encode(argument()?)), None)?,
        "put" | "delete" => {
            let create = options.contains_key("create");
            if create && (options.contains_key("revision") || command == "delete") {
                return Err(Error::new(
                    400,
                    "CLI_ARGUMENT",
                    "Use --create only for creation; updates/deletes require --revision.",
                ));
            }
            let revision = if create {
                Value::Null
            } else {
                Value::String(option("revision")?)
            };
            let mut payload = json!({"requestId":option("request-id")?,"actor":option("actor")?,"id":argument()?,"action":command,"expectedRevision":revision});
            if command == "put" {
                payload["markdown"] = Value::String(fs::read_to_string(option("file")?)?);
            }
            client.request("/v1/operations", Some(payload))?
        }
        _ => {
            return Err(Error::new(
                400,
                "CLI_ARGUMENT",
                format!("Unknown command: {command}"),
            ));
        }
    };
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

fn arguments(args: &[String]) -> Result<(Vec<String>, HashMap<String, String>)> {
    let (mut positionals, mut options) = (Vec::new(), HashMap::new());
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        if let Some(name) = arg.strip_prefix("--") {
            if name == "create" {
                options.insert(name.into(), String::new());
            } else if [
                "url",
                "actor",
                "request-id",
                "revision",
                "file",
                "limit",
                "offset",
                "chars",
                "plan",
                "prefix",
                "kind",
                "period",
                "entity",
                "direction",
            ]
            .contains(&name)
            {
                options.insert(
                    name.into(),
                    iter.next()
                        .ok_or_else(|| {
                            Error::new(400, "CLI_ARGUMENT", format!("--{name} needs a value."))
                        })?
                        .clone(),
                );
            } else {
                return Err(Error::new(
                    400,
                    "CLI_ARGUMENT",
                    format!("Unknown option: {arg}"),
                ));
            }
        } else {
            positionals.push(arg.clone());
        }
    }
    Ok((positionals, options))
}
fn positive(name: &str, default: usize) -> Result<usize> {
    match env::var(name) {
        Err(_) => Ok(default),
        Ok(v) => v.parse().ok().filter(|n| *n > 0).ok_or_else(|| {
            Error::new(
                400,
                "CONFIG_ERROR",
                format!("{name} must be a positive integer."),
            )
        }),
    }
}

async fn serve(reindex: bool) -> Result<()> {
    let host = env::var("JBRAIN_HOST").unwrap_or_else(|_| "127.0.0.1".into());
    let port = env::var("JBRAIN_PORT")
        .unwrap_or_else(|_| "7331".into())
        .parse::<u16>()
        .map_err(|_| Error::new(400, "CONFIG_ERROR", "Invalid JBRAIN_PORT."))?;
    let token = env::var("JBRAIN_TOKEN").ok().filter(|s| !s.is_empty());
    if !["localhost", "127.0.0.1", "::1"].contains(&host.as_str()) && token.is_none() {
        return Err(Error::new(
            400,
            "CONFIG_ERROR",
            "JBRAIN_TOKEN is required beyond localhost.",
        ));
    }
    let history = match env::var("JBRAIN_HISTORY")
        .unwrap_or_else(|_| "git".into())
        .as_str()
    {
        "off" => false,
        "git" => true,
        _ => {
            return Err(Error::new(
                400,
                "CONFIG_ERROR",
                "JBRAIN_HISTORY must be off or git.",
            ));
        }
    };
    let data_dir = PathBuf::from(env::var("JBRAIN_DATA_DIR").unwrap_or_else(|_| ".jbrain".into()));
    let brain = Brain::open(Config {
        data_dir,
        history,
        reindex,
        max_pending: positive("JBRAIN_MAX_PENDING", 64)?,
        max_pending_bytes: positive("JBRAIN_MAX_PENDING_BYTES", 8 * 1024 * 1024)?,
    })?;
    let bind = if host.contains(':') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    };
    let listener = tokio::net::TcpListener::bind(bind).await?;
    let address = listener.local_addr()?;
    let app = api::router(brain.clone(), token, &host);
    println!(
        "{}",
        json!({"event":"ready","url":format!("http://{address}"),"runtime":"rust"})
    );
    let shutdown_brain = brain.clone();
    let result = axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            shutdown_signal().await;
            shutdown_brain.stop_accepting();
        })
        .await;
    brain.shutdown();
    result?;
    Ok(())
}
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
        tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{}}
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
fn print_help() {
    println!(
        "jbrain — native shared OKF brain\n\n  serve [--reindex]\n  health\n  list [--limit 50] [--offset 0]\n  get <id>\n  search <query>\n  ask <question> (configured remote model)\n  evidence <catalog|find|read|compute|links|neighbors> --file arguments.json\n  query <question> [--limit 8] [--chars 16000] [--plan plan.json]\n  browse [--prefix path/] [--kind Type] [--period value]\n  neighbors <id> [--direction outgoing|incoming] [--kind Type] [--period value] [--entity name]\n  links <id>\n  operation <request-id>\n  put <id> --file <okf.md> --actor <agent/version> --request-id <uuid> --create\n  put <id> --file <okf.md> --actor <agent/version> --request-id <uuid> --revision <current>\n  delete <id> --actor <agent/version> --request-id <uuid> --revision <current>\n  mcp  (optional native stdio proxy; direct /mcp HTTP uses fewer processes)\n\nClients use JBRAIN_URL and optional JBRAIN_TOKEN. See README.md."
    );
}
