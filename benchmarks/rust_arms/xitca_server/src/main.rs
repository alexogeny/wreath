//! The xitca-web arm.
//!
//! The third compiled arm, and the other consistent TechEmpower front-runner
//! alongside ntex. Same purpose as `axum` and `ntex`: bound the matrix from
//! above, and let a reader tell "compiled code does this" apart from "Axum does
//! this".
//!
//! Routing note: xitca-web's path syntax is `{param}`, matching the shared table,
//! so `ROUTE_SPECS` registers unchanged.

use xitca_web::handler::handler_service;
use xitca_web::handler::path::PathRef;
use xitca_web::http::{header, WebResponse};
use xitca_web::route::{delete, get, patch, post, put};
use xitca_web::{App, WebContext};

const LARGE_BODY: &[u8; 65_536] = &[b'x'; 65_536];

fn text(body: impl Into<Vec<u8>>) -> WebResponse {
    let mut response = WebResponse::new(body.into().into());
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    response
}

fn json(body: String) -> WebResponse {
    let mut response = WebResponse::new(body.into_bytes().into());
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("application/json"),
    );
    response
}

/// `GET /` -- `PlainTextResponse("hello, world")`.
async fn plaintext() -> WebResponse {
    text("hello, world")
}

/// `GET /json` -- `JSONResponse({"message": "hello"})`.
async fn json_response() -> WebResponse {
    json(r#"{"message":"hello"}"#.to_string())
}

/// `GET /users/{user_id}` -- echoes the captured segment back as JSON.
///
/// Taken off the path rather than through a typed extractor so this arm does the
/// same work as the others: find the segment, encode it as JSON.
async fn parameter(PathRef(path): PathRef<'_>) -> WebResponse {
    let user_id = path.rsplit('/').next().unwrap_or("");
    json(serde_json::json!({ "user_id": user_id }).to_string())
}

/// `GET /headers` -- the `x-benchmark` request header, or empty when absent.
async fn header_lookup(ctx: &WebContext<'_>) -> WebResponse {
    let value = ctx
        .req()
        .headers()
        .get("x-benchmark")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();
    text(value)
}

/// `POST /body` -- the received body's length, as text.
async fn request_body(body: Vec<u8>) -> WebResponse {
    text(body.len().to_string())
}

/// `POST /json-body` -- decode and re-encode, matching `await request.json()`.
async fn request_json(body: Vec<u8>) -> WebResponse {
    match serde_json::from_slice::<serde_json::Value>(&body) {
        Ok(value) => json(value.to_string()),
        Err(_) => {
            let mut response = text("invalid json");
            *response.status_mut() = xitca_web::http::StatusCode::BAD_REQUEST;
            response
        }
    }
}

/// `GET /response-64k` -- a fixed 64 KiB body.
async fn large_response() -> WebResponse {
    text(&LARGE_BODY[..])
}

/// `GET /cached` -- a small body carrying the shared `Cache-Control` value.
async fn cached() -> WebResponse {
    let mut response = text("cacheable");
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("public, max-age=60"),
    );
    response
}

/// Every entry in the 10,000-route table answers identically.
async fn routing_leaf() -> WebResponse {
    text("route-hit")
}

fn main() -> std::io::Result<()> {
    let options = match bench_common::parse_args(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => bench_common::fail("xitca", error),
    };
    let table = match options.routes.as_deref() {
        Some(path) => match bench_common::read_route_table(path) {
            Ok(table) => table,
            Err(error) => bench_common::fail("xitca", error),
        },
        None => Vec::new(),
    };
    let address = options.address();

    let mut app = App::new()
        .at("/", get(handler_service(plaintext)))
        .at("/json", get(handler_service(json_response)))
        .at("/users/{user_id}", get(handler_service(parameter)))
        .at("/headers", get(handler_service(header_lookup)))
        .at("/body", post(handler_service(request_body)))
        .at("/json-body", post(handler_service(request_json)))
        .at("/response-64k", get(handler_service(large_response)))
        .at("/cached", get(handler_service(cached)));
    for (method, route) in &table {
        let service = match method.as_str() {
            "GET" => get(handler_service(routing_leaf)),
            "POST" => post(handler_service(routing_leaf)),
            "PUT" => put(handler_service(routing_leaf)),
            "PATCH" => patch(handler_service(routing_leaf)),
            "DELETE" => delete(handler_service(routing_leaf)),
            other => bench_common::fail("xitca", format!("unknown method {other}")),
        };
        app = app.at(route.as_str(), service);
    }

    println!("wreath-bench-xitca listening on {address}");
    let server = app.serve().bind(&address)?;
    match options.threads {
        Some(count) => server.worker_threads(count).run().wait(),
        None => server.run().wait(),
    }
}
