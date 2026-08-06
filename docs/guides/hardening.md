# Hardening

There is a category of defect that a framework can close on your behalf, and
Wreath closes a great deal of it. A key that goes through
`wreath.objects.normalize_key` cannot traverse. A document that goes through
`wreath.xml.parse` cannot expand an external entity. A query compiled from a
`Select` cannot be injected into. A cookie signed by `SessionMiddleware` cannot
be forged. None of that is advice; it is what the code does.

And none of it is compulsory. An application can always reach around the
framework — and when it does, every one of those defects comes back exactly as
it was:

```python
sql = f"SELECT ... WHERE reference ILIKE '%{needle}%'"   # and there it is again
```

## The problem is not detection

Wreath has been able to find that line for a while: `wreath audit code` reads an
application's own modules and reports the classes that leave no trace in a
correct-looking 200 — SQL assembled by formatting, a signing key written as a
literal, a token drawn from `random`, an archive extracted without vetting its
members, an authorization check that returns rather than refusing when it cannot
decide.

The problem was that **nothing ran it**. An audit is a command somebody has to
remember, on a machine somebody has to configure, at a moment somebody has to
choose. The defects it finds are precisely the ones that do not announce
themselves in a test, a review, or a response body — so the audit is most likely
to be skipped exactly where it was most needed.

So it moved to the one moment in an application's life that is not optional:

```python
app = Wreath(hardening="block")     # this application does not boot carrying one
```

## The three settings

`WREATH_HARDENING` in the environment overrides whatever the code asked for,
because turning this up — or off — must not require a deploy.

| policy | at startup |
| --- | --- |
| `warn` | every finding is logged; the application starts. **The default.** |
| `block` | the error-level findings are raised; the application does not start. |
| `off` | nothing is scanned. |

`warn` is the default rather than `block`, and that is a decision about adoption
rather than about severity. A framework that refuses to start an application it
has never seen before, over a rule that application's author has never read, is
a framework that gets pinned to the previous version — and then the rules
protect nobody at all. `warn` puts the findings in front of the person who can
fix them, on the first run, with no configuration.

`block` is what a deployment turns on once its findings are at zero, and it is
the setting that makes the whole thing worth having. After it, the defect cannot
reach production, because the process carrying it will not come up.

Only **error**-level findings block. A warn-level rule is one whose correct form
is a judgement call — `case-mapped-authz` and `debug-enabled` are both of those
— and refusing to boot over a judgement call is how `block` stops being a
setting anybody is willing to turn on. Warnings are still logged, right beside
the errors that did block.

## Moving a deployment from warn to block

1. Start it. `warn` is already on, so the first run prints the worklist.
2. Fix what it names. Every finding carries the wreath primitive that makes its
   defect unwritable, not just a statement that the line is dangerous — the
   remedy for `sql-interpolation` is [Safe SQL](safe-sql.md), the remedy for
   `path-from-request` is `normalize_key` and a `wreath.storage` backend.
3. Waive what genuinely does not apply, in place, with a reason:

   ```python
   key = _FIXTURE_KEY  # wreath-audit: allow hardcoded-secret -- test fixture, never deployed
   ```

   The reason is required. "We know" is not a reason, and a bare waiver is how a
   rule set stops meaning anything. There is no file-level or
   application-level waiver, deliberately: those drift away from the code they
   were written about, and then they are switching off a rule nobody remembers
   agreeing to.
4. Set `hardening="block"`, or `WREATH_HARDENING=block` in the environment for
   production only.

## What gets read

**The source tier** reads the application's own code, found from where its
handlers were defined — a package root when the handlers live in a package, the
module itself when they do not. Site packages, the standard library and wreath's
own tree are never scanned, so you are never shown a finding you have no move
against. The result is cached per file against its size and modification time,
so a process that starts several applications parses each module once.

**The configuration tier** reads the live application object instead: the
registered outbound clients and their destination policies. This is the half a
source rule structurally cannot see. Written out, `allow_private=True` is
obvious; written as `allow_private=settings.ALLOW_PRIVATE_FETCH`, the source
says nothing at all and the object at startup says everything.

## The bound worth knowing

This is static analysis over one module at a time. A value laundered through a
helper in another file will not be followed, and no promise is made that it
will. That is a reason to keep the safe API the easy one — which is what
`wreath.sql`, `wreath.objects.normalize_key` and `wreath.xml` are for — rather
than a reason to distrust a finding. Every rule reports a shape that is in the
source, not a risk it inferred.

Reference: [`wreath.hardening`](../reference/hardening.md),
[`wreath audit`](../reference/audit.md).
