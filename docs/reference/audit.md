# Audit rule reference

`wreath audit` runs a curated, zero-dependency ruleset over the HTML and responses your
app generates. This page lists every rule, its WCAG success criterion or performance
budget, its severity, and whether `--fix` can remediate it. See the
[auditing guide](../guides/auditing.md) for how to run it.

Severities: **error** fails the build; **warn** fails only under `--strict`; findings are
grouped by surface (`api-docs`, `static:<path>`, `runtime:<path>`, or `app`).

## Accessibility (WCAG 2.2 A/AA)

| Rule | Criterion | Severity | `--fix` | Checks / remediation |
|---|---|---|---|---|
| `html-lang` | 3.1.1 | error | ✅ | `<html>` has a non-empty `lang` (injects `lang="en"`) |
| `document-title` | 2.4.2 | error | — | a non-empty `<title>` |
| `img-alt` | 1.1.1 | error | ✅ | every `<img>`, `<input type=image>`, and `<area href>` has a text alternative |
| `control-label` | 1.3.1 / 4.1.2 | error | — | inputs have a label / `aria-label` / wrapping `<label>` |
| `duplicate-id` | 4.1.2 | error | — | ids are unique within the document |
| `heading-order` | 1.3.1 | warn | — | exactly one `<h1>`; no skipped heading levels |
| `landmarks` | 1.3.1 / 2.4.1 | warn | — | a `<main>` landmark; buttons have an accessible name |
| `link-text` | 2.4.4 | warn | — | links have discernible, non-vague text |
| `table-headers` | 1.3.1 | warn | ✅ | data tables use `<th>`; `<th>` has `scope` (adds `scope="col"`) |
| `aria-valid` | 4.1.2 | warn | — | `aria-*` attributes and `role` tokens are valid WAI-ARIA 1.2 |
| `tabindex` | 2.4.3 | warn | ✅ | no positive `tabindex` (clamps to `0`) |
| `viewport-scale` | 1.4.4 | warn | ✅ | viewport doesn't disable zoom (strips the restriction) |
| `contrast` | 1.4.3 | warn | — | design-token fg/bg pairs meet 4.5:1 (normal) / 3:1 (large), per theme |
| `non-text-contrast` | 1.4.11 | warn | — | form-control / focus borders meet 3:1 against the surface |
| `frame-title` | 4.1.2 | error | — | every `<iframe>` has a `title` (accessible name) |
| `autoplay` | 1.4.2 | warn | — | `<audio>`/`<video autoplay>` is `muted` or offers a control |
| `meta-refresh` | 2.2.1 | warn | — | no timed `<meta http-equiv=refresh>` |
| `focus-visible` | 2.4.7 | warn | — | CSS/inline styles don't remove the focus outline |

`contrast` resolves the CSS custom-property tokens in each inline `<style>` independently
for the light `:root`, `@media (prefers-color-scheme: dark)`, and `:root[data-theme=…]`
themes, and checks unambiguous foreground/background pairs (a `color` and `background` in
the same rule, or a semantic `var()` text colour on the base surface). It is advisory
(warn) because a static check can't know a run's text size or opacity; the reported band
(`fails normal text` vs `fails even large text`) tells you how far off the pair is.

## Performance

| Rule | Budget / check | Severity | Remediation |
|---|---|---|---|
| `compression-enabled` | `CompressionMiddleware` mounted (or `Content-Encoding` at runtime) | warn | mount `CompressionMiddleware` |
| `cache-control` | `CacheControlMiddleware` mounted (or `Cache-Control`/`ETag` at runtime) | warn | mount it / set `cache_control` on `static()` |
| `security-headers` | `SecurityHeadersMiddleware` mounted (or CSP at runtime) | warn | mount `SecurityHeadersMiddleware` |
| `html-size` | document ≤ 100 KiB | warn | trim or paginate |
| `json-size` | OpenAPI document ≤ 512 KiB | warn | trim descriptions/examples or split the API |
| `img-dims` | `<img>` has `width` + `height` (avoids layout shift) | warn | set explicit dimensions |
| `render-blocking` | no sync `<link rel=stylesheet>` / `<script src>` in `<head>` | warn | inline critical CSS; `defer`/`async` or move scripts |
| `inline-asset` | no large (>16 KiB) **un-nonced** inline `<style>`/`<script>` | warn | externalise, or add a CSP nonce if intentional |

The middleware checks introspect the loaded application's actual stack, so they never
false-positive on a mounted-but-differently-named middleware.

## Security & HTTP compliance (runtime)

`wreath audit runtime <url>` checks the *actual bytes on the wire* against the RFC
and secure-header rules that only a live response can prove. Point it at any
deployment of your service — these are first-class compliance checks for a SaaS
built on Wreath, not just for Wreath's own surfaces. Cookie rules see **every**
`Set-Cookie` (not just the last), and HSTS is only expected on `https://` URLs.

