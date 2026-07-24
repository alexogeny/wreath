# Audit rule reference

`wreath audit` runs a curated, zero-dependency ruleset over the HTML and responses your
app generates. This page lists every rule, its WCAG success criterion or performance
budget, its severity, and whether `--fix` can remediate it. See the
[auditing guide](../guides/auditing.md) for how to run it.

Severities: **error** fails the build; **warn** fails only under `--strict`; findings are
grouped by surface (`api-docs`, `static:<path>`, `runtime:<path>`, or `app`).

## Accessibility (WCAG 2.1 A/AA)

| Rule | Criterion | Severity | `--fix` | Checks / remediation |
|---|---|---|---|---|
| `html-lang` | 3.1.1 | error | ✅ | `<html>` has a non-empty `lang` (injects `lang="en"`) |
| `document-title` | 2.4.2 | error | — | a non-empty `<title>` |
| `img-alt` | 1.1.1 | error | ✅ | every `<img>` has `alt` (adds `alt=""`, flagged to describe) |
| `control-label` | 1.3.1 / 4.1.2 | error | — | inputs have a label / `aria-label` / wrapping `<label>` |
| `duplicate-id` | 4.1.1 | error | — | ids are unique within the document |
| `heading-order` | 1.3.1 | warn | — | exactly one `<h1>`; no skipped heading levels |
| `landmarks` | 1.3.1 / 2.4.1 | warn | — | a `<main>` landmark; buttons have an accessible name |
| `link-text` | 2.4.4 | warn | — | links have discernible, non-vague text |
| `table-headers` | 1.3.1 | warn | ✅ | data tables use `<th>`; `<th>` has `scope` (adds `scope="col"`) |
| `aria-valid` | 4.1.2 | warn | — | `aria-*` attributes and `role` tokens are known |
| `tabindex` | 2.4.3 | warn | ✅ | no positive `tabindex` (clamps to `0`) |
| `viewport-scale` | 1.4.4 | warn | ✅ | viewport doesn't disable zoom (strips the restriction) |
| `contrast` | 1.4.3 | warn | — | design-token fg/bg pairs meet 4.5:1 (normal) / 3:1 (large), per theme |

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

## Out of scope

By design the auditor does **not** cover runtime-only behaviour (keyboard focus traps,
ARIA live regions, motion/animation, anything needing a real browser or JS engine). An
N+1 query advisory is intentionally omitted: Wreath's ORM defaults relationships to
`load="raise"`, so accidental N+1 is structurally prevented rather than something to lint.
