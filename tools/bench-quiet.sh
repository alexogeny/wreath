#!/usr/bin/env bash
# Run the framework matrix on a quieted machine, without retyping the sudo dance.
# Tier-1 quieting writes to sysfs and stops named services, so it needs root; the
# benchmark must *stay* root afterwards to undo those writes. That means the whole
# run goes under sudo, and sudo resets PATH, which loses the load generator. This
# script is that invocation, once, correctly.
#   tools/bench-quiet.sh                        # the standard matrix
#   tools/bench-quiet.sh --requests 32000       # ... with an override
#   tools/bench-quiet.sh --framework wreath-metal blacksheep
# Arguments are appended after the defaults, and argparse takes the last value
# for a repeated option, so anything below can be overridden by passing it again.
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
bench="$repo/.venv/bin/wreath-bench"

if [[ ! -x "$bench" ]]; then
  echo "bench-quiet: $bench is missing; run 'uv sync' in $repo first." >&2
  exit 1
fi

# h2load is resolved from PATH by the benchmark's child process, and sudo's
# secure_path would drop the directory it lives in. Fail here, where the message
# can say what to install, rather than mid-matrix.
generator="${WREATH_LOAD_GENERATOR:-h2load}"
if ! command -v "$generator" >/dev/null 2>&1; then
  echo "bench-quiet: $generator is not on PATH (Debian: apt install nghttp2-client)." >&2
  exit 1
fi

# Sanic is not here on purpose: it is the slowest arm in the suite to boot, and
# every trial boots its own server. It is still in the matrix -- pass
# `--framework sanic ...` to get it back.
# The last three arms are reference points rather than peers: `blacksheep-granian`
# is the same BlackSheep app on Granian instead of Uvicorn and only means anything
# read against `blacksheep`; `granian-rsgi` is no framework at all, the floor; and
# `axum` is Rust end to end, the ceiling. See benchmarks/README.md.
args=(
  --quiet=1 --quiet-apply
  --matrix-only
  --no-db
  --framework wreath wreath-native wreath-metal blacksheep blacksheep-granian granian-rsgi axum
  --concurrency 8
  --requests 16000
  --load-generator "$generator"
  "$@"
)

# Build the Rust arm before the machine is quieted, not during the run: a cargo
# build is minutes of every core at full tilt, and it would land on whichever
# arm happened to be measuring. Skipped when the matrix does not include it.
if [[ " ${args[*]} " == *" axum "* ]]; then
  if ! command -v cargo >/dev/null 2>&1; then
    echo "bench-quiet: the axum arm needs a Rust toolchain (https://rustup.rs)." >&2
    exit 1
  fi
  # Just this arm, not the whole rust_arms workspace: the other members are not
  # wired into run.py yet, so building them is minutes bought for nothing. The
  # binary still lands in the workspace's shared target/, which run.py reads.
  cargo build --release --quiet \
    --manifest-path "$repo/benchmarks/rust_arms/axum_server/Cargo.toml"
fi

if [[ $EUID -eq 0 ]]; then
  exec "$bench" "${args[@]}"
fi

# -E keeps the caller's environment (uv/venv vars the benchmark reads); the
# explicit PATH is what survives secure_path, and is how the child finds h2load.
exec sudo -E env "PATH=$PATH" "$bench" "${args[@]}"
