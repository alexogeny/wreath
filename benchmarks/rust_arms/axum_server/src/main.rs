//! The Axum arm: the Rust reference ceiling of the framework matrix.
//!
//! This is not a competitor Wreath expects to beat. It is a *ceiling*: the same
//! scenarios, served by a compiled framework with no interpreter in the request
//! path, so a Python number can be read as a fraction of what the hardware and
//! the kernel will do at all. A matrix whose fastest and slowest entries are both
//! Python cannot tell you whether the spread you are looking at is the framework
//! or the runtime.
//!
//! **It takes the cores the harness gave it, and no more.** `--threads` defaults
//! to Tokio's own default, which derives its worker count from
//! `available_parallelism()` -- and that respects the `taskset` affinity
//! `benchmarks/run.py` applies via `WREATH_BENCH_SERVER_CPUS`. Pinned to one
//! physical core it runs two workers, the same two logical CPUs every other
//! server is confined to.
//!
//! This started as a `current_thread` runtime, on the theory that one thread
//! matched the single event loop every Python arm runs. It did not: Granian uses
//! two OS threads even at `--runtime-threads 1 --blocking-threads 1` (counted in
//! `/proc/<pid>/task`, not assumed), so forcing one thread here handed a Python
//! server an extra core and made the Rust *ceiling* measure ~40% below it. A rule
//! that inverts the result it exists to bound is the wrong rule; the right one is
//! that every arm gets the same CPUs and configures itself as it would ship.
//!
//! Handlers mirror `benchmarks/apps.py` response for response -- same bodies,
//! same content types -- because the load generator compares bytes on the wire.

use std::net::SocketAddr;

use axum::body::Bytes;
use axum::extract::Path;
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, patch, post, put};
use axum::serve::Listener;
use axum::Router;

/// A `TcpListener` that disables Nagle's algorithm on every accepted connection.
///
/// `axum::serve` does not do this -- its `Listener` impl for `tokio::net::
/// TcpListener` accepts and returns the socket untouched, and the nodelay helper
/// in axum's own source is test-only. Uvicorn, Granian and Wreath all set
/// `TCP_NODELAY`, so without this the Axum arm would be the only server in the
/// matrix waiting on Nagle before putting a small response on the wire.
struct NoDelayListener(tokio::net::TcpListener);

impl Listener for NoDelayListener {
    type Io = tokio::net::TcpStream;
    type Addr = SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match self.0.accept().await {
                Ok((stream, addr)) => {
                    if let Err(error) = stream.set_nodelay(true) {
                        // Refuse rather than serve a connection that would make
                        // this arm quietly incomparable to every other one.
                        bench_common::fail("axum", format!("cannot set TCP_NODELAY: {error}"));
                    }
                    return (stream, addr);
                }
                Err(_) => tokio::task::yield_now().await,
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        self.0.local_addr()
    }
}

/// `GET /` -- `PlainTextResponse("hello, world")`.
async fn plaintext() -> &'static str {
    "hello, world"
}

/// `GET /json` -- `JSONResponse({"message": "hello"})`.
async fn json_response() -> Response {
    json_body(r#"{"message":"hello"}"#.to_string())
}

/// `GET /users/{user_id}` -- echoes the captured segment back as JSON.
async fn parameter(Path(user_id): Path<String>) -> Response {
    // A real JSON encoder on a one-key map, rather than formatting the value
    // into a literal: the Python arms all pay for escaping here.
    json_body(serde_json::json!({ "user_id": user_id }).to_string())
}

/// `GET /headers` -- the `x-benchmark` request header, or empty when absent.
async fn header_lookup(headers: HeaderMap) -> String {
    headers
        .get("x-benchmark")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string()
}

/// `POST /body` -- the received body's length, as text.
async fn request_body(body: Bytes) -> String {
    body.len().to_string()
}

/// `POST /json-body` -- decode and re-encode, matching `await request.json()`.
async fn request_json(body: Bytes) -> Response {
    match serde_json::from_slice::<serde_json::Value>(&body) {
        Ok(value) => json_body(value.to_string()),
        Err(_) => (StatusCode::BAD_REQUEST, "invalid json").into_response(),
    }
}

/// `GET /response-64k` -- a fixed 64 KiB body.
async fn large_response() -> Bytes {
    Bytes::from_static(&[b'x'; 65_536])
}

/// `GET /cached` -- a small body carrying the shared `Cache-Control` value.
async fn cached() -> Response {
    ([(header::CACHE_CONTROL, "public, max-age=60")], "cacheable").into_response()
}

/// Every entry in the 10,000-route table answers identically.
async fn routing_leaf() -> &'static str {
    "route-hit"
}

fn json_body(body: String) -> Response {
    ([(header::CONTENT_TYPE, "application/json")], body).into_response()
}

fn build_router(routes: Option<&str>) -> Result<Router, String> {
    let mut router = Router::new()
        .route("/", get(plaintext))
        .route("/json", get(json_response))
        .route("/users/{user_id}", get(parameter))
        .route("/headers", get(header_lookup))
        .route("/body", post(request_body))
        .route("/json-body", post(request_json))
        .route("/response-64k", get(large_response))
        .route("/cached", get(cached));
    if let Some(path) = routes {
        for (method, route) in bench_common::read_route_table(path)? {
            let handler = match method.as_str() {
                "GET" => get(routing_leaf),
                "POST" => post(routing_leaf),
                "PUT" => put(routing_leaf),
                "PATCH" => patch(routing_leaf),
                "DELETE" => delete(routing_leaf),
                other => return Err(format!("route table has unknown method {other}")),
            };
            router = router.route(&route, handler);
        }
    }
    Ok(router)
}

fn main() {
    let options = match bench_common::parse_args(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => bench_common::fail("axum", error),
    };
    let router = match build_router(options.routes.as_deref()) {
        Ok(router) => router,
        Err(error) => bench_common::fail("axum", error),
    };

    let runtime = match options.threads {
        Some(1) => tokio::runtime::Builder::new_current_thread().enable_all().build(),
        Some(count) => tokio::runtime::Builder::new_multi_thread()
            .worker_threads(count)
            .enable_all()
            .build(),
        None => tokio::runtime::Builder::new_multi_thread().enable_all().build(),
    }
    .expect("tokio runtime");

    let address: SocketAddr = options
        .address()
        .parse()
        .expect("host and port form a socket address");

    runtime.block_on(async move {
        let listener = match tokio::net::TcpListener::bind(address).await {
            Ok(listener) => listener,
            Err(error) => bench_common::fail("axum", format!("cannot bind {address}: {error}")),
        };
        println!("wreath-bench-axum listening on {address}");
        if let Err(error) = axum::serve(NoDelayListener(listener), router).await {
            bench_common::fail("axum", error);
        }
    });
}
