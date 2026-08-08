# Auditing: accessibility & performance

Wreath ships `wreath audit` — a zero-dependency, offline auditor for the HTML and
responses your app generates. Because Wreath owns that output (the API-docs surface,
templates, static files), the audit is precise: it renders the exact bytes users receive
and checks them against a curated **WCAG 2.1 A/AA** ruleset plus a set of performance
budgets derived from your app's actual middleware stack.

## User story: keep an accessibility regression out of main

> *As an API author, my docs surface and static pages need to stay WCAG-clean. I
> want CI to fail the moment someone ships an image with no `alt` text or a
> contrast regression — without standing up a headless browser.*

```bash
wreath audit static app.main:app --strict
```

It renders the exact bytes your app serves and checks them against the WCAG 2.1
A/AA ruleset, exiting non-zero on any **error** so the pipeline blocks on a
regression. `--strict` promotes warnings to failures too; add `--json` for a
machine-readable report to annotate the PR. No browser, no network — the auditor
is offline and reasons about the server-generated markup directly.

## Run it

```bash
wreath audit static app.main:app
```

Point it at your application import target (`--factory` if it's an application factory).
It audits the API-docs surface by default; add static HTML trees with `--static`:

```bash
wreath audit static app.main:app --static public --static build/site
```

A clean run prints `no findings — clean.` Otherwise findings are grouped by surface,
worst-severity first:

```
api-docs
   WARN landmarks (WCAG 1.3.1): document has no <main> landmark
        → wrap primary content in <main>

app
   WARN compression-enabled (perf:compression): no CompressionMiddleware mounted; text responses are uncompressed
        → mount wreath.middleware.CompressionMiddleware

0 error(s), 2 warning(s)
```

## Wire it into CI

`wreath audit` exits non-zero when it finds an **error**, so it drops straight into a
pipeline — the same ergonomics as `wreath migrations check`:

```bash
wreath audit static app.main:app --strict
```

`--strict` promotes warnings to failures too. Add `--json` for a machine-readable report
(`{"summary": …, "findings": […]}`, stable key order) to annotate PRs.

## What it checks

**Accessibility (WCAG 2.1 A/AA).** Document language, page title, image `alt`, form
control labels, heading order, landmarks, link text, table headers, ARIA validity,
duplicate ids, positive `tabindex`, zoom-disabling viewports, and **colour contrast**
(1.4.3) computed from your design tokens across the light and dark themes. Each finding
cites its success criterion. The full list — with severity, WCAG SC, and whether `--fix`
can remediate it — is in the [audit rule reference](../reference/audit.md).

**Performance.** Whether `CompressionMiddleware`, `CacheControlMiddleware`, and
`SecurityHeadersPolicy` are mounted (checked by introspecting your app, not guessed),
plus document-size and OpenAPI-size budgets, missing image dimensions, render-blocking
`<head>` assets, and large un-nonced inline `<style>`/`<script>`.

Static HTML trees mounted via `app.static(...)` are discovered automatically; `--static`
adds extra directories.

## Auto-fix

`--fix` applies the **safe, semantics-preserving** subset via byte-offset splicing (it
never re-serialises the document, so formatting and CSP nonces survive): it injects a
missing `lang`, adds `alt=""` (flagged for you to describe), adds `scope` to `<th>`,
clamps a positive `tabindex` to `0`, and strips a zoom-disabling viewport. Static HTML
files are edited in place; for the *generated* API-docs surface it prints patch
suggestions to apply to the source, since that HTML is rebuilt each run. Everything else
stays suggestion-only.

```bash
wreath audit static app.main:app --fix
```

## Runtime mode

`wreath audit runtime <url>` audits a **running** server's live responses — the HTML body
plus response headers (compression, `Cache-Control`/`ETag`, CSP) — using only the standard
library:

```bash
wreath audit runtime http://localhost:8000 --strict
```

It also runs the **HTTP-compliance & security** rules against the actual bytes on the
wire: cookie flags and `__Host-`/`__Secure-` prefixes (RFC 6265bis), HSTS (RFC 6797), the
`401`→`WWW-Authenticate` and `405`→`Allow` MUSTs (RFC 9110), CORS
wildcard-with-credentials, and `nosniff`/`Referrer-Policy`. Point it at a staging or
production URL to get a compliance report for your own service — see the
[audit reference](../reference/audit.md#security-http-compliance-runtime) for every rule.

## Source mode

`wreath audit code <path>…` reads your application's own modules instead of its
output. The other two modes see what you *emit*, and the defects that do the
real damage leave no trace there — a query built by string formatting, a signing
key that is a literal, an authorization check that returns instead of refusing.
Every one of them serves a perfectly ordinary 200.

```bash
wreath audit code src --strict
```

It needs no application object and no running server, only paths, so it is the
one mode that runs on a diff:

```bash
wreath audit code $(git diff --name-only origin/main -- '*.py')
```

Test directories are skipped unless you pass `--tests`, because test code
legitimately hardcodes secrets, seeds PRNGs deterministically and compares
tokens with `==` — findings that are all correct and all useless.

Every finding names the primitive that replaces the defect rather than telling
you that something is dangerous:

```
src/stations/router.py
  ERROR outbound-url-from-request (CWE-918) 84:15: the destination of this
        request comes from registration
         → give the client a DestinationPolicy naming the hosts you mean; it
           checks every DNS answer and every redirect, not just the string you
           were handed
```

The ruleset is curated and deliberately quiet: a rule that fires on the correct
spelling of the same intent is worse than no rule, because a gate nobody can
keep clean is a gate everybody learns to pass. If one is wrong for a line you
have already reasoned about, an existing ruff `# noqa` for the equivalent
`flake8-bandit` code is honoured, and `# wreath-audit: allow <rule> -- <reason>`
covers the rest. The reason is required. See the
[audit reference](../reference/audit.md#source-level-security-code) for every
rule and for what keeps each one quiet.

## Dev middleware

Mount `AuditMiddleware` in **development only** to log a11y findings for every `text/html`
response as you browse. It is opt-in, never rewrites a response, and swallows its own
errors, so it can only ever add log lines:

```python
from wreath._audit import AuditMiddleware

if settings.dev:
    app.add_middleware(AuditMiddleware())
```

## Scope

The auditor reasons about **static, server-generated markup**. Runtime-only concerns —
keyboard focus traps, ARIA live regions, motion, or anything needing a real browser — are
out of scope by design, as is an N+1 query advisory (Wreath's ORM already defaults
relationships to `load="raise"`, so accidental N+1 is structurally prevented).
