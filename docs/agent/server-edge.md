# Edge server guidance

Read this before changing `wreath.edge`, reverse-proxy configuration, forwarding, or native edge protocol code.

## Native-only edge contract


- **`wreath.edge` has no Python path, and that is deliberate. Do not add one.**
  For a reverse proxy a Python fallback is a footgun rather than a safety net: it
  does not degrade gracefully -- it degrades *silently*, by roughly 6x, in
  the one component whose entire job is to be faster than the thing in front of
  it. Measured on this machine, per *physical core* with the load generator
  pinned elsewhere: `serve()` forwards 33,300-33,900 plaintext requests a second
  against nginx's 30,900-31,700, and 37,100-38,200 with TLS against nginx's
  36,800-37,600. The ASGI `ReverseProxy` manages 8,300. Nothing in a running
  system announces which one it took.

  **Quote per-core throughput, not per-process CPU.** Roughly a third of a
  proxy's core is kernel softirq handling packets, and it lands in no process's
  `utime`/`stime`. Per-process figures made this proxy look 22% cheaper than
  nginx when the honest answer is 7%. Two further traps that each cost a day:
  the load generator can cost more per request than the server (`oha` spent 22.5
  CPU-us driving something that costs 9.9), and nothing looks saturated until it
  is pinned -- every process sat at 0.6-0.7 of a core at every concurrency while
  the machine burned 4-5.

  So: the request path is C. `serve()` is a `loop.create_server` protocol that
  parses a head in place, picks an upstream from a compiled table, writes to a
  pre-warmed upstream transport and relays the response -- no scope, no
  `Request`, no coroutine, no Task. It takes **no ASGI app**, and that absence is
  load-bearing rather than an oversight: an app is the seam Python returns
  through. The Python that remains is configuration -- `Upstream`, `Ejection`,
  `UpstreamPool`, `DestinationPolicy` -- startup-only, compiled into the native
  structures once, costing nothing per request.

  **Do not migrate `ReverseProxy` leaf by leaf.** That was tried: moving the
  header transform into C -- the obvious hot leaf -- measured 113.2 against a
  110.1-117.2 baseline, which is nothing. The primitives were already native and
  the cost was materialising the message at all, which is why the answer was a
  protocol and not a faster function.

  Two rules follow, and they are what keep this from becoming the third state
  this file spends the rest of its length forbidding:

  * **Anything the native path cannot do yet is refused at configuration time,
    never at request time.** An upstream needing a feature that is not built
    raises when the pool is constructed, naming what is missing. Loud, at
    startup, in front of a developer -- never a silent slow path in production.
  * **Edge correctness uses an independent implementation as its oracle.** Run
    the differential corpus through haproxy and nginx, comparing forwarded
    bytes -- hop-by-hop stripping, `Connection` tokens, `Host` and
    `Content-Length` re-framing, and smuggling vectors.

  What refuses here is a *build* without the extension, and it refuses at import
  with a named error rather than degrading.
