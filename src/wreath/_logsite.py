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
    LOG_ARG_INT_MAX,
    LOG_ARG_INT_MIN,
    LOG_ARG_LENGTH_MAX,
    LOG_MAX_ARGS,
    LOG_SPEC_BOOL,
    LOG_SPEC_BYTES,
    LOG_SPEC_FLOAT,
    LOG_SPEC_INT,
    LOG_SPEC_NONE,
    LOG_SPEC_STR,
    CaptureDisposition,
    LogArg,
    LogArgType,
    LogCell,
    Severity,
    siphash24,
)

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

#: Field names an application has declared personal, installed by
#: `wreath.privacy.Privacy.classify`. Empty until something classifies a column,
#: so a project that does not use `wreath.privacy` pays one frozenset membership
#: test per *declared* site -- which happens once per site, at import, never on
#: the logging path.
#:
#: This is the seam that makes a classification worth writing once. Declaring
#: `Photo.owner_id` personal changes how a log field called `owner_id` is
#: captured, with no second configuration file to keep in step with the schema
#: -- and a second file is precisely how every hand-maintained redaction list
#: drifts from the columns it is supposed to describe.
#:
#: Names rather than `(table, column)` pairs, because a keyword argument at a
#: log call site carries a name and nothing else. That is broader than strictly
#: correct in one direction only: an unrelated field sharing the name is
#: fingerprinted rather than written verbatim, which is the safe way to be wrong.
_PERSONAL_NAMES: set[str] = set()


