# Native server and protocols

Wreath is a normal ASGI application, so it will happily run behind Uvicorn or any
other server you already trust. But it also carries its own, and this is where
the "native" in Wreath earns its keep: an HTTP/1.1, HTTP/2, and optional HTTP/3
server that moves the parsing and dispatch hot path into C, on top of an asyncio
(or uvloop) transport.

The simplest way to run it is from the command line:

```bash
wreath run app:app --host 0.0.0.0 --port 8000
```

From Python you build a validated `ServerConfig` and hand it to `run`:

```python
from wreath.server import run, ServerConfig, TLSConfig

config = ServerConfig(host="0.0.0.0", port=8000, protocols=("http/1.1", "h2"))
run(app, config=config, tls=TLSConfig("cert.pem", "key.pem"))
```

## Choosing protocols

HTTP/2 and HTTP/3 need the native extension. A listener that offers both
`http/1.1` and `h2` negotiates between them over TLS ALPN, so a client gets the
best protocol it supports and older clients still work. HTTP/3 is compiled in
only when the extension is built with `WREATH_BUILD_HTTP3=1`, because it pulls in
a QUIC stack you shouldn't pay for unless you want it.

## Configuring from the environment

The same application should run differently in development and production without
code changes. `ServerConfig.from_env()` layers `WREATH_*` environment variables
over the defaults, and any argument you pass explicitly wins over the
environment. To catch a missing secret before it causes a mysterious failure
mid-request, name your boot-critical variables and Wreath will warn at startup:

```python
from wreath.server import run
run(app, required_env=["DATABASE_URL"])
```

The [Configuration and state](config-state.md) guide covers this in full.

**Reference:** [`wreath.server`](../reference/server.md).
