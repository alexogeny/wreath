# Prescriptive plan: generalize the stream-fusion C API (postgres as the second implementer)

Status: implemented (July 2026) — `wreath_stream.h`, probe table in
`reactor_transport.c`, pg capsule + fused egress in `postgres/protocol.c`,
tests in `tests/postgres/test_stream_fusion.py`. The crossing-count red test
described below was replaced by fusion-state getters (`_fused_stream`,
`_direct_protocol_writes`): `sys.setprofile` cannot observe C→C calls, so the
plan's original observable was unmeasurable as specified.

The HTTP client followed as the third implementer (July 2026), but not
mechanically: the client had no C transport-facing protocol at all (it used
asyncio streams), so `Http1ClientStream` in `client_http1.c` now provides a
C-owned stream reader/protocol (StreamReader-shaped awaitable reads with
asyncio exception semantics, writer backpressure) that implements the capsule;
`http_client.py` swaps `asyncio.open_connection` for it when available
(`WREATH_CLIENT_NATIVE_STREAM=0` restores streams). Tests in
`tests/test_http_client_native_stream.py`; the pre-existing client suites are
the parity net and run through the new reader on every loop, TLS included.

Related material:

- `AGENTS.md`
- `docs/plans/native-buffered-protocol-ingress.md` (prior art: built the http1 seam this plan generalizes)
- `docs/agents/request-boundary-baseline.json`
- `src/wreath/_native/server.h` (`WreathHttp1CAPI`, `WreathTransportCAPI`)
- `src/wreath/_native/reactor_transport.c` (`st_bind_protocol_methods`, `st_deliver_received`)
- `src/wreath/_native/postgres/protocol.c` (`WreathPgBufferedProtocol`)

## Goal

The metal transport already delivers ingress to native HTTP/1 with zero Python
object traffic through the `WREATH_HTTP1_CAPI` capsule
(`check` / `acquire_read_buffer` / `commit_read` / `feed_external`). Nothing in
that seam is HTTP-specific. Generalize it into a `WreathStreamCAPI` that any
native protocol can register, and make the PostgreSQL driver the second
implementer, so DB wire traffic stops paying the Python `BufferedProtocol`
calling convention on every socket read.

## What the seam costs today (per DB read event, metal loop)

`st_deliver_received`'s buffered branch performs, per chunk:

1. `PyLong_FromSsize_t` (requested size) — object allocation
2. `PyObject_CallOneArg(proto_get_buffer)` — Python calling convention into a C
   method, which allocates a **memoryview** (`wreath_pg_slab_writable_view`)
