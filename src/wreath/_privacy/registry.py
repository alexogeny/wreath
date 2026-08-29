"""Where an application says which of its columns are personal, and whose.

Declaration only. There is no inference here, no column-name heuristic and no
"looks like an email" rule, and that absence is a design decision rather than
an unfinished feature: a heuristic that flags `email` and misses
`contact_string` produces a plan that is confident and wrong, which is the
precise failure this whole module exists to prevent. A wrong plan that looks
authoritative is worse than no plan -- `wreath.infra` states the same rule for
the same reason.

**The registration surface is explicit on purpose, and it is a seam.** A sibling
change is landing a generic per-model declaration-metadata hook in
`wreath.orm.table`, at which point

    class Photo(Model):
        owner_id: Annotated[UUID, Subject()]
        exif_gps: Annotated[Point | None, Personal(erase="null")]

becomes a thin adapter that calls `classify()` with exactly the arguments below.
The annotation is sugar; this registry is the data. Building the registry first
means the planner, the executor and the tests do not move when the sugar lands,
and it means the classification of a model somebody else's package declares can
still be stated by the application that deploys it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Erase, Pseudonymise

__all__ = [
    "Classification",
    "PrivacyDeclarationError",
    "PrivacyRegistry",
    "Retention",
]


class PrivacyDeclarationError(ValueError):
    """A classification that cannot be executed, refused where it was written."""


#: Dispositions accepted as a bare value in `personal={...}`. A `Pseudonymise`
#: is accepted too, but only as an instance, because it carries a reason.
_ERASE_VALUES: frozenset[str] = frozenset(item.value for item in Erase)

#: What somebody reaches for when they mean "make it unreadable but keep it
#: joinable", and what this module refuses under that name. Each maps to the
#: advice that replaces it, because a refusal that does not say what to do
#: instead gets worked around rather than fixed.
_PSEUDONYM_SPELLINGS: dict[str, str] = {
    "hash": "a hash is reversible by lookup and still identifies one subject",
    "hashed": "a hash is reversible by lookup and still identifies one subject",
    "sha256": "a digest of a value is a stable identifier for that value",
    "mask": "masking hides characters and keeps the rest identifying",
    "tokenize": "a token maps back to the subject wherever the map is kept",
    "tokenise": "a token maps back to the subject wherever the map is kept",
    "anonymise": "name the transform, not the claim; see Erase.NULL/REDACT",
    "anonymize": "name the transform, not the claim; see Erase.NULL/REDACT",
    "pseudonymise": "spell it Pseudonymise(Declared('why')), which records the reason",
    "pseudonymize": "spell it Pseudonymise(Declared('why')), which records the reason",
}


@dataclass(frozen=True, slots=True)
class Classification:
    """One model's declared personal data, and how it reaches its subject."""

    model: type
    #: The column on *this* model holding the subject's identity, or None when
    #: the model is reached through foreign keys instead.
    subject: str | None
    #: Column name -> `Erase` value or `Pseudonymise`.
    personal: dict[str, Any]
    #: Whether erasure deletes the row rather than emptying its columns.
    delete: bool
    #: A written exemption. Present means the rows are retained whole.
    exempt: str | None


@dataclass(frozen=True, slots=True)
class Retention:
    """A model's declared retention window, enforced by a scheduled pass."""

    model: type
    #: Seconds after `on` at which a row becomes deletable.
    after: float
    #: The timestamp column the window is measured from. Must be indexed, or
    #: the sweep degrades into a sequential scan every five minutes forever.
    on: str
    reason: str


