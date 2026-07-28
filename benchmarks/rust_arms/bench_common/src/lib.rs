//! What every compiled arm needs before it can serve: its options, and the
//! route table.
//!
//! Shared rather than copied four times because both are places where a
//! divergence would be invisible in the result. Two arms parsing `--port`
//! differently is a bug you find immediately; two arms registering *different
//! route tables* is a bug that just makes one of them look faster.

use std::fmt;

/// The command line every arm accepts, so `run.py` builds one invocation shape.
pub struct Options {
    pub host: String,
    pub port: u16,
    pub routes: Option<String>,
    /// `None` means the runtime's own default worker count.
    ///
    /// For the Tokio arms that default derives from `available_parallelism()`,
    /// which respects the `taskset` affinity `benchmarks/run.py` applies -- so an
    /// arm takes exactly the cores the harness gave it. Forcing a count here
    /// would hand one arm more or fewer CPUs than the rest. See the module
    /// comment in `axum_server/src/main.rs` for how getting this wrong inverted
    /// a result.
    pub threads: Option<usize>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_string(),
            port: 8000,
            routes: None,
            threads: None,
        }
    }
}

impl Options {
    /// `host:port`, ready to parse into a `SocketAddr`.
    pub fn address(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

#[derive(Debug)]
pub struct ArgError(pub String);

impl fmt::Display for ArgError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Parse the shared options, rejecting anything unrecognised.
///
/// Unknown flags are an error rather than ignored: `run.py` passes `--routes` to
/// every arm, and an arm that quietly dropped it would boot with no route table
/// and answer the routing scenarios with 404s -- which the matrix would record
/// as a throughput number rather than as the failure it is.
pub fn parse_args<I: Iterator<Item = String>>(args: I) -> Result<Options, ArgError> {
    let mut options = Options::default();
    let mut args = args;
    while let Some(flag) = args.next() {
        let mut value = || {
            args.next()
                .ok_or_else(|| ArgError(format!("{flag} needs a value")))
        };
        match flag.as_str() {
            "--host" => options.host = value()?,
            "--port" => {
                options.port = value()?
                    .parse()
                    .map_err(|_| ArgError("--port must be a number".into()))?
            }
            "--routes" => options.routes = Some(value()?),
            "--threads" => {
                let count: usize = value()?
                    .parse()
                    .map_err(|_| ArgError("--threads must be a number".into()))?;
                if count == 0 {
                    return Err(ArgError("--threads must be at least 1".into()));
                }
                options.threads = Some(count);
            }
            other => return Err(ArgError(format!("unknown option: {other}"))),
        }
    }
    Ok(options)
}

/// The 10,000-route table, as `(method, path)` pairs.
///
/// Read from the JSON file `run.py` writes from `ROUTE_SPECS`, never re-derived
/// here: every Python arm registers that exact table, and a second implementation
/// of the spec is a second thing to keep correct. Paths use `{param}` syntax,
/// which Axum, ntex and xitca-web all accept unchanged.
pub fn read_route_table(path: &str) -> Result<Vec<(String, String)>, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|error| format!("cannot read route table {path}: {error}"))?;
    serde_json::from_str(&text)
        .map_err(|error| format!("route table {path} is not [[method, path], ...]: {error}"))
}

/// Exit with a message on the same stream and shape for every arm.
pub fn fail(arm: &str, message: impl fmt::Display) -> ! {
    eprintln!("wreath-bench-{arm}: {message}");
    std::process::exit(2)
}
