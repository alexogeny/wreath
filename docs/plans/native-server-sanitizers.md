# Task: Run the native HTTP server C extension under ASan/UBSan

Status: implemented

Related: `docs/plans/native-http-server.md` (step 12), `docs/decisions/0008-native-http-server-boundary.md`

## Context

`src/neo/_native/_servermodule.c` is a hand-written C `asyncio.Protocol`
(`neo._native._server.HttpProtocol`). Its pure-Python twin is
`neo._pure.server`; behavioral tests are `tests/test_server_protocol.py`
(parameterized over both implementations, native cases marked with a skip
guard) and `tests/test_server.py` (loopback sockets). The facade that selects
the implementation is `neo.server`. Target CPython 3.14.

The protocol is functionally complete and passes the full suite, but the C has
not been run under sanitizers. This task closes that gap.

## HTTP/2 coverage (native-http2-http3 plan, Step 3)

The native server extension is now split into `server_common.c`,
`server_http1.c`, `server_http2.c`, and `server_hpack.c`.
`tools/sanitizers/setup_server.py` compiles all of them into the sanitized
`neo._native._server`. Run the HTTP/2 codec suite under ASan/UBSan/LSan:

```bash
uv run --no-sync python tools/sanitizers/build_server.py
LD_PRELOAD=$(gcc -print-file-name=libasan.so) \
  ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 \
  LSAN_OPTIONS=suppressions=$(pwd)/tools/sanitizers/lsan.supp \
  PYTHONPATH=$(pwd)/.sanitizers/native-server/lib \
  .venv/bin/python -m pytest tests/http2 --ignore=tests/http2/test_network.py -q
```

Result: the entire in-process HTTP/2 codec suite (146 tests) is clean under
ASan+UBSan+LSan, as is a dedicated repeated create/use/close driver. The only
LSan reports come from `test_network.py`, whose leaked allocations have no
frames in Neo's C — they are asyncio/OpenSSL server-teardown artifacts (the same
class as the HTTP/1.1 `test_tls_transport`) and are excluded above.

## HTTP/3 coverage (native-c-hotspots plan, Step 5)

The HTTP/3 response path queues the application's own immutable `bytes` objects
and hands nghttp3 raw pointers into them. Those addresses stay exposed until the
peer acknowledges the payload, so ASan is the only thing that can show a
retransmission never reads released storage.

`tools/sanitizers/build_http3.py` rebuilds only `neo._native._http3`
(`_http3module.c`, `http3_asgi.c`, `http3_connection.c`) with
ASan/UBSan against the same QUIC libraries as a `NEO_BUILD_HTTP3=1` build; the
sibling extensions are copied in unsanitized so the package stays importable.

```bash
uv run --no-sync python tools/sanitizers/build_http3.py
LD_PRELOAD=$(gcc -print-file-name=libasan.so) \
  ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
  UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
  PYTHONPATH=$(pwd)/.sanitizers/native-http3/lib \
  .venv/bin/python -m pytest tests/http3 -m '' -q
```

Result: the whole HTTP/3 suite is clean under ASan+UBSan, including the
streaming-response, acknowledgement-release, and body-limit cases that drive a
real QUIC client.

Re-running with `detect_leaks=1` reports ~246 KB in ~296 allocations, and **no
reported leak has a frame in `http3_asgi.c` or `http3_connection.c`** — every
stack is entirely inside libpython or `cryptography`'s Rust extension (the
self-signed certificate the tests generate). They are the same class as the
HTTP/1.1 and HTTP/2 `test_network.py` teardown artifacts. Nothing is
attributable to a closed stream. Prefer `detect_leaks=0` for routine runs, as
above, since pytest itself leaks megabytes under LSan.

## PostgreSQL coverage (native-c-hotspots plan, Step 4)

The control-message queue compacts an owned list behind a head index. Drive it
under the same sanitizers to prove compaction leaks no reference:

```bash
uv run --no-sync python tools/sanitizers/build_postgres.py
LD_PRELOAD=$(gcc -print-file-name=libasan.so) \
  ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
  UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
  PYTHONPATH=$(pwd)/.sanitizers/native-postgres/lib \
  .venv/bin/python -m pytest tests/postgres -q
```

Result: clean.

## Goal

Prove the C is memory- and UB-clean under AddressSanitizer + UndefinedBehavior
Sanitizer, add a fragmentation fuzzer, and document the build. Fix every defect
found; do not silence it.

## Steps

1. **Build a sanitizer variant of only the `_server` extension.**
   - Compile and link with `-fsanitize=address,undefined -fno-omit-frame-pointer -g -O1`.
   - Do it out-of-tree (a dedicated `setup.py build_ext` invocation with custom
     `CFLAGS`, or a small `Makefile` under the scratchpad) so the normal build
     is untouched.
   - ASan must see CPython's allocations: run under a Python built with ASan, or
     use `LD_PRELOAD=$(gcc -print-file-name=libasan.so)` together with
     `PYTHONMALLOC=malloc`.
   - Set `ASAN_OPTIONS=detect_leaks=1:halt_on_error=1` and
     `UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1`. Expect unrelated leaks
     from the interpreter itself; suppress them with an LSan suppressions file
     scoped to `_servermodule.c` frames.

2. **Drive it.** Run
   `pytest tests/test_server_protocol.py tests/test_server.py -k native` and the
   6000-request pipelined stress (GET + chunked POST over one keep-alive
   connection) under the instrumented interpreter.

3. **Add `tests/test_server_fuzz.py`.** Feed the native protocol a fake
   transport with randomized request streams: valid and malformed heads, split
   at every byte boundary, chunked bodies with bad sizes/CRLFs, oversized
   headers/bodies, and mid-request disconnects. Assert no crash, and that the
   native implementation never diverges in *status class* from the pure
   implementation on the same input. Save any crashing input as a fixture and
   add a targeted regression test.

4. **Fix all findings** in the C: refcounts, bounds, signed overflow,
   use-after-free on the resizable buffer, and timer/waiter retention.

5. **Document** the exact ASan/UBSan build and run commands in
   `docs/native/README.md`.

## Constraints

- Keep GIL assumptions as-is; do **not** add `Py_MOD_GIL_NOT_USED`.
- Compare pure vs native only on observable behavior (wire bytes, ASGI
  messages, closure), never internal object layout.
- Leave `src/neo/auth/` alone.

## Done when

- The native cases, the stress run, and the fuzzer all run clean under
  ASan/UBSan (no protocol-owned leaks or errors).
- Regression fixtures exist for any crash found.
- `uv run pytest`, `uv run ruff check .`, and `uv run ty check` still pass in
  the normal (non-sanitizer) build.
- The sanitizer build is documented in `docs/native/README.md`.
