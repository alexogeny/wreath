//! The ntex arm.
//!
//! ntex is consistently at the top of the TechEmpower plaintext and JSON boards,
//! so it is here for the same reason `axum` is: to bound the matrix from above
//! with something that is not Python. Having *two* Rust arms is not redundancy --
//! it separates "this is what compiled code does" from "this is what one
//! particular compiled framework does", and if the two disagree by much, the
//! ceiling was really a property of Axum rather than of Rust.
//!
//! Routing note: ntex's path syntax is `{param}`, the same as the shared table's,
//! so `ROUTE_SPECS` is registered unchanged -- no dialect rewrite, nothing to
//! drift. See `route_path` in `benchmarks/scenarios.py`.
//!
//! Threads: `--threads` maps to ntex's worker count, and is left at the default
//! (one worker per available CPU) so the arm takes exactly the cores the
//! harness's `taskset` gave it, like every other server. See the module comment
//! in `../axum_server/src/main.rs` for why forcing a lower count is wrong.

use ntex::http::header::{HeaderValue, CACHE_CONTROL, CONTENT_TYPE};
use ntex::web::types::Path;
use ntex::web::{self, App, HttpRequest, HttpResponse, HttpServer};

const LARGE_BODY: &[u8; 65_536] = &[b'x'; 65_536];

/// `GET /` -- `PlainTextResponse("hello, world")`.
async fn plaintext() -> HttpResponse {
    HttpResponse::Ok()
        .header(CONTENT_TYPE, "text/plain; charset=utf-8")
        .body("hello, world")
}

/// `GET /json` -- `JSONResponse({"message": "hello"})`.
async fn json_response() -> HttpResponse {
    json_body(r#"{"message":"hello"}"#.to_string())
}

/// `GET /users/{user_id}` -- echoes the captured segment back as JSON.
async fn parameter(user_id: Path<String>) -> HttpResponse {
    json_body(serde_json::json!({ "user_id": user_id.into_inner() }).to_string())
}

/// `GET /headers` -- the `x-benchmark` request header, or empty when absent.
async fn header_lookup(request: HttpRequest) -> HttpResponse {
    let value = request
        .headers()
        .get("x-benchmark")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();
    HttpResponse::Ok()
        .header(CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(value)
}

/// `POST /body` -- the received body's length, as text.
async fn request_body(body: ntex::util::Bytes) -> HttpResponse {
    HttpResponse::Ok()
        .header(CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(body.len().to_string())
}

/// `POST /json-body` -- decode and re-encode, matching `await request.json()`.
async fn request_json(body: ntex::util::Bytes) -> HttpResponse {
    match serde_json::from_slice::<serde_json::Value>(&body) {
        Ok(value) => json_body(value.to_string()),
        Err(_) => HttpResponse::BadRequest().body("invalid json"),
    }
}

/// `GET /response-64k` -- a fixed 64 KiB body.
async fn large_response() -> HttpResponse {
    HttpResponse::Ok()
        .header(CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(&LARGE_BODY[..])
}

/// `GET /cached` -- a small body carrying the shared `Cache-Control` value.
async fn cached() -> HttpResponse {
    let mut response = HttpResponse::Ok();
    response.header(CONTENT_TYPE, "text/plain; charset=utf-8");
    response.header(CACHE_CONTROL, HeaderValue::from_static("public, max-age=60"));
    response.body("cacheable")
}

/// Every entry in the 10,000-route table answers identically.
async fn routing_leaf() -> HttpResponse {
    HttpResponse::Ok()
        .header(CONTENT_TYPE, "text/plain; charset=utf-8")
        .body("route-hit")
}

fn json_body(body: String) -> HttpResponse {
    HttpResponse::Ok()
        .header(CONTENT_TYPE, "application/json")
        .body(body)
}

fn main() -> std::io::Result<()> {
    let options = match bench_common::parse_args(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => bench_common::fail("ntex", error),
    };
    // Read once, before any worker starts: `HttpServer` runs its factory per
    // worker, and re-reading the 10,000-route file inside that closure would
    // parse it once per core for no reason.
    let table = match options.routes.as_deref() {
        Some(path) => match bench_common::read_route_table(path) {
            Ok(table) => table,
            Err(error) => bench_common::fail("ntex", error),
        },
        None => Vec::new(),
    };
    let address = options.address();

    ntex::rt::System::new("wreath-bench-ntex").block_on(async move {
        let mut server = HttpServer::new(move || {
            let mut app = App::new()
                .service(web::resource("/").route(web::get().to(plaintext)))
                .service(web::resource("/json").route(web::get().to(json_response)))
                .service(web::resource("/users/{user_id}").route(web::get().to(parameter)))
                .service(web::resource("/headers").route(web::get().to(header_lookup)))
                .service(web::resource("/body").route(web::post().to(request_body)))
                .service(web::resource("/json-body").route(web::post().to(request_json)))
                .service(web::resource("/response-64k").route(web::get().to(large_response)))
                .service(web::resource("/cached").route(web::get().to(cached)));
            for (method, route) in &table {
                let handler = match method.as_str() {
                    "GET" => web::get(),
                    "POST" => web::post(),
                    "PUT" => web::put(),
                    "PATCH" => web::patch(),
                    "DELETE" => web::delete(),
                    other => bench_common::fail("ntex", format!("unknown method {other}")),
                };
                app = app.service(web::resource(route.as_str()).route(handler.to(routing_leaf)));
            }
            app
        });
        if let Some(count) = options.threads {
            server = server.workers(count);
        }
        println!("wreath-bench-ntex listening on {address}");
        server.bind(&address)?.run().await
    })
}