| Rule | Reference | Severity | Checks / remediation |
|---|---|---|---|
| `cookie-samesite-none-insecure` | RFC 6265bis 5.4.7 | error | `SameSite=None` cookies must be `Secure` (else browsers drop them) |
| `cookie-prefix` | RFC 6265bis 4.1.3 | error | `__Host-`/`__Secure-` cookies meet their attribute rules |
| `cookie-samesite` | OWASP | warn | every cookie sets a `SameSite` attribute (CSRF) |
| `cookie-secure` | OWASP | warn | cookies on HTTPS set `Secure` |
| `cookie-httponly` | OWASP | warn | cookies set `HttpOnly` unless a script must read them |
| `hsts` | RFC 6797 | warn | HTTPS responses send `Strict-Transport-Security` |
| `hsts-max-age` | RFC 6797 | info | HSTS `max-age` ≥ 180d (1y for preload) |
| `content-type-options` | OWASP | warn | `X-Content-Type-Options: nosniff` present |
| `www-authenticate` | RFC 9110 15.5.2 | error | a `401` carries `WWW-Authenticate` |
| `allow-header` | RFC 9110 15.5.6 | error | a `405` carries `Allow` |
| `cors-credentials` | Fetch (CORS) | error | no `Access-Control-Allow-Origin: *` with `Allow-Credentials: true` |
| `referrer-policy` | OWASP | info | a `Referrer-Policy` header is set |

Mounting `SecurityHeadersMiddleware` clears the header-based warnings; the cookie
and status-code rules are satisfied by how your handlers set cookies and raise
`Unauthorized(challenge=…)` / `MethodNotAllowed(allow=…)`.

## Source-level security (code)

`wreath audit code <path>…` reads your application's own modules. The other two
tiers look at what an application *emits* — rendered HTML, live response headers
— and neither can see the defect classes that do the real damage, because those
leave no trace in a correct-looking 200. A query built by string formatting, a
signing key that is a literal, an authorization check that returns instead of
refusing: all of them serve a perfectly ordinary response.

This tier needs no application object and no running server, only paths, so it
runs on a diff in CI. Test directories are skipped unless you pass `--tests`,
because test code legitimately hardcodes secrets, seeds PRNGs deterministically
and compares tokens with `==`.

| Rule | Reference | Severity | Checks |
|---|---|---|---|
| `sql-interpolation` | CWE-89 | error | SQL built by string interpolation reaches the database unmodified |
| `timing-unsafe-compare` | CWE-208 | error | a secret compared with `==` leaks its prefix through response timing |
| `weak-randomness` | CWE-338 | error | a security value drawn from `random`, which is a predictable Mersenne Twister |
| `hardcoded-secret` | CWE-798 | error | a signing key or password written as a string literal, passed **or declared** |
| `ssrf-policy-widened` | CWE-918 | error | an outbound client permitted to reach private, loopback or link-local addresses |
| `outbound-url-from-request` | CWE-918 | error | an outbound request whose destination the caller chose |
| `unsafe-xml-parser` | CWE-611 | error | XML read with a parser that resolves external entities |
| `template-from-request` | CWE-1336 | error | a template compiled from a value that is not a literal |
| `dynamic-import` | CWE-470 | error | a module, attribute or expression resolved from data |
| `unsafe-archive-extract` | CWE-22 | error | an archive extracted without member, size, ratio or symlink limits |
| `mass-assignment` | CWE-915 | error | a request body walked onto an object with `setattr` |
| `cors-reflect-origin` | CWE-942 | error | the request `Origin` reflected into `Access-Control-Allow-Origin` |
| `wildcard-trust-list` | CWE-346 | error | a trust boundary configured to accept every peer |
| `secret-in-log` | CWE-532 | error | a credential, or a caller's own body, formatted into a log record |
| `authz-fail-open` | CWE-863 | error | an authorization check that returns, rather than refuses, when it cannot decide |
| `auth-disable-flag` | CWE-1188 | error | a configuration flag that skips authentication entirely |
| `auth-fallback-on-exception` | CWE-1390 | error | an authentication path that retries with a weaker verifier when the strong one raises |
| `substring-security-match` | CWE-697 | error | a security decision made by substring, so a longer value satisfies a shorter rule |
| `case-mapped-authz` | CWE-178 | warn | an authorization decision made after a Unicode case mapping |
| `debug-enabled` | CWE-489 | warn | the application constructed with `debug=True` as a literal |
| `untrusted-forwarded-header` | CWE-348 | warn | a forwarded client address read without a configured proxy trust boundary |
| `error-detail-leaked` | CWE-209 | warn | a caught exception's own text returned to the caller |
| `env-conditional-security` | CWE-1188 | warn | a security control whose strength depends on which environment is running |
| `unparseable` | `wreath:audit` | warn | the file could not be parsed, so no rule could be applied to it |