def declare_personal(names: frozenset[str]) -> None:
    """Replace the set of field names treated as personal.

    Replace rather than union: a registry that has dropped a classification
    should stop claiming the name, and an accumulating set could never shrink.
    """
    _PERSONAL_NAMES.clear()
    _PERSONAL_NAMES.update(names)


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

    A name an application has classified as personal defaults to HASHED
    whatever its type, because the type argument is wrong for exactly the
    values that matter most: a subject identifier is very often an `int` or a
    `UUID`-as-`int`, and "an integer is not a secret-bearing shape" stops being
    true the moment the integer is which person this record is about. An
    explicit disposition still wins -- the classification changes the *default*,
    and a call site that says RAW on purpose has made a decision this should
    not overrule.
    """
    if type_ not in _PACKABLE:
        raise LogSiteError(
            f"field {name!r} declares {type_!r}, which cannot be packed into a log "
            f"record; declare one of {', '.join(t.__name__ for t in _PACKABLE)}"
        )
    if disposition is None:
        disposition = (
            CaptureDisposition.HASHED
            if type_ in _SECRET_BEARING or name in _PERSONAL_NAMES
            else CaptureDisposition.RAW
        )
    return LogField(name=name, type=type_, disposition=disposition)


#: Declared type -> the nibble the emitter branches on. Keyed by `type` rather
#: than by the literal classes so the lookup below types cleanly; `declare`
#: already refuses anything absent here, at import, which is what makes the
#: `.get` fallback unreachable rather than a silent default.
_SPEC_TYPES: Final[dict[type, int]] = {
    type(None): LOG_SPEC_NONE,
    bool: LOG_SPEC_BOOL,
    int: LOG_SPEC_INT,
    float: LOG_SPEC_FLOAT,
    str: LOG_SPEC_STR,
    bytes: LOG_SPEC_BYTES,
}


def spec_blob(fields: tuple[LogField, ...]) -> bytes:
    """Flatten a site's fields into one byte each: `(type << 4) | disposition`.

    The native emitter walks this beside the argument tuple, so packing branches
    on a small integer rather than on `isinstance` and a `CaptureDisposition`
    comparison per argument. It is computed once, at registration, because that
    is the whole point of interning a call site: the static half is decided at
    import and the request path only carries values.
    """
    return bytes(
        (_SPEC_TYPES[field.type] << 4) | int(field.disposition) for field in fields
    )


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
    #: `fields` flattened for the native emitter. Derived, never passed in.
    specs: bytes = b""


def specs_for(site: LogSite, fields: tuple[LogField, ...]) -> bytes:
    """The spec blob for *these* fields, reusing the site's when they match.

    Only the kwargs tiers need this. `intern_template` keys on the template
    text, and the text does not pin the types: `log.info("v is {v}", v=1)` and
    the same line with a string reach the same interned site, whose declared
    fields are whichever call arrived first. Packing the second call against the
    first call's types would turn its value into a counted mismatch and lose it,
    which the Python packer never did -- so the native emitter must not either.

    Rebuilding the blob unconditionally costs ~360ns; comparing first costs
    ~170ns and skips the rebuild in every case that is not this pathology. A
    record stays self-describing either way -- every argument carries its own
    type tag -- so a reader decodes the value that was passed rather than the
    one the site expected.
    """
    return site.specs if site.fields == fields else spec_blob(fields)


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
                specs=spec_blob(fields),
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
                specs=spec_blob(fields),
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
            specs=spec_blob(fields),
        )
        self._by_id.append(site)
        return site

    def get(self, site_id: int) -> LogSite | None:
        """The site behind an id, or None when the record is uninterned."""
        if 1 <= site_id <= len(self._by_id):
            return self._by_id[site_id - 1]
        return None

    @property
    def key(self) -> tuple[int, int]:
        """The fingerprint key, for an emitter that hashes somewhere else.

        The native emitter computes the same SipHash in C, and it has to use
        *this* registry's key or the two halves of one process would fingerprint
        the same string differently -- which would break correlation within a
        recording, the one property a fingerprint exists to provide. Handing the
        key out is what keeps the registry its owner.
        """
        return self._key

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

    This is half of the Python packing; the other half is `LogCell.encode`.
    Together they are what runs when there is no ring, when a record is
    buffered, or when the caller is not the loop -- `wreath_nfr_log` packs
    every other record straight into a cell. **Which is which, and why, is
    written once at the head of the log-record section in `_flight_schema.py`.**

    Returns the argument and whether the value failed its declared type. A
    mismatch is *counted*, never raised: a log call that can break the request
    that made it is worse than a log line that reads `?`. Three values used to
    break that promise from inside this function -- an int wider than the int64
    slot, a float too wide to narrow, and a lone surrogate -- and each is now a
    counted mismatch. The parity corpus is what found them.
    """
    if spec.disposition is CaptureDisposition.HASHED:
        return LogArg.hashed(registry.fingerprint(_as_bytes(value))), False
    if spec.disposition in (CaptureDisposition.MASKED, CaptureDisposition.LENGTH):
        return LogArg.length(min(len(_as_bytes(value)), LOG_ARG_LENGTH_MAX)), False
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
        # A Python int is unbounded and the wire slot is int64. Out of range is
        # a mismatch, not an exception: `struct.pack` used to raise here, out of
        # the sink and into whatever made the log call, which is the one thing
        # this function promises cannot happen.
        if not LOG_ARG_INT_MIN <= value <= LOG_ARG_INT_MAX:
            return LogArg.none(), True
        return LogArg.integer(value), False
    if spec.type is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return LogArg.none(), True
        try:
            widened = float(value)
        except OverflowError:
            # An int too wide to become a double. Same shape as the int32/int64
            # case above and the same answer: a value the wire cannot carry is a
            # mismatch, not an exception raised at whoever made the log call.
            return LogArg.none(), True
        return LogArg.real(widened), False
    if spec.type is str:
        if not isinstance(value, str):
            return LogArg.none(), True
        return LogArg.text(value), False
    # Registration admits only `_PACKABLE`; every other member returned above,
    # so bytes is the closed final case rather than another defensive branch.
    if not isinstance(value, bytes):
        return LogArg.none(), True
    return LogArg.text(value.decode("utf-8", "replace")), False


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
