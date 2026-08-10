"""Executable compliance spec for wreath — RFC / WCAG / ASGI, codified.

Each test names the clause it enforces, so this suite *is* the compliance
record (it replaced the former docs/agents/compliance-map.md tracking doc). If a
requirement is not asserted here, treat it as unverified.

Layout:
- test_http.py            — RFC 9110/9111/9112 framing, status MUSTs, Date, cookies (6265bis)
- test_websocket.py       — RFC 6455 close codes (wire-level MUSTs: tests/test_server_websocket.py)
- test_accessibility.py   — WCAG 2.2 A/AA, via the audit rule set
- test_security_headers.py — response security/compliance headers (the runtime auditor)
- test_jwt_ec.py          — JWT ES256 / EdDSA vs RFC vectors + a `cryptography` oracle
- test_asgi.py            — ASGI 3.0 message shapes/ordering, lifespan, scope conformance

No known mandatory (MUST / Level-A) gaps remain unenforced. Deliberate
non-goals, by design rather than oversight:
- The 1.3.1-structural a11y checks (heading-order, landmarks) stay WARN: they are
  heuristics with valid exceptions, and a build-failing false positive is what
  gets a linter switched off. Teams wanting strict Level-A run `wreath audit
  --strict`, which fails on warnings too.
"""
