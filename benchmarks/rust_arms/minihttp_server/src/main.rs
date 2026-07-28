//! The may-minihttp arm: the compiled *floor*, not a framework.
//!
//! `granian-rsgi` answers "what is left when the Python framework is gone?".
//! This is the same question on the other side of the language boundary: a
//! coroutine-per-connection HTTP server with no framework, no middleware and no
//! router, so the matrix has a bound on what the machine will do when almost
//! nothing is asked of it.
//!
//! may-minihttp is built on `may`, which is green threads rather than
//! async/await -- one coroutine per connection, no futures. That difference is
//! part of the point: if the async arms and this one land close together, the
//! executor model is not what the matrix is measuring.
//!
//! **It has no router, and is excluded from the routing scenarios for that
//! reason** -- exactly like `granian-rsgi`. Dispatch below is a `match` on the
//! path plus one prefix test. That is not routing: no parameter extraction, no
//! method resolution, no ordering, no 10,000-entry table. Enter it in the
//! `routing-*` scenarios and it would post the best number in the table for work
//! no other arm is allowed to skip. It still accepts `--routes` so `run.py` can
//! pass one invocation shape to every compiled arm; the file is validated and
//! then ignored.
//!
//! **Caveat that belongs in any reading of this row: this arm runs with Nagle's
//! algorithm on.** `may_minihttp`'s accept loop has its `set_nodelay(true)` call
//! commented out in the crate source, and the crate exposes no hook to set it --
//! `HttpServer::start` owns the listener. Every other server in the matrix sets
//! `TCP_NODELAY`. Fixing it would mean vendoring the crate, which would stop this
//! being the code a may-minihttp user actually gets. Treat a low number here as
//! possibly the socket option rather than the server.

use std::io::Read;

use may_minihttp::{HttpServer, HttpService, Request, Response};

const LARGE_BODY: &[u8; 65_536] = &[b'x'; 65_536];

#[derive(Clone)]
struct Floor;

impl HttpService for Floor {
    fn call(&mut self, request: Request, response: &mut Response) -> std::io::Result<()> {
        // `Request::body` takes `self`, so anything borrowed from the request --
        // the path, a header -- has to be copied out before the body is touched.
        let path = request.path().to_string();
        let benchmark_header = request
            .headers()
            .iter()
            .find(|header| header.name.eq_ignore_ascii_case("x-benchmark"))
            .map(|header| String::from_utf8_lossy(header.value).into_owned());

        match path.as_str() {
            "/" => {
                response.header("Content-Type: text/plain; charset=utf-8");
                response.body("hello, world");
            }
            "/json" => {
                response.header("Content-Type: application/json");
                response.body(r#"{"message":"hello"}"#);
            }
            "/headers" => {
                response.header("Content-Type: text/plain; charset=utf-8");
                response.body_vec(benchmark_header.unwrap_or_default().into_bytes());
            }
            "/body" => {
                let body = read_body(request)?;
                response.header("Content-Type: text/plain; charset=utf-8");
                response.body_vec(body.len().to_string().into_bytes());
            }
            "/json-body" => {
                let body = read_body(request)?;
                match serde_json::from_slice::<serde_json::Value>(&body) {
                    Ok(value) => {
                        response.header("Content-Type: application/json");
                        response.body_vec(value.to_string().into_bytes());
                    }
                    Err(_) => {
                        response.status_code(400, "Bad Request");
                        response.body("invalid json");
                    }
                }
            }
            "/response-64k" => {
                response.header("Content-Type: text/plain; charset=utf-8");
                response.body_vec(LARGE_BODY.to_vec());
            }
            "/cached" => {
                response.header("Content-Type: text/plain; charset=utf-8");
                response.header("Cache-Control: public, max-age=60");
                response.body("cacheable");
            }
            // The whole of this arm's "routing": everything after the prefix is
            // the id. See the module comment -- this is why it is a floor.
            other if other.starts_with("/users/") => {
                let user_id = &other["/users/".len()..];
                response.header("Content-Type: application/json");
                response
                    .body_vec(serde_json::json!({ "user_id": user_id }).to_string().into_bytes());
            }
            _ => {
                response.status_code(404, "Not Found");
                response.header("Content-Type: text/plain; charset=utf-8");
                response.body("not found");
            }
        }
        Ok(())
    }
}

fn read_body(request: Request) -> std::io::Result<Vec<u8>> {
    let mut buffer = Vec::new();
    request.body().read_to_end(&mut buffer)?;
    Ok(buffer)
}

fn main() {
    let options = match bench_common::parse_args(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => bench_common::fail("minihttp", error),
    };
    // `--routes` is accepted and deliberately unused; see the module comment.
    // Still validated, so a broken table is reported by the one arm that does
    // not read it rather than silently tolerated.
    if let Some(path) = options.routes.as_deref() {
        if let Err(error) = bench_common::read_route_table(path) {
            bench_common::fail("minihttp", error);
        }
    }

    // `may` sizes its scheduler explicitly rather than from CPU affinity, so
    // unlike the Tokio arms this one has to be told. `available_parallelism`
    // respects the harness's `taskset`, so this lands on the same cores.
    let workers = options.threads.unwrap_or_else(|| {
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
    });
    may::config().set_workers(workers);

    let address = options.address();
    println!("wreath-bench-minihttp listening on {address}");
    match HttpServer(Floor).start(address.as_str()) {
        Ok(handle) => {
            let _ = handle.join();
        }
        Err(error) => bench_common::fail("minihttp", format!("cannot bind {address}: {error}")),
    }
}
