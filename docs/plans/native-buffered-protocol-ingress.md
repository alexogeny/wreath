# Native buffered HTTP/1 ingress plan

## Goal

Make native `Http1Protocol` a real `asyncio.BufferedProtocol` so socket transports receive directly into Neo's C-owned request buffer, eliminating the unconditional `data_received()` buffer copy while preserving HTTP parsing, ASGI behavior, backpressure, TLS, and the pure-Python fallback.

## Repository constraints

- Target CPython 3.14 only.
- Keep `src/neo` dependency-free.
- Preserve native/pure observable parity.
- Keep `data_received()` as a compatibility ingestion path for direct tests and transports that invoke it.
- Do not reallocate or free C memory while exported through the buffer protocol.
- Validate C changes with protocol tests, fuzz tests, ASan/UBSan, and repeated benchmarks.

## Implementation plan

### 1. Make the native type an actual `asyncio.BufferedProtocol`

Update `src/neo/_native/_servermodule.c`:

- Import `asyncio.BufferedProtocol` during module initialization.
- Replace `PyType_FromSpec(&http_protocol_spec)` with type creation using `BufferedProtocol` as a base, such as `PyType_FromSpecWithBases`.
- Include the imported base in all initialization cleanup paths.
- Keep the public names `Http1Protocol` and `HttpProtocol` unchanged.

This inheritance is required because asyncio selects buffered transport behavior using `isinstance(protocol, BufferedProtocol)`, not merely method presence.

### 2. Add explicit receive-export state

Extend `NeoHttpProtocol` in `src/neo/_native/server.h` with fields similar to:

```c
Py_ssize_t read_offer_offset;
Py_ssize_t read_offer_size;
Py_ssize_t read_exports;
int compact_pending;
int buffer_update_pending;
```

Required invariants:

- `read_offer_offset + read_offer_size <= buf_cap`.
- `buf_len` advances only in `buffer_updated()` for buffered reads.
- `buf_reserve()` must not call `PyMem_Realloc` while `read_exports > 0`.
- `connection_lost()` must not free exported memory.
- Only one outstanding transport read offer is accepted at a time.
- Parser consumption cannot invalidate the currently exported address.

Document these invariants next to the fields.

### 3. Expose the C buffer safely through the buffer protocol

Add buffer slots to `http_protocol_spec` in `src/neo/_native/server_http1.c`:

- `Py_bf_getbuffer`
- `Py_bf_releasebuffer`

The exported region should be only the unused writable tail:

```text
buf + buf_len ... buf + buf_cap
```

The exporter must:

- provide a writable, contiguous byte buffer;
- retain the protocol through `Py_buffer.obj`;
- increment `read_exports`;
- decrement it in `releasebuffer`;
- reject an export when no read offer is active;
- never expose consumed or already-filled bytes.

Do not use an ownerless `PyMemoryView_FromMemory`: it could outlive the protocol and leave a dangling pointer.

### 4. Implement `get_buffer(sizehint)`

Add `Http1Protocol.get_buffer(sizehint)`:

1. Reject overlapping read offers.
2. Apply any deferred compaction before exporting.
3. Ensure a non-empty writable tail exists.
4. Grow the C buffer before creating the export.
5. Record the offered offset and length.
6. Return the protocol itself, or an owned memoryview over its buffer-protocol export.

Use a bounded receive target rather than blindly honoring a huge `sizehint`. The initial private policy should be:

- minimum allocation: the existing 4096-byte baseline;
- normal receive offer: 64 KiB;
- honor smaller positive hints when practical;
- grow geometrically using the existing overflow checks;
- never return a zero-length buffer, as asyncio treats that as an error.

Keep this receive-chunk policy private until measurements justify configuration.

### 5. Implement `buffer_updated(nbytes)`

Add `Http1Protocol.buffer_updated(nbytes)`:

- require an active offer;
- validate `0 <= nbytes <= read_offer_size`;
- advance `buf_len` by exactly `nbytes`;
- clear the active offer;
- invoke `run_drive(self)`;
- propagate parser errors using the same close/error behavior as `data_received()`.

A zero-byte update should be harmless and must not invoke undefined pointer behavior.

Do not resize the buffer from inside `buffer_updated()` while its export may still be alive.

### 6. Defer compaction while exported

Modify `do_consume()` in `src/neo/_native/server_http1.c`:

- Retain the current cheap reset when fully drained, but do not move or resize memory during an active export.
- When the existing 64 KiB compaction condition is met while exported, set `compact_pending`.
- Perform the `memmove` at the start of the next `get_buffer()`, after confirming `read_exports == 0`.

Parsing may mutate bytes logically through cursor movement, but exported addresses must remain stable.

### 7. Refactor `data_received()` around shared ingestion

Keep `data_received()` registered for:

- direct protocol/fuzz test harnesses;
- compatibility with unusual transports;
- easier native/pure parity testing.