@dataclass
class PrivacyRegistry:
    """Every classification one application declared, and the checks on them.

    Mutable by construction because declarations arrive at import time in
    whatever order the modules load, and frozen in effect afterwards: nothing
    reads this registry until a plan is built.
    """

    subject_model: type | None = None
    subject_key: str = ""
    subject_delete: bool = False
    classifications: dict[type, Classification] = field(default_factory=dict)
    retentions: dict[type, Retention] = field(default_factory=dict)

    def subject(self, model: type, *, key: str = "id", delete: bool = False) -> None:
        """Name the model that *is* a data subject, and its identity column.

        There is exactly one per registry. Two subject roots would mean a row
        could be reached by two different subjects with two different answers,
        and the correct behaviour for that is a design question no framework
        should answer on an application's behalf.
        """
        if self.subject_model is not None and self.subject_model is not model:
            raise PrivacyDeclarationError(
                f"a registry has one subject model; {_name(self.subject_model)} was "
                f"declared before {_name(model)}. A second subject would let one row "
                "be reached by two subjects with two different dispositions"
            )
        self._require_column(model, key, what="subject key")
        self.subject_model = model
        self.subject_key = key
        self.subject_delete = bool(delete)

    def classify(
        self,
        model: type,
        *,
        subject: str | None = None,
        personal: dict[str, Any] | None = None,
        delete: bool = False,
        exempt: str | None = None,
    ) -> None:
        """Declare a model's personal columns and how erasure treats them.

        Args:
            model: the ORM model class.
            subject: the column holding the subject's identity, when this model
                carries it directly. Left None, the model is reached through
                foreign keys instead and the planner works the path out.
            personal: column name -> `Erase` value or `Pseudonymise`.
            delete: erase the whole row rather than emptying its columns.
                Off by default: deleting is the irreversible option *and* the
                one that breaks referential integrity, so it is opted into.
            exempt: a written reason these rows survive an erasure whole --
                a legal hold, a financial record, an audit trail.

        Raises:
            PrivacyDeclarationError: for a column the model does not declare,
                a disposition that claims to erase and does not, a `NULL` on a
                `NOT NULL` column, or an exemption with no reason.
        """
        columns = dict(personal or {})
        if subject is not None:
            self._require_column(model, subject, what="subject column")
        checked: dict[str, Any] = {}
        for column, disposition in columns.items():
            self._require_column(model, column, what="personal column")
            checked[column] = self._check_disposition(model, column, disposition)
        reason = self._check_exemption(exempt)
        if reason is not None and checked:
            # Not an error: a table can be exempt *and* carry personal columns,
            # and the plan prints both. But an exemption plus a `delete=True`
            # is two opposite instructions, and one of them would silently win.
            if delete:
                raise PrivacyDeclarationError(
                    f"{_name(model)} declares exempt= and delete=True together; an "
                    "exemption keeps the rows and a delete removes them. Declare one"
                )
        self.classifications[model] = Classification(
            model=model,
            subject=subject,
            personal=checked,
            delete=bool(delete),
            exempt=reason,
        )

    def retain(self, model: type, *, after: float, on: str, reason: str = "") -> None:
        """Declare how long this model's rows live, measured from `on`.

        Enforced by a scheduled `wreath.passes` walk whose deletions are
        counted, so "we have a retention policy" is a number somebody can read
        rather than a sentence in a policy document.
        """
        if not isinstance(after, int | float) or isinstance(after, bool) or after <= 0:
            raise PrivacyDeclarationError(
                f"{_name(model)} retention needs after= as a positive number of "
                f"seconds; got {after!r}"
            )
        self._require_column(model, on, what="retention column")
        column = _column(model, on)
        if (
            column is not None
            and not getattr(column, "indexed", False)
            and not getattr(column, "primary_key", False)
        ):
            # A retention sweep runs on a schedule forever. Walking an
            # unindexed timestamp is a sequential scan every tick, which is a
            # production incident that arrives months after the declaration.
            raise PrivacyDeclarationError(
                f"{_name(model)}.{on} is not indexed, and a retention sweep walks it "
                "on a schedule forever. Declare index=True on the column, or point "
                "on= at one that is indexed"
            )
        self.retentions[model] = Retention(model=model, after=float(after), on=on, reason=reason)

    def personal_names(self) -> frozenset[str]:
        """Every column name declared personal anywhere, for the log-site hook.

        Names rather than `(table, column)` pairs, because the consumer is
        `wreath._logsite.declare`, which sees a keyword argument's name and
        nothing else. Broader than strictly correct in one direction only: a
        log field that happens to share a name with a personal column is
        hashed rather than written verbatim, which is the safe way to be wrong.
        """
        names: set[str] = set()
        for item in self.classifications.values():
            names.update(item.personal)
            if item.subject:
                names.add(item.subject)
        if self.subject_model is not None and self.subject_key:
            names.add(self.subject_key)
        return frozenset(names)

    def _check_disposition(self, model: type, column: str, disposition: Any) -> Any:
        """Resolve one column's disposition, refusing anything that lies.

        The central refusal of the module. A value that *sounds* like erasure
        and is not -- a hash, a mask, a token -- is rejected by name, with the
        reason it is not erasure and the spelling that records the decision
        instead.
        """
        if isinstance(disposition, Pseudonymise):
            reason = getattr(disposition.reason, "reason", None)
            # An emptiness test would be redundant beside the isinstance one:
            # the only way to reach here with a `str` is through
            # `wreath.passes.Declared`, which already refuses a blank reason at
            # construction. Anything else answers `None` and fails the type
            # test. One spelling of the rule, in the class that owns it.
            if not isinstance(reason, str):
                raise PrivacyDeclarationError(
                    f"{_name(model)}.{column}: Pseudonymise(...) needs "
                    "Declared('why this data stays identifiable'). Pseudonymised "
                    "data is still personal data, so the decision is written down "
                    "rather than defaulted"
                )
            return disposition
        # No `.value` unwrapping for an `Erase`: it is a `StrEnum`, so it *is* a
        # `str` whose value is its text -- `Erase.NULL.strip().lower()` is
        # already `"null"`. A mutation run found the conditional changed no
        # outcome, which is the definition of a second spelling of one
        # condition. The only place the two could differ is `{value!r}` in a
        # refusal below, and an `Erase` never reaches one.
        value = disposition
        if not isinstance(value, str):
            raise PrivacyDeclarationError(
                f"{_name(model)}.{column}: {disposition!r} is not a disposition; use "
                f"Erase.NULL, Erase.REDACT, Erase.RETAIN, or "
                "Pseudonymise(Declared('why'))"
            )
        lowered = value.strip().lower()
        advice = _PSEUDONYM_SPELLINGS.get(lowered)
        if advice is not None:
            raise PrivacyDeclarationError(
                f"{_name(model)}.{column}: {value!r} is not erasure -- {advice}. "
                "Erasure is irreversible: use Erase.NULL or Erase.REDACT. To keep "
                "the value joinable and say so, use "
                "Pseudonymise(Declared('why')), which records that the subject is "
                "still identifiable"
            )
        if lowered not in _ERASE_VALUES:
            raise PrivacyDeclarationError(
                f"{_name(model)}.{column}: unknown disposition {value!r}; expected "
                f"one of {', '.join(sorted(_ERASE_VALUES))}"
            )
        if lowered == Erase.NULL.value:
            self._require_nullable(model, column)
        if lowered == Erase.REDACT.value:
            self._require_textual(model, column)
        return lowered

    def _check_exemption(self, exempt: Any) -> str | None:
        if exempt is None:
            return None
        reason = getattr(exempt, "reason", exempt)
        if not isinstance(reason, str) or not reason.strip():
            raise PrivacyDeclarationError(
                "exempt= needs a written reason -- the rows survive an erasure "
                "request and somebody has to be able to say why to a regulator"
            )
        return reason

    def _require_column(self, model: type, column: str, *, what: str) -> None:
        mapping = getattr(model, "__wreath_column_map__", None)
        if not mapping:
            # An unmapped class, or a plain object in a unit test. Nothing to
            # check against; the planner refuses it later, where it can say
            # which registry the model is missing from.
            return
        if column not in mapping:
            raise PrivacyDeclarationError(
                f"{_name(model)}: {what} {column!r} is not a column on this model; "
                f"it declares {', '.join(sorted(mapping)) or 'none'}"
            )

    def _require_textual(self, model: type, column: str) -> None:
        """Refuse `REDACT` on a column a text marker cannot be written to.

        Redaction overwrites with a fixed marker, and a fixed marker has to be
        a value the column's type accepts. On an integer or a timestamp there
        is no such value that is not also a plausible datum -- writing `0` or
        the epoch would look like data rather than like an absence -- so the
        refusal happens here, where the declaration is, rather than as a type
        error in the middle of an erasure.
        """
        item = _column(model, column)
        # No "is this a mapped column?" guard: an unmapped class answers `None`
        # here, and `None` has no `pg_type`, so the name is empty and the check
        # below already declines to judge. A mutation run found the guard
        # changed no outcome, and two spellings of one condition drift.
        name = str(getattr(getattr(item, "pg_type", None), "name", "")).lower()
        if not name:
            return
        if any(token in name for token in ("text", "char", "citext", "json")):
            return
        raise PrivacyDeclarationError(
            f"{_name(model)}.{column} is {name}, and Erase.REDACT writes a text "
            "marker. Use Erase.NULL on a nullable column, delete=True to remove the "
            "row, or Pseudonymise(Declared('why')) if the value has to stay"
        )

    def _require_nullable(self, model: type, column: str) -> None:
        # No `item is None` guard, for the reason `_require_textual` states:
        # both reads below already default the way an unmapped column needs
        # them to -- not a primary key, and nullable until something says
        # otherwise.
        item = _column(model, column)
        if getattr(item, "primary_key", False):
            raise PrivacyDeclarationError(
                f"{_name(model)}.{column} is the primary key and cannot be nulled. "
                "Erase the row with delete=True, or redact the columns that are not "
                "the key"
            )
        if not getattr(item, "nullable", True):
            raise PrivacyDeclarationError(
                f"{_name(model)}.{column} is NOT NULL, so Erase.NULL would fail at "
                "erasure time rather than here. Use Erase.REDACT, or make the column "
                "nullable"
            )


def _column(model: type, name: str) -> Any:
    mapping = getattr(model, "__wreath_column_map__", None) or {}
    return mapping.get(name)


def _name(model: type) -> str:
    return getattr(model, "__name__", str(model))
