"""The application's own defects, refused at startup instead of found later.

Wreath already knows how to recognise these. `wreath audit code` reads an
application's source and reports the defect classes that a framework cannot
close on its author's behalf -- SQL assembled by formatting, a signing key
written as a literal, a token drawn from `random`, an archive extracted without
vetting its members, an authorization check that returns rather than refusing
when it cannot decide. The catalog is `wreath._audit.rules.code.CODE_RULES`, and
every entry names the wreath primitive that makes its defect unwritable.

The gap this module closes is that **nothing ran it**. An audit is a command
somebody has to remember, on a machine somebody has to configure, at a moment
somebody has to choose; and the defects it finds are exactly the ones that do
not announce themselves in a test, a review, or a correct-looking 200 response.
So the audit moves to the one place in an application's life that is not
optional: the moment it starts.

```python
app = Wreath(hardening="block")     # this application does not boot carrying one
```

## The three settings

`WREATH_HARDENING` in the environment overrides whatever the code asked for,
because turning this up -- or off -- must not need a deploy.

| policy | at startup |
| --- | --- |
| `warn` | every finding is logged; the application starts. **The default.** |
| `block` | the error-level findings are raised; the application does not start. |
| `off` | nothing is scanned. |

Under `block` the refusal happens during ASGI lifespan startup, before any pool
is opened and before the server binds. A conforming server answers
`lifespan.startup.failed` by exiting, so the process that came up carrying a
finding does not go on to serve a request.

Only errors block. A `Severity.WARN` rule is one whose correct form is a
judgement call, and refusing to boot over a judgement call is how `block` stops
being a setting anybody is willing to turn on; warnings are logged beside the
errors that did block.

`warn` is the default rather than `block`, and that is a deliberate choice about
adoption rather than about severity. A framework that refuses to start an
application it has never seen before, over a rule the application's author has
never read, is a framework that gets pinned to the previous version -- and then
the rules protect nobody. `warn` puts the findings in front of the person who
can fix them, on the first run, with no configuration at all. `block` is what a
deployment turns on once its findings are at zero, and it is the setting that
makes this worth having: from then on the defect cannot reach production,
because the process carrying it will not come up.

## What is scanned

The application's own code, found from where its handlers were defined -- a
package root when the handlers live in a package, the module itself when they do
not. Site packages, the standard library and wreath's own tree are never
scanned, so nothing is ever reported that the reader cannot fix.

That is the source tier. The **configuration tier** reads the live application
object instead: the registered outbound clients and their destination policies,
which are assembled at runtime from environment values that no source rule can
see. `allow_private=settings.ALLOW_PRIVATE_FETCH` says nothing at all in the
source and everything at boot.

## Waiving a finding

Waivers are the audit's own, in the spelling `wreath audit code` already
accepts, on the line the finding is about:

```python
key = _DEV_KEY  # wreath-audit: allow hardcoded-secret -- fixture, never deployed
```

Nothing here accepts a file-level or application-level waiver. Those drift away
from the code they were written about, and then they are switching off a rule
nobody remembers agreeing to.

## The bound worth knowing

This is static analysis over one module at a time. A value laundered through a
helper in another file will not be followed. That is a reason to keep the safe
API the easy one -- which is what `wreath.sql`, `wreath.objects.normalize_key`
and `wreath.xml` are for -- rather than a reason to distrust a finding: every
rule reports a shape that is in the source, not a risk it has inferred.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from ._audit.model import Finding, Report, Severity

__all__ = [
    "HardeningError",
    "Policy",
    "POLICY_ENV",
    "apply_policy",
    "application_sources",
    "audit_application",
    "audit_source_for_tenancy",
    "check_application",
    "resolve_policy",
]

logger = logging.getLogger("wreath.hardening")

Policy = Literal["off", "warn", "block"]

#: Overrides whatever the application asked for. An operator has to be able to
#: turn this up without a code change, and down without one either.
POLICY_ENV = "WREATH_HARDENING"

#: A mapping rather than a set so the lookup *is* the narrowing: `Policy` is a
#: Literal, and a membership test on a set leaves the value a plain `str`.
_POLICIES: dict[str, Policy] = {"off": "off", "warn": "warn", "block": "block"}


class HardeningError(Exception):
    """Raised at startup under `hardening="block"`.

    Carries every finding rather than the first, so one boot produces the whole
    worklist instead of turning the fix into a queue of restarts.
    """

    def __init__(self, findings: Iterable[Finding]) -> None:
        self.findings: tuple[Finding, ...] = tuple(findings)
        count = len(self.findings)
        listing = "\n  ".join(_render(finding) for finding in self.findings)
        super().__init__(
            f"hardening='block' refused this application: {count} finding"
            f"{'' if count == 1 else 's'}\n  {listing}\n"
            "Fix them, waive one in place with a reason "
            "(# wreath-audit: allow <rule> -- why), or start with hardening='warn'."
        )


def _render(finding: Finding) -> str:
    where = f"{finding.surface}:{finding.location}" if finding.location else finding.surface
    suggestion = f" -- {finding.suggestion}" if finding.suggestion else ""
    return f"{where}: {finding.rule_id} {finding.message}{suggestion}"


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def resolve_policy(requested: str = "warn") -> Policy:
    """The policy in force, with `WREATH_HARDENING` taking precedence.

    A value that is not one of the three raises rather than falling back.
    Fail-closed here would mean `block` and fail-open would mean `off`, and both
    are worse than telling the operator they typed it wrong at the one moment
    they are looking at it.
    """
    override = os.environ.get(POLICY_ENV)
    value = override or requested
    policy = _POLICIES.get(value.strip().lower())
    if policy is None:
        source = f"{POLICY_ENV}=" if override else "hardening="
        raise ValueError(
            f"{source}{value!r} is not a hardening policy; expected one of "
            f"{', '.join(sorted(_POLICIES))}"
        )
    return policy


def apply_policy(findings: Iterable[Finding], policy: Policy) -> tuple[Finding, ...]:
    """Log or raise, per `policy`, and return what was reported.

    `warn` logs one line per finding, not one summary line. A count is something
    people learn to scroll past, and the whole point of the default policy is
    that its output can be acted on without running anything else.

    Only errors block. A `Severity.WARN` rule is one whose correct form is a
    judgement call -- `case-mapped-authz` and `debug-enabled` are both of those
    -- and refusing to start over a judgement call is how `block` stops being a
    setting anybody turns on.
    """
    reported = tuple(findings)
    if policy == "off" or not reported:
        return reported
    if policy == "block":
        blocking = [finding for finding in reported if finding.severity is Severity.ERROR]
        if blocking:
            for finding in reported:
                if finding.severity is not Severity.ERROR:
                    logger.warning("%s", _render(finding))
            raise HardeningError(blocking)
    for finding in reported:
        log = logger.error if finding.severity is Severity.ERROR else logger.warning
        log("%s", _render(finding))
    if policy == "warn":
        logger.warning(
            "wreath.hardening: %d finding%s above. Set hardening='block' once "
            "they are at zero, so the next one cannot reach production.",
            len(reported),
            "" if len(reported) == 1 else "s",
        )
    return reported


# ---------------------------------------------------------------------------
# what to scan
# ---------------------------------------------------------------------------


def application_sources(app: Any) -> list[Path]:
    """The files and directories holding this application's own code.

    Derived from where its handlers were defined, because that is the only thing
    an application reliably tells the framework about itself.

    A handler in a *package* contributes that package's root directory: an
    application laid out as `shop/routers/orders.py` keeps its queries in
    `shop/db.py`, and scanning only the router would miss them. A handler in a
    top-level module contributes just that file, because its "package root" is
    whatever directory the module happens to sit in -- a scripts folder, a home
    directory, a test suite -- and walking that is somewhere between wasteful
    and wrong.

    Site packages and wreath's own tree are excluded, so an application is never
    reported a finding it has no way to fix.
    """
    here = Path(__file__).resolve().parent
    roots: dict[str, Path] = {}
    handlers = [route.endpoint for route in getattr(app, "_routes", ())]
    handlers += [handler for _path, handler in getattr(app, "_ws_routes", ())]
    for handler in handlers:
        # `@authenticated()` and friends wrap the endpoint, and a wrapper
        # defined in wreath would resolve to wreath's own tree -- which is
        # excluded below, so the application would silently scan nothing.
        while (wrapped := getattr(handler, "__wrapped__", None)) is not None:
            handler = wrapped
        # Coalesced rather than guarded: a handler with no `__module__` finds no
        # module under `""`, and a missing module has no `__file__`, so the
        # `origin is None` check below already skips it. Written as a second
        # guard it was redundant, and a mutant removing it went unnoticed.
        module_name = getattr(handler, "__module__", "") or ""
        if module_name == "__main__":
            # Its `__file__` is the script that was run, and that directory is a
            # scripts folder or a home directory far more often than it is the
            # application.
            continue
        module = sys.modules.get(module_name)
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        location = Path(origin).resolve()
        if "site-packages" in location.parts or location.is_relative_to(here):
            continue
        depth = module_name.count(".")
        if depth == 0:
            roots[location.as_posix()] = location
            continue
        # `shop.routers.orders` at `.../shop/routers/orders.py` -> `.../shop`.
        # `depth - 1`, not `depth`: the last hop up would leave the package and
        # land on whatever directory it happens to be installed into.
        root = location.parent
        for _ in range(depth - 1):
            root = root.parent
        roots[root.as_posix()] = root
    return sorted(roots.values())


def audit_configuration(app: Any) -> list[Finding]:
    """Findings about how this application is configured, not how it is written.

    Read off the live object graph, so a policy assembled at runtime is checked
    as it actually resolved rather than as it appears in the source. This is the
    half a source rule structurally cannot reach: the `ssrf-policy-widened` rule
    sees `allow_private=True` written out, and sees nothing at all when the same
    switch arrives from a settings module.
    """
    findings: list[Finding] = []
    for name, client in getattr(app, "_http_clients", {}).items():
        policy = getattr(client, "_destination", None)
        permitted = [
            switch
            for switch in ("allow_private", "allow_loopback", "allow_link_local")
            if getattr(policy, switch, False)
        ]
        if permitted:
            findings.append(
                Finding(
                    rule_id="ssrf-policy-widened",
                    severity=Severity.ERROR,
                    surface=f"client:{name}",
                    message=(
                        "this client's DestinationPolicy resolved with "
                        f"{', '.join(permitted)} at startup"
                    ),
                    reference="CWE-918",
                    suggestion=(
                        "leave DestinationPolicy at its defaults and name the "
                        "hosts you mean with hosts=; 169.254.169.254 is the "
                        "cloud metadata endpoint"
                    ),
                )
            )
    return findings


def audit_source_for_tenancy(source: str, *, surface: str = "app") -> list[Finding]:
    """Find tenant-schema literals written out in application source.

    The one shape that walks past a `SET LOCAL search_path`: a name that is
    already qualified. The GRANTs make it fail *closed* -- the server refuses --
    but that refusal happens in production, and this makes it fail *early*,
    where the person who wrote it is still looking at it.

    An `ERROR` rather than a warning, because there is no legitimate reason for
    an application to name another tenant's schema: its own is what the search
    path resolves, and the central schema is named by its own name.
    """
    from .tenancy import find_schema_literals

    return [
        Finding(
            rule_id="tenant-schema-literal",
            severity=Severity.ERROR,
            surface=surface,
            message=(
                f"this query names the tenant schema {name!r} explicitly, which resolves "
                "past the request's own search_path"
            ),
            reference="CWE-639",
            suggestion=(
                "drop the schema qualifier and let the tenant binding resolve it; if the "
                "table really is another tenant's, that is the finding"
            ),
        )
        for name in find_schema_literals(source)
    ]


def audit_application(app: Any) -> Report:
    """Both tiers, as one report: the configuration, then the source."""
    from ._audit.scan import scan_paths

    report = Report()
    report.extend(audit_configuration(app))
    sources = application_sources(app)
    if sources:
        # `include_tests` stays off: a test suite hardcodes secrets, seeds
        # generators and compares tokens with `==` entirely legitimately, and
        # reporting all of that at every boot is how the whole thing gets
        # switched off in week one.
        report.extend(scan_paths(sources).findings)
    return report


def check_application(app: Any, policy: Policy) -> tuple[Finding, ...]:
    """Audit `app`, then log or raise per `policy`. The startup entry point."""
    if policy == "off":
        return ()
    return apply_policy(audit_application(app).sorted(), policy)
