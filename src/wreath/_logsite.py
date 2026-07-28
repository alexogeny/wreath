"""Interned log call sites: the static half of a log statement, stored once.

A log statement is mostly constant. The template, severity, module, line, field
names, argument types and redaction dispositions never change between calls;
only the values do. This module keeps the constant half in a bounded registry
keyed by a dense integer, so a record on the ring carries `site_id` plus packed
arguments and nothing else.

That is the split NanoLog gets from a compile-time pass. Python has none, so the
binding happens at import: `wreath.logging.event(...)` registers a site and
returns a callable that emits only the dynamic half. The same table serves the
kwargs tier, which interns lazily on the template text.

**Ownership is explicit.** The registry is not ambient module state that anything
may reach into: `wreath.logging` owns exactly one, installs it, and hands it to
the projector for rendering. `testing_runtime` swaps a fresh one in and out so a
test never inherits another test's ids.

Redaction is deny-by-default, matching `wreath.recording`: a scalar is written
verbatim, and a string is fingerprinted with a process-local keyed SipHash
unless its field declares `RAW`. To read a string in cleartext you say so at the
call site, where a reviewer will see it.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass
from typing import Any, Final

from ._flight_schema import (
    LOG_MAX_ARGS,
    CaptureDisposition,
    LogArg,
    LogArgType,
    LogCell,
    Severity,
)
from ._pure.flight import siphash24

#: Call sites retained per registry. A site is ~200 bytes of Python objects, so
#: this is a small fixed cost; overflow degrades to uninterned records rather
#: than growing without bound. Provisional, like every other NFR budget.
DEFAULT_SITE_CAPACITY: Final = 4096

#: Types a declared field may carry. Anything else is refused at registration,
#: which is import time -- the point of declaring types at all is that a value
#: the packer cannot handle fails loudly then, not at 3am.
_PACKABLE: Final = (int, float, bool, str, bytes, type(None))

#: Field types whose values are fingerprinted unless the site declares RAW.
_SECRET_BEARING: Final = (str, bytes)

_FORMATTER: Final = string.Formatter()


class LogSiteError(ValueError):
    """A call site is malformed. Raised at registration, never at emit time."""


@dataclass(frozen=True, slots=True)
class LogField:
    """One declared argument of a call site."""

    name: str
    type: type
    disposition: CaptureDisposition


def declare(
    name: str, type_: type, disposition: CaptureDisposition | None = None
) -> LogField:
    """Declare a field, defaulting its disposition by type.

    Scalars default to RAW because an integer is not a secret-bearing shape;
    strings and bytes default to HASHED because they are where a token ends up.
    """
    if type_ not in _PACKABLE:
        raise LogSiteError(
            f"field {name!r} declares {type_!r}, which cannot be packed into a log "
            f"record; declare one of {', '.join(t.__name__ for t in _PACKABLE)}"
        )
    if disposition is None:
        disposition = (
            CaptureDisposition.HASHED
            if type_ in _SECRET_BEARING
            else CaptureDisposition.RAW
        )
    return LogField(name=name, type=type_, disposition=disposition)


@dataclass(frozen=True, slots=True)
class LogSite:
    """The interned static half of one log statement."""

    site_id: int
    event_name: str
    template: str
    severity: Severity
    fields: tuple[LogField, ...]
    module: str = ""
    lineno: int = 0


def _template_names(template: str) -> tuple[str, ...]:
    """The `{name}` placeholders of a template, in order, ignoring literals."""
    names: list[str] = []
    for _literal, name, _spec, _conv in _FORMATTER.parse(template):
        if name:
            names.append(name)
    return tuple(names)


def validate(event_name: str, template: str, fields: tuple[LogField, ...]) -> None:
    """Refuse a malformed site at registration.

    Every check here is one an operator would otherwise meet as a broken log
    line in production, and every one is cheap to make at import.
    """
    if len(fields) > LOG_MAX_ARGS:
        raise LogSiteError(
            f"site {event_name!r} declares {len(fields)} fields; a record holds at "
            f"most {LOG_MAX_ARGS}"
        )
    declared = [f.name for f in fields]
    if len(set(declared)) != len(declared):
        raise LogSiteError(f"site {event_name!r} declares a field name twice")
    used = set(_template_names(template))
    missing = used.difference(declared)
    if missing:
        raise LogSiteError(
            f"site {event_name!r} has a template naming {sorted(missing)}, which no "
            f"field declares"
        )
    unused = [name for name in declared if name not in used]
    if unused:
        raise LogSiteError(
            f"site {event_name!r} declares {sorted(unused)}, which the template "
            f"never uses; an unused field is a record nobody can read"
        )


class SiteRegistry:
    """A bounded table of interned call sites.

    Ids are dense and start at 1, so 0 always means "uninterned" on the wire and
    a reader never has to distinguish a missing site from site zero.
    """

    __slots__ = ("_by_id", "_by_name", "_by_template", "_capacity", "_key", "_overflow")

    def __init__(self, capacity: int = DEFAULT_SITE_CAPACITY) -> None:
        if capacity < 1:
            raise LogSiteError(f"site capacity must be positive, got {capacity}")
        self._capacity = capacity
        self._by_id: list[LogSite] = []
        self._by_name: dict[str, LogSite] = {}
        self._by_template: dict[str, LogSite] = {}
        self._overflow = 0
        # A process-local key, seeded once and never on an emit path. Two
        # processes fingerprint the same string differently, which is the point:
        # a fingerprint correlates occurrences within one recording and
        # discloses nothing across them.
        raw = os.urandom(16)
        self._key = (
            int.from_bytes(raw[:8], "little"),
            int.from_bytes(raw[8:], "little"),
        )

    def set_capacity(self, capacity: int) -> None:
        """Re-apply a configured ceiling to a table that already holds sites.

        The server adopts the boot-time registry so import-time call sites keep
        their ids, and then has to impose the configured capacity on it --
        otherwise `LoggingConfig.site_capacity` would be a field that does
        nothing. Sites already interned above a lowered ceiling are kept: their
        ids are already on records in flight, and evicting them would make those
        records unreadable. The ceiling binds new registrations only.
        """
        if capacity < 1:
            raise LogSiteError(f"site capacity must be positive, got {capacity}")
        self._capacity = capacity

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def overflow(self) -> int:
        """Sites refused because the table was full."""
        return self._overflow

    def register(
        self,
        event_name: str,
        template: str,
        severity: Severity,
        fields: tuple[LogField, ...],
        *,
        module: str = "",
        lineno: int = 0,
    ) -> LogSite:
        """Intern a site declared with `wreath.logging.event`.

        A duplicate event name is refused rather than shadowed: two sites with
        one name make every aggregate over that name a lie.
        """
        validate(event_name, template, fields)
        if event_name in self._by_name:
            raise LogSiteError(f"log event {event_name!r} is already registered")
        site = self._intern(event_name, template, severity, fields, module, lineno)
        if site is None:
            # Table full. The caller still gets a usable site object so its
            # records pack and emit; they simply travel uninterned.
            return LogSite(
                site_id=0,
                event_name=event_name,
                template=template,
                severity=severity,
                fields=fields,
                module=module,
                lineno=lineno,
            )
        self._by_name[event_name] = site
        return site

    def intern_template(
        self, template: str, severity: Severity, fields: tuple[LogField, ...]
    ) -> LogSite:
        """Intern a site for the kwargs tier, keyed on the template *text*.

        Keying on text rather than object identity is deliberate. Identity would
        mint a fresh site for every call whose template was built at runtime,
        filling the table with garbage; the text is a stable key whose hash
        CPython already caches on the string object.
        """
        found = self._by_template.get(template)
        if found is not None:
            return found
        site = self._intern(template, template, severity, fields, "", 0)
        if site is None:
            return LogSite(
                site_id=0,
                event_name=template,
                template=template,
                severity=severity,
                fields=fields,
            )
        self._by_template[template] = site
        return site

    def _intern(
        self,
        event_name: str,
        template: str,
        severity: Severity,
        fields: tuple[LogField, ...],
        module: str,
        lineno: int,
    ) -> LogSite | None:
        if len(self._by_id) >= self._capacity:
            self._overflow += 1
            return None
        site = LogSite(
            site_id=len(self._by_id) + 1,
            event_name=event_name,
            template=template,
            severity=severity,
            fields=fields,
            module=module,
            lineno=lineno,
        )
        self._by_id.append(site)
        return site

    def get(self, site_id: int) -> LogSite | None:
        """The site behind an id, or None when the record is uninterned."""
        if 1 <= site_id <= len(self._by_id):
            return self._by_id[site_id - 1]
        return None

    def fingerprint(self, raw: bytes) -> int:
        """The keyed, non-reversible fingerprint of a redacted value."""
        return siphash24(raw, *self._key)


# --- packing ----------------------------------------------------------------


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", "replace")


def pack_value(
    registry: SiteRegistry, value: object, spec: LogField
) -> tuple[LogArg, bool]:
    """Convert one Python value into a packed argument.

    This is half of the Python packing a native emitter would replace; the other
    half is `LogCell.encode`. **What is deferred and why is written once, at the
    head of the log-record section in `_flight_schema.py`** -- read that before
    starting on `wreath_nfr_log`.


    Returns the argument and whether the value failed its declared type. A
    mismatch is *counted*, never raised: a log call that can break the request
    that made it is worse than a log line that reads `?`.
    """
    if spec.disposition is CaptureDisposition.HASHED:
        return LogArg.hashed(registry.fingerprint(_as_bytes(value))), False
    if spec.disposition in (CaptureDisposition.MASKED, CaptureDisposition.LENGTH):
        return LogArg.length(len(_as_bytes(value))), False
    if value is None:
        return LogArg.none(), spec.type is not type(None)
    # bool before int: bool is an int subclass and would otherwise pack as one.
    if spec.type is bool:
        if not isinstance(value, bool):
            return LogArg.none(), True
        return LogArg.boolean(value), False
    if spec.type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            return LogArg.none(), True
        return LogArg.integer(value), False
    if spec.type is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return LogArg.none(), True
        return LogArg.real(float(value)), False
    if spec.type is str:
        if not isinstance(value, str):
            return LogArg.none(), True
        return LogArg.text(value), False
    if spec.type is bytes:
        if not isinstance(value, bytes):
            return LogArg.none(), True
        return LogArg.text(value.decode("utf-8", "replace")), False
    return LogArg.none(), True


def infer_field(name: str, value: object) -> LogField:
    """Derive a field spec for the kwargs tier, deny-by-default.

    Scalars keep their value; anything string-shaped is fingerprinted. This is
    the rule that lets the ergonomic tier stay a one-liner and still be unable
    to leak a secret.
    """
    if isinstance(value, bool):
        return LogField(name, bool, CaptureDisposition.RAW)
    if isinstance(value, int):
        return LogField(name, int, CaptureDisposition.RAW)
    if isinstance(value, float):
        return LogField(name, float, CaptureDisposition.RAW)
    if value is None:
        return LogField(name, type(None), CaptureDisposition.RAW)
    return LogField(name, str, CaptureDisposition.HASHED)


# --- reading a record back --------------------------------------------------


def _display(arg: LogArg, spec: LogField | None) -> Any:
    """One argument as a Python value for rendering or structured output."""
    if arg.type is LogArgType.STR:
        return arg.text_value
    if arg.type is LogArgType.INT:
        return arg.number
    if arg.type is LogArgType.FLOAT:
        return arg.fraction
    if arg.type is LogArgType.BOOL:
        return bool(arg.number)
    if arg.type is LogArgType.HASH:
        return f"#{arg.number:016x}"
    if arg.type is LogArgType.LENGTH:
        return f"<{arg.number} bytes>"
    return None if spec is None else "?"


def attributes(registry: SiteRegistry, cell: LogCell) -> dict[str, Any]:
    """The record's arguments as named values, for structured output."""
    site = registry.get(cell.site_id)
    if site is None:
        return {f"arg{i}": _display(arg, None) for i, arg in enumerate(cell.args)}
    return {
        spec.name: _display(arg, spec)
        for spec, arg in zip(site.fields, cell.args, strict=False)
    }


def render(registry: SiteRegistry, cell: LogCell) -> str:
    """Reconstruct the human-readable message, off the request path.

    This is the deferred half of deferred formatting: the record carried
    arguments, the registry carries the template, and the two meet on the
    writer thread rather than in the handler.
    """
    site = registry.get(cell.site_id)
    if site is None:
        return f"<unknown log site {cell.site_id}>"
    values = attributes(registry, cell)
    for spec in site.fields:
        values.setdefault(spec.name, "?")
    try:
        return site.template.format(**values)
    except (IndexError, KeyError, ValueError):
        # A template that cannot render is still worth reading. Falling back
        # keeps a malformed site from silencing the record entirely.
        return f"{site.event_name} {values}"


@dataclass(slots=True)
class SiteCounters:
    """Degradations that must stay visible rather than silent."""

    type_mismatch: int = 0
    arity_mismatch: int = 0
    dropped_no_runtime: int = 0
