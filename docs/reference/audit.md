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

## Adding a rule

A rule is a callable registered in one of three lists, all re-exported from
`wreath._audit.rules`:

| Registry | Module | Signature |
| --- | --- | --- |
| `A11Y_RULES` | `_audit/rules/a11y.py` | `(root: Node, surface: str) -> Iterator[Finding]` |
| `HTML_PERF_RULES` | `_audit/rules/perf.py` | `(root: Node, surface: str) -> Iterator[Finding]` |
| `RESPONSE_SECURITY_RULES` | `_audit/rules/security.py` | `(view: ResponseView) -> Iterator[Finding]` |

Each module has a local `_rule` decorator that appends to its registry, so adding
one is a decorated function next to its neighbours — no registration elsewhere.

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

Keep it decidable from static markup or a recorded response — see *Out of
scope* below — and keep it dependency-free, like the rest of `src/wreath`.

## Out of scope

By design the auditor does **not** cover runtime-only behaviour (keyboard focus traps,
ARIA live regions, motion/animation, anything needing a real browser or JS engine). An
N+1 query advisory is intentionally omitted: Wreath's ORM defaults relationships to
`load="raise"`, so accidental N+1 is structurally prevented rather than something to lint.