Every finding names the Wreath primitive that replaces the defect, because a
finding with no remediation is a complaint. `--fix` does not apply to this tier:
these are decisions, not markup.

### What makes it quiet

A security linter that cries wolf gets suppressed wholesale, and then it is
worse than nothing, because the suppression outlives the person who understood
it. Three properties keep this ruleset usable, and each was arrived at by
sweeping it over `src/wreath` and `example/` and narrowing whatever fired:

* **Taint starts at the route boundary.** A handler is identifiable from its
  decorator alone, so its parameters are known to be caller-controlled without
  an application object. Interpolating a *module constant* into SQL is how you
  write a schema-qualified statement; interpolating a *handler parameter* is an
  injection. The first draft of this tier did not separate them and reported 103
  findings against Wreath's own source.
* **Provenance decides, not shape.** `"admin" in roles` over a collection and
  `"admin" in scope_string` are one shape and two different pieces of code; the
  rule fires only where the file itself declares the subject to be a string.
* **A narrow `except` is trusted.** If you named the type you caught, you know
  what its message says because you wrote it, so `error-detail-leaked` fires
  only on a broad handler — the one that cannot know what it is holding.

The current sweep is **zero findings** from these rules against `src/wreath` and
`example/`. Two pre-existing findings remain and are listed in the audit
subsystem's notes rather than suppressed.

### Waivers

A finding your project has already declared and justified is not reported again.
Where ruff's `flake8-bandit` has an equivalent code, an existing `# noqa` for it
is honoured — `S608` for `sql-interpolation`, `S105`/`S106`/`S107` for
`hardcoded-secret`, `S102`/`S307` for `dynamic-import`, `S311` for
`weak-randomness`, `S314`/`S405`/`S320` for `unsafe-xml-parser`, `S202` for
`unsafe-archive-extract`. Re-raising something you have already declared under a
second name is how the second tool gets switched off.

For a finding ruff has no code for, the audit has its own marker, and the reason
is required:

```python
# wreath-audit: allow case-mapped-authz -- the list is ASCII by construction
```

A bare marker with no reason is itself a finding.

## Adding a rule

A rule is a callable registered in one of four lists, all re-exported from
`wreath._audit.rules`:

| Registry | Module | Signature |
| --- | --- | --- |
| `A11Y_RULES` | `_audit/rules/a11y.py` | `(root: Node, surface: str) -> Iterator[Finding]` |
| `HTML_PERF_RULES` | `_audit/rules/perf.py` | `(root: Node, surface: str) -> Iterator[Finding]` |
| `RESPONSE_SECURITY_RULES` | `_audit/rules/security.py` | `(view: ResponseView) -> Iterator[Finding]` |
| `CODE_RULES` | `_audit/rules/code.py` | a `CodeRule` row plus detection in `_Scanner` |

The first three modules have a local `_rule` decorator that appends to their
registry, so adding one is a decorated function next to its neighbours — no
registration elsewhere. The source tier is one AST walk, so a rule there is a
row in `CODE_RULES` carrying its CWE and remediation, plus the detection in
`_Scanner`.

Four things go with it, and a rule is not finished without all four:

1. **The rule**, decorated with `_rule`, yielding a `Finding` with its id,
   `Severity`, surface, message, the criterion or specification it cites, and a
   `suggestion` when `--fix` can remediate it.
2. **A row in the table above**, in the matching section. This page is the
   catalogue; a rule missing from it is a rule nobody can look up.
3. **A test in `tests/audit/`**, covering the finding *and* the markup that must
   not trigger it. False positives are the failure mode that gets a linter
   switched off — the ARIA attribute table in `a11y.py` is narrow for exactly
   this reason, and says so.
4. **A severity you can defend.** `error` fails the build; `warn` fails only
   under `--strict`; `info` reports. Prefer the lower one until the rule has
   been wrong a few times and stayed right.

For a source rule, the quiet half needs more than an `assert_clean`, because one
passes trivially against a rule that never fires at all. Two checks establish it:
`wreath audit code src/wreath example` must stay at zero, and
`wreath mutant --path src/wreath/_audit/rules/code.py --changed <ref>` must kill
the guards you wrote. A surviving mutant on a precision guard means no test
holds that guard down, and the rule can be widened back to noise without anything
going red.

Keep it decidable from static markup or a recorded response — see *Out of
scope* below — and keep it dependency-free, like the rest of `src/wreath`.

## Out of scope

By design the auditor does **not** cover runtime-only behaviour (keyboard focus traps,
ARIA live regions, motion/animation, anything needing a real browser or JS engine). An
N+1 query advisory is intentionally omitted: Wreath's ORM defaults relationships to
`load="raise"`, so accidental N+1 is structurally prevented rather than something to lint.
