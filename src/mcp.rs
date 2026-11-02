use crate::{
    brain::Brain,
    cli::Client,
    error::{Error, Result},
};
use rmcp::{
    ErrorData, RoleServer, ServerHandler,
    model::*,
    service::RequestContext,
    transport::streamable_http_server::{
        StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
    },
};
use serde_json::{Value, json};
use std::sync::{Arc, OnceLock};

#[derive(Clone)]
pub enum Backend {
    Local(Arc<Brain>),
    Remote(Client),
}
#[derive(Clone)]
pub struct Mcp {
    pub backend: Backend,
}

pub fn http_service(
    brain: Arc<Brain>,
    host: &str,
) -> StreamableHttpService<Mcp, LocalSessionManager> {
    let config = StreamableHttpServerConfig::default()
        .with_legacy_session_mode(false)
        .with_json_response(true)
        .with_sse_keep_alive(None)
        .with_sse_retry(None)
        .with_allowed_hosts(["127.0.0.1", "localhost", "::1", host]);
    let mut config = config;
    config.max_request_body_bytes = crate::api::MAX_BODY;
    StreamableHttpService::new(
        move || {
            Ok(Mcp {
                backend: Backend::Local(brain.clone()),
            })
        },
        Arc::new(LocalSessionManager::default()),
        config,
    )
}