Extract shared behavior so `data_received()`:

1. ensures there is no active buffered read;
2. reserves space;
3. copies the supplied bytes;
4. advances `buf_len`;
5. calls the same parser-driving helper as `buffer_updated()`.

The production asyncio socket path should use `get_buffer()`/`buffer_updated()` and therefore avoid this copy.

### 8. Harden shutdown and error handling

Update connection and GC paths in `src/neo/_native/server_http1.c`:

- `connection_lost()` clears active-offer state but does not free exported memory.
- `clear`/`dealloc` assert or safely handle `read_exports`.
- Initialization failures reset all offer/export fields.
- Parser exceptions from `buffer_updated()` close consistently with exceptions from `data_received()`.
- A second `get_buffer()` before release/update fails deterministically rather than reallocating.
- Invalid or oversized `buffer_updated()` counts raise an appropriate exception and close the connection if necessary.

The protocol object should remain alive naturally through `Py_buffer.obj` until all exported views are released.

## Tests

### Native buffer API tests

Add focused tests in `tests/test_server_protocol.py`:

- Native `Http1Protocol` is an `asyncio.BufferedProtocol`.
- `get_buffer(-1)`, `get_buffer(0)`, and positive size hints return writable non-empty buffers.
- Writing a fragmented request into returned buffers and calling `buffer_updated()` invokes the app correctly.
- Multiple keep-alive requests reuse the allocation.
- Pipelined requests survive deferred compaction.
- Invalid negative or oversized update counts are rejected.
- Overlapping read offers are rejected.
- `connection_lost()` while a view exists does not cause use-after-free.
- A released view permits later growth and compaction.

### Exercise buffered ingress in existing harnesses

Update direct feeders in:

```text
tests/test_server_fuzz.py
tests/test_server_protocol.py
tests/test_server_websocket.py
```

Introduce a small test helper:

```python
def feed(protocol, data: bytes) -> None:
    if isinstance(protocol, asyncio.BufferedProtocol):
        target = memoryview(protocol.get_buffer(len(data)))
        target[:len(data)] = data
        protocol.buffer_updated(len(data))
        target.release()
    else:
        protocol.data_received(data)
```

Run important native tests through both:

- buffered ingestion;
- retained `data_received()` compatibility path.

Cover:

- fragmented request lines and headers;
- fixed and chunked bodies;
- oversized heads and bodies;
- pipelining;
- read pause/resume;
- WebSocket upgrade plus buffered frames;
- malformed fuzz corpus.

### Real transport coverage

Extend `tests/test_server.py` with real loopback tests proving:

- plain TCP GET and POST work through the buffered native protocol;
- TLS requests still work;
- keep-alive and pipelining remain correct;
- large request bodies are not truncated when the offered buffer fills;
- disconnect during a partial request releases the protocol.

Run with the default asyncio loop and uvloop when the optional benchmark environment provides it.

## Benchmark proof

Measure before and after without changing parser or response behavior:

- constant GET;
- fragmented GET;
- 1 KiB, 64 KiB, and 1 MiB POST echo;
- keep-alive at multiple concurrency levels;
- plain TCP and TLS separately.

Record repeated trials for:

```bash
uv run python -m benchmarks.run --framework neo-native
```

Add or use a focused ingress benchmark that records:

- throughput;
- median/p95/p99;
- bytes received;
- `data_received()` copy count;
- buffer growth count;
- compaction count;
- peak retained input-buffer capacity.

The key proof is not only throughput: production socket traffic should show zero `data_received()` copies.

## Validation

Run:

```bash
uv run pytest tests/test_server.py tests/test_server_protocol.py \
  tests/test_server_fuzz.py tests/test_server_websocket.py

uv run ruff check .
uv run ty check
uv run python tools/sanitizers/build_server.py
```

Run the native malformed-input and disconnect corpus against the sanitized extension. Specifically check for use-after-free, realloc-with-export, buffer overflow, and leaked protocol objects.

## Documentation

Update:

```text
docs/guides/server.md
docs/internals/performance.md
docs/concepts/request-lifecycle.md
```

Document that:

- native HTTP/1 uses `BufferedProtocol` for direct socket reads;
- `data_received()` remains an internal compatibility path;
- receive buffers are protocol-owned and cannot be recycled while exported;
- this optimization does not change portable ASGI behavior.

## Acceptance checks

- Native `Http1Protocol` is recognized by asyncio as a `BufferedProtocol`.
- Real TCP traffic enters through `get_buffer()`/`buffer_updated()`.
- No ingress `memcpy` occurs on that production path.
- Generic ASGI and Neo-native applications retain identical behavior.
- Fragmented, pipelined, chunked, TLS, WebSocket, backpressure, and disconnect tests pass.
- Buffer allocation cannot move or be freed while exported.
- Sanitizers report no memory errors.
- Repeated benchmarks retain raw before/after results; no performance claim is made from a single run.
