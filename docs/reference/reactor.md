# `wreath.reactor`

The event loop behind the experimental `metal` tier: an `asyncio`-compatible
`SelectorEventLoop` that inline-drives non-suspending request coroutines, and
optionally backs every deadline with the native hashed timing wheel instead of
asyncio's timer heap.

## TLS in C

`metal_tls_context` and `metal_tls_client_context` return contexts whose crypto
runs in the reactor's own transport rather than in `asyncio.sslproto`.

```python
from wreath.reactor import metal_event_loop, metal_tls_context

ctx = metal_tls_context(certfile="cert.pem", keyfile="key.pem")
server = await loop.create_server(Protocol, "0.0.0.0", 443, ssl=ctx)
```

**Why they take paths and not an `ssl.SSLContext`.** There is no supported way
to borrow OpenSSL's `SSL_CTX *` out of a Python context, so a caller who hands
one in can only get asyncio's TLS. Naming the material lets the reactor build
its own — the same answer `TLSConfig` and the HTTP/3 backend already give.

Both are real `ssl.SSLContext` objects, so they are safe to pass anywhere: a
loop that does not recognise them simply uses the Python half.

Measured on one machine, one physical core, handshakes amortised:

| | req/s | CPU µs/req |
| --- | --- | --- |
| server, `asyncio.sslproto` | 21,500 | 46.5 |
| **server, native** | **46,000** | **21.8** |
| nginx | 48,200 | 20.8 |

A TLS connection previously left the metal tier altogether — asyncio's accept
loop, asyncio's transport, and a Python object per read and per write — so the
2.14× is mostly that, not the crypto.

Outbound is the same transport with `SSL_connect`, and verification is on by
default: both the chain and the host name are checked inside OpenSSL, and an IP
literal is matched against the certificate's `iPAddress` SAN rather than its DNS
names. `wreath.http_client` uses it automatically; see `ClientTLS`.

::: wreath.reactor