fn tools() -> &'static Vec<Tool> {
    static TOOLS: OnceLock<Vec<Tool>> = OnceLock::new();
    TOOLS.get_or_init(||{
        let definitions=[
            ("brain_ask","Run the daemon's pinned model workflow: preserve question conditions, read sources, compute exact values, verify citations and independently audit the answer. Requires model configuration; returns actual usage. Only complete=true is an accepted answer; needs_verification/retryable is not an answer.",json!({"question":{"type":"string","minLength":1,"maxLength":4096}}),vec!["question"]),
            ("brain_evidence","Explicit read-only evidence tools for external agents: catalog, find, read, compute, links, neighbors. See docs/ACCURACY_WORKFLOW.md for typed arguments. Never equate candidates or plan satisfaction with answer support.",json!({"tool":{"type":"string","enum":["catalog","find","read","compute","links","neighbors"]},"arguments":{"type":"object"}}),vec!["tool","arguments"]),
            ("brain_health","Check shared service readiness.",json!({}),vec![]),
            ("brain_read","Read an OKF concept and its current revision before editing.",json!({"id":{"type":"string"}}),vec!["id"]),
            ("brain_list","Browse concept metadata.",json!({"limit":{"type":"integer","minimum":1,"maximum":100,"default":50},"offset":{"type":"integer","minimum":0,"default":0}}),vec![]),
            ("brain_search","Lexical BM25 search over titles, tags, aliases and content. No embeddings. Send 2-5 distinctive keywords (names, terms, dates), not a full sentence, and search once per fact; common question words are ignored.",json!({"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100,"default":20}}),vec!["query"]),
            ("brain_query","Interpret a question and retrieve bounded, revision-cited evidence. complete is always false here: plan_satisfied only describes interpreted clauses. Verify original question with brain_ask. Optional plan is caller interpreted; this tool makes no model calls.",json!({"question":{"type":"string","minLength":1,"maxLength":4096},"limit":{"type":"integer","minimum":1,"maximum":16,"default":8},"max_chars":{"type":"integer","minimum":2048,"maximum":64000,"default":16000},"plan":{"type":"object","additionalProperties":false,"required":["clauses"],"properties":{"clauses":{"type":"array","minItems":1,"maxItems":4,"items":{"type":"object","additionalProperties":false,"properties":{"text":{"type":"string"},"entities":{"type":"array","maxItems":4,"items":{"type":"string"}},"period":{"type":["string","null"]},"date":{"type":["string","null"]},"metrics":{"type":"array","maxItems":4,"items":{"type":"string"}},"kind":{"type":["string","null"]},"basis":{"type":["string","null"]},"terms":{"type":"array","maxItems":32,"items":{"type":"string"}}}}}}}}),vec!["question"]),
            ("brain_browse","Progressively browse a derived metadata catalog, with prefix, kind and period filters.",json!({"prefix":{"type":"string"},"kind":{"type":"string"},"period":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100},"offset":{"type":"integer","minimum":0}}),vec![]),
            ("brain_neighbors","Read at most 64 resolved linked documents, filtering before limiting. Includes link label and heading.",json!({"id":{"type":"string"},"direction":{"type":"string","enum":["incoming","outgoing"]},"kind":{"type":"string"},"period":{"type":"string"},"entity":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":64}}),vec!["id"]),
            ("brain_links","Follow outgoing concept links and incoming backlinks.",json!({"id":{"type":"string"}}),vec!["id"]),
            ("brain_operation","Inspect a durable request after a disconnect or uncertain result.",json!({"requestId":{"type":"string"}}),vec!["requestId"]),
            ("brain_write","Create/update/delete an OKF concept. Creation uses expectedRevision:null; update/delete requires the revision from brain_read. For put, markdown (full OKF text with a type) is required; for delete, omit it. Supply a unique requestId and actor. Retry uncertain delivery with the EXACT SAME ID/payload. On REVISION_CONFLICT, read, merge, and submit a NEW ID. Success confirms durable files and published search.",json!({"requestId":{"type":"string","minLength":1,"maxLength":128},"actor":{"type":"string","minLength":1},"action":{"type":"string","enum":["put","delete"]},"id":{"type":"string"},"expectedRevision":{"type":["string","null"]},"markdown":{"type":"string"}}),vec!["requestId","actor","action","id","expectedRevision"]),
        ];
        definitions.into_iter().map(|(name,description,properties,required)|serde_json::from_value(json!({"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":false},"annotations":{"readOnlyHint":name!="brain_write","destructiveHint":name=="brain_write","idempotentHint":true,"openWorldHint":false}})).unwrap()).collect()
    })
}

impl Mcp {
    async fn dispatch(&self, name: &str, args: Value) -> Result<Value> {
        let string = |key: &str| {
            args.get(key)
                .and_then(Value::as_str)
                .ok_or_else(|| Error::new(400, "INVALID_PARAMETER", format!("{key} is required.")))
        };
        let number = |key: &str, default: usize, max: usize| -> Result<usize> {
            match args.get(key) {
                None => Ok(default),
                Some(v) => v
                    .as_u64()
                    .map(|n| n as usize)
                    .filter(|n| *n <= max)
                    .ok_or_else(|| {
                        Error::new(400, "INVALID_PARAMETER", "Invalid pagination value.")
                    }),
            }
        };
        let path = match name {
            "brain_health" => "/health".into(),
            "brain_read" => format!("/v1/concept?id={}", crate::cli::encode(string("id")?)),
            "brain_list" => format!(
                "/v1/concepts?limit={}&offset={}",
                number("limit", 50, 100)?,
                number("offset", 0, 1_000_000)?
            ),
            "brain_search" => format!(
                "/v1/search?q={}&limit={}",
                crate::cli::encode(string("query")?),
                number("limit", 20, 100)?
            ),
            "brain_links" => format!("/v1/links?id={}", crate::cli::encode(string("id")?)),
            "brain_operation" => format!(
                "/v1/operations/{}",
                crate::cli::encode(string("requestId")?)
            ),
            "brain_write" => "/v1/operations".into(),
            "brain_query" => "/v1/query".into(),
            "brain_ask" => "/v1/ask".into(),
            "brain_evidence" => "/v1/evidence".into(),
            "brain_browse" | "brain_neighbors" => {
                let endpoint = if name == "brain_browse" {
                    "browse"
                } else {
                    "neighbors"
                };
                let fields = args
                    .as_object()
                    .ok_or_else(|| {
                        Error::new(400, "INVALID_PARAMETER", "Arguments must be an object.")
                    })?
                    .iter()
                    .map(|(key, value)| {
                        format!(
                            "{}={}",
                            crate::cli::encode(key),
                            crate::cli::encode(
                                value
                                    .as_str()
                                    .map(str::to_owned)
                                    .unwrap_or_else(|| value.to_string())
                                    .as_str()
                            )
                        )
                    })
                    .collect::<Vec<_>>();
                format!("/v1/{endpoint}?{}", fields.join("&"))
            }
            _ => return Err(Error::new(400, "UNKNOWN_TOOL", "Unknown tool.")),
        };
        match &self.backend {
            Backend::Remote(client) => {
                let client = client.clone();
                let payload = matches!(
                    name,
                    "brain_write" | "brain_query" | "brain_ask" | "brain_evidence"
                )
                .then_some(args);
                tokio::task::spawn_blocking(move || client.request(&path, payload))
                    .await
                    .map_err(|e| Error::new(503, "SERVICE_UNREACHABLE", e.to_string()))?
            }
            Backend::Local(brain) => match name {
                "brain_health" => brain.health(),
                "brain_read" => brain.get(string("id")?),
                "brain_list" => {
                    brain.list(number("limit", 50, 100)?, number("offset", 0, 1_000_000)?)
                }
                "brain_search" => brain.search(string("query")?, number("limit", 20, 100)?),
                "brain_links" => brain.links(string("id")?),
                "brain_query" => brain.query(serde_json::from_value(args)?),
                "brain_ask" => {
                    crate::ask::Workflow::ask(brain.clone(), serde_json::from_value(args)?).await
                }
                "brain_evidence" => brain.evidence(string("tool")?, args["arguments"].clone()),
                "brain_browse" => brain.browse(serde_json::from_value(args)?),
                "brain_neighbors" => brain.neighbors(serde_json::from_value(args)?),
                "brain_operation" => brain.operation(string("requestId")?),
                "brain_write" => {
                    let (status, value) = brain.write(args).await?;
                    if status != 200 {
                        return Err(Error::new(status, "WRITE_REJECTED", value.to_string()));
                    }
                    Ok(value)
                }
                _ => unreachable!(),
            },
        }
    }
}
impl ServerHandler for Mcp {
    fn get_info(&self) -> ServerInfo {
        let mut info = ServerInfo::default();
        info.capabilities = ServerCapabilities::builder().enable_tools().build();
        info.server_info = Implementation::new("jbrain", "0.2.0");
        info.instructions=Some("Shared OKF brain. Always read before updating; preserve the revision. Use stable request IDs for retries and merge revision conflicts. Use brain_ask for model-verified answers when configured; brain_query returns provisional evidence; brain_search is lexical BM25: it ignores common question words and, when few notes match all words, adds notes matching any word, so check the score and snippet. Send short keywords, one fact per search. Inspect coverage and qualifiers before answering; candidate documents alone do not establish support.".into());
        info
    }
    fn get_tool(&self, name: &str) -> Option<Tool> {
        tools().iter().find(|tool| tool.name == name).cloned()
    }
    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> std::result::Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult {
            tools: tools().clone(),
            ..Default::default()
        })
    }
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _: RequestContext<RoleServer>,
    ) -> std::result::Result<CallToolResponse, ErrorData> {
        let result = self
            .dispatch(
                &request.name,
                Value::Object(request.arguments.unwrap_or_default()),
            )
            .await;
        Ok(match result {
            Ok(value) => CallToolResult::success(vec![ContentBlock::text(value.to_string())]),
            Err(error) => {
                let payload = if error.code == "WRITE_REJECTED" {
                    serde_json::from_str::<Value>(&error.message).unwrap_or_else(|_| error.body())
                } else {
                    error.body()
                };
                CallToolResult::error(vec![ContentBlock::text(
                    json!({"status":error.status,"error":payload["error"]}).to_string(),
                )])
            }
        }
        .into())
    }
}
