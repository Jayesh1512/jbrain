use crate::{
    brain::Brain,
    error::{Error, Result},
    mcp,
};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Path, Query, Request, State},
    http::{StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashMap, sync::Arc};
use tokio::sync::Semaphore;

pub const MAX_BODY: usize = 6_500_000;
struct ApiState {
    brain: Arc<Brain>,
    token: Option<[u8; 32]>,
    body_slots: Semaphore,
}
type App = Arc<ApiState>;

pub fn router(brain: Arc<Brain>, token: Option<String>, host: &str) -> Router {
    let state = Arc::new(ApiState {
        brain: brain.clone(),
        token: token
            .filter(|s| !s.is_empty())
            .map(|t| Sha256::digest(format!("Bearer {t}").as_bytes()).into()),
        body_slots: Semaphore::new(4),
    });
    Router::new()
        .route("/",get(||async{Json(json!({"name":"jbrain","version":"0.2.0","runtime":"rust","protocol":"OKF v0.2"}))}))
        .route("/health",get(health))
        .route("/v1/concept",get(concept))
        .route("/v1/concepts",get(list))
        .route("/v1/search",get(search))
        .route("/v1/query",post(query))
        .route("/v1/ask",post(ask))
        .route("/v1/evidence",post(evidence))
        .route("/v1/browse",get(browse))
        .route("/v1/neighbors",get(neighbors))
        .route("/v1/links",get(links))
        .route("/v1/operations",post(write))
        .route("/v1/operations/{id}",get(operation))
        .nest_service("/mcp",mcp::http_service(brain,host))
        .fallback(||async{Error::new(404,"NOT_FOUND","Route not found.")})
        .layer(middleware::from_fn_with_state(state.clone(),guard))
        .with_state(state)
}

async fn guard(State(state): State<App>, request: Request, next: Next) -> Response {
    if let Some(expected) = state.token {
        let supplied = Sha256::digest(
            request
                .headers()
                .get(header::AUTHORIZATION)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .as_bytes(),
        );
        let mut difference = 0u8;
        for (a, b) in supplied.iter().zip(expected) {
            difference |= a ^ b;
        }
        if difference != 0 {
            return Error::new(401, "UNAUTHORIZED", "A valid service token is required.")
                .into_response();
        }
    }
    if let Some(origin) = request.headers().get(header::ORIGIN) {
        let host = request
            .headers()
            .get(header::HOST)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if origin.to_str().ok() != Some(format!("http://{host}").as_str()) {
            return Error::new(403, "ORIGIN_DENIED", "Cross-origin access is disabled.")
                .into_response();
        }
    }
    let mut response = next.run(request).await;
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    response
        .headers_mut()
        .insert(header::X_CONTENT_TYPE_OPTIONS, "nosniff".parse().unwrap());
    response
}
async fn health(State(state): State<App>) -> Result<Response> {
    let health = state.brain.health()?;
    Ok((
        if health["status"] == "ready" {
            StatusCode::OK
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        },
        Json(health),
    )
        .into_response())
}
async fn concept(
    State(state): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Response> {
    let value = state.brain.get(argument(&query, "id"))?;
    let etag = format!("\"{}\"", value["revision"].as_str().unwrap());
    Ok(([(header::ETAG, etag)], Json(value)).into_response())
}
async fn list(
    State(state): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Json<Value>> {
    Ok(Json(state.brain.list(
        number(&query, "limit", 50, 100)?,
        number(&query, "offset", 0, 1_000_000)?,
    )?))
}
async fn search(
    State(state): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Json<Value>> {
    Ok(Json(state.brain.search(
        argument(&query, "q"),
        number(&query, "limit", 20, 100)?,
    )?))
}
async fn links(
    State(state): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Json<Value>> {
    Ok(Json(state.brain.links(argument(&query, "id"))?))
}
async fn query(State(state): State<App>, request: Request<Body>) -> Result<Json<Value>> {
    let bytes = to_bytes(request.into_body(), 16_384)
        .await
        .map_err(|_| Error::new(413, "BODY_TOO_LARGE", "Query exceeds 16 KiB."))?;
    let input = serde_json::from_slice(&bytes)?;
    Ok(Json(state.brain.query(input)?))
}
async fn ask(State(state): State<App>, request: Request<Body>) -> Result<Json<Value>> {
    let bytes = to_bytes(request.into_body(), 16_384)
        .await
        .map_err(|_| Error::new(413, "BODY_TOO_LARGE", "Ask exceeds 16 KiB."))?;
    Ok(Json(
        crate::ask::Workflow::ask(state.brain.clone(), serde_json::from_slice(&bytes)?).await?,
    ))
}
async fn evidence(State(state): State<App>, request: Request<Body>) -> Result<Json<Value>> {
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct EvidenceRequest {
        tool: String,
        arguments: Value,
    }
    let bytes = to_bytes(request.into_body(), 16_384)
        .await
        .map_err(|_| Error::new(413, "BODY_TOO_LARGE", "Evidence request exceeds 16 KiB."))?;
    let input: EvidenceRequest = serde_json::from_slice(&bytes)?;
    Ok(Json(state.brain.evidence(&input.tool, input.arguments)?))
}
async fn browse(
    State(state): State<App>,
    Query(request): Query<crate::retrieval::BrowseRequest>,
) -> Result<Json<Value>> {
    Ok(Json(state.brain.browse(request)?))
}
async fn neighbors(
    State(state): State<App>,
    Query(request): Query<crate::retrieval::NeighborRequest>,
) -> Result<Json<Value>> {
    Ok(Json(state.brain.neighbors(request)?))
}
async fn operation(State(state): State<App>, Path(id): Path<String>) -> Result<Json<Value>> {
    Ok(Json(state.brain.operation(&id)?))
}
async fn write(State(state): State<App>, request: Request<Body>) -> Result<Response> {
    if !request
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|s| s.to_ascii_lowercase().starts_with("application/json"))
    {
        return Err(Error::new(415, "JSON_REQUIRED", "Send application/json."));
    }
    let value = {
        let _permit = state.body_slots.acquire().await.map_err(|_| {
            Error::new(
                503,
                "QUEUE_FULL",
                "Request parser is unavailable. Retry the same ID and payload.",
            )
        })?;
        let bytes = to_bytes(request.into_body(), MAX_BODY).await.map_err(|_| {
            Error::new(
                413,
                "BODY_TOO_LARGE",
                "Request body exceeds the service limit.",
            )
        })?;
        serde_json::from_slice(&bytes)?
    };
    let (status, value) = state.brain.write(value).await?;
    Ok((StatusCode::from_u16(status).unwrap(), Json(value)).into_response())
}
fn argument<'a>(query: &'a HashMap<String, String>, name: &str) -> &'a str {
    query.get(name).map(String::as_str).unwrap_or("")
}
fn number(
    query: &HashMap<String, String>,
    name: &str,
    default: usize,
    max: usize,
) -> Result<usize> {
    match query.get(name) {
        None => Ok(default),
        Some(value) => value
            .parse::<usize>()
            .ok()
            .filter(|n| *n <= max)
            .ok_or_else(|| Error::new(400, "INVALID_PARAMETER", "Invalid pagination parameter.")),
    }
}