3. `PyObject_GetBuffer(PyBUF_WRITABLE)` + `PyBuffer_Release`
4. `memcpy` provided buffer → pg slab (this copy is inherent; pg retains slab
   memory in records/windows, unlike http1's borrow — it stays)
5. `PyLong_FromSsize_t(chunk)` — second allocation
6. `PyObject_CallOneArg(proto_buffer_updated)` — second convention call

Outbound, `buffered_connection_made` caches the bound `transport.write` and pays
one Python-convention call per query dispatch, even though the reactor already
exports `WREATH_TRANSPORT_CAPI` with direct C `write`/`writelines` (the fused
http1 server uses it; pg does not).

Fusing removes items 1–3 and 5–6 on ingress and the bound-method call on
egress; pg queries then also ride the metal async-send queue (a pipelined batch
of query submissions leaves in one `io_uring_enter`).

## Design

### 1. `WreathStreamCAPI` (extract, do not redesign)

- In `server.h`, rename the struct to `WreathStreamCAPI` with
  `typedef WreathStreamCAPI WreathHttp1CAPI;` for source compatibility. The
  shape stays byte-identical to v2 (`version`, `check`,
  `acquire_read_buffer`, `commit_read`, `feed_external`) — no ABI break, no
  version bump.
- The transport replaces `g_http1_capi` + `int fused_http1` with a small
  static probe table of capsule names, tried lazily at
  `st_bind_protocol_methods` (missing modules `PyErr_Clear`, same as
  `load_http1_capi` today):

  ```c
  static const char *stream_capsules[] = {
      WREATH_HTTP1_CAPI_NAME,                       /* first implementer */
      "wreath._native._postgres._STREAM_C_API",     /* this plan */
      "wreath._native._client._STREAM_C_API",       /* future */
  };
  ```

  The transport stores `const WreathStreamCAPI *fused` and the first capsule
  whose `check(protocol)` passes wins. `_fused_http1` keeps its current
  meaning (fused *and* the http1 capsule); add a `_fused_stream` getter
  returning the capsule name or None for diagnostics.
- A static list is chosen over a registration function in the core capsule:
  no init-order coupling, no new machinery, fails soft. Revisit only if a
  third-party protocol ever needs to register.

### 2. Postgres implements the capsule

In `postgres/protocol.c`, export `_STREAM_C_API` from `_postgresmodule.c`:

- `check` — `PyObject_TypeCheck` against `WreathPgBufferedProtocol`.
- `acquire_read_buffer` — the logic of `buffered_get_buffer` minus the
  memoryview: `reclaim_retired(8)`, rotate/acquire slab, return
  `current->data + write_position` and the remaining slab capacity.
- `commit_read(n)` — bounds-check, `write_position += n`, `parse_messages()`.
- `feed_external(data, n)` — reserve slab tail (rotating as needed), memcpy,
  `parse_messages()`. Postgres must copy (records retain slab memory beyond
  the callback), so this is the same single copy the memoryview path already
  paid — the saving is the object churn and calling convention.

Invariants (document beside the fields, mirroring the http1 offer contract):

- Only one outstanding offer; `acquire` while an offer is live is an error.
- The offered slab must not be retired/reclaimed while the offer is live
  (`reclaim_retired` runs only inside `acquire`, before the new offer — keep
  it that way; add an `offer_live` flag asserted in retirement paths).
- Abandon a failed read by committing 0 (clears the offer, parses nothing).
- Pg slabs are stable allocations (never realloc'd), so no export pinning
  beyond the offer flag is needed.

Keep the Python `get_buffer`/`buffer_updated` methods unchanged as the
fallback: non-metal loops, the pure tier, and direct-call tests preserve
observable parity.

### 3. Outbound through the transport C API

`buffered_connection_made` probes `WREATH_TRANSPORT_CAPI` (already exported);
when the transport passes its `check`, stash the `write` function pointer and
bypass the bound method. Fall back to the current bound-method path otherwise.
Exact-`bytes` query payloads then enter the metal egress queue zero-copy and
batch with response sends.

### 4. Explicitly out of scope (separate plans)

- Native future kit (C-settable operation futures) — next plan; this one keeps
  `create_future`/`set_result` as-is.
- HTTP client as third implementer — mechanical once this lands.
- Any change to pg message parsing, slab sizing, or pipeline semantics.

## Test-first work items

Each red test must fail for the stated reason before production code changes.

1. **Crossing-count red test** (`tests/postgres/test_stream_fusion.py`):
   drive one `fetch` round trip against the `FakePostgres` fixture
   (`tests/postgres/test_connection.py`) on a metal loop, counting C-entry
   crossings and object allocations across the read event with
   `sys.setprofile`. Assert the fused budget (no `get_buffer`/
   `buffer_updated` calls observed). Fails today because the buffered path
   calls both.
2. **Parity**: the full `tests/postgres/` suite run with a metal-loop fixture,
   fused and with fusion disabled (non-metal loop) — identical results.
3. **Lifecycle red tests**: connection_lost with a live offer; slab rotation
   mid-message; a DataRow spanning two read events; pipelined queries across
   one drain batch; offer abandonment on a failed read (commit 0).
4. **Transport-side**: `_fused_stream` reports the pg capsule; `_fused_http1`
   unchanged for http1; a protocol matching no capsule still takes the
   Python buffered path.
5. **Sanitizers**: ASan/UBSan pass per repo convention (offer lifetime is the
   use-after-free class to hunt).
6. Record the new crossing numbers in the test as exact expected counts
   (deterministic), in the spirit of `request-boundary-baseline.json`;
   `wreath-request-trace` scenarios stay scripted-connection by design.

## Risks

- **Offer lifetime vs slab retirement** — the one memory-safety risk; covered
  by the invariant flag plus lifecycle tests and sanitizers.
- **Capsule probe order/absence** — probing is lazy and clears errors; a
  worker that never imports `_postgres` never pays the probe more than once.
- **Behavioral drift between fused and fallback paths** — parity suite run
  both ways is the guard.

## Estimated shape

- `server.h` + `reactor_transport.c`: ~80 lines (typedef, probe table, fused
  pointer, getter).
- `postgres/protocol.c` + `_postgresmodule.c`: ~150 lines (capsule fns, offer
  flag, transport-capi write).
- Tests: ~250 lines.
- No Python-side changes; no new configuration.
