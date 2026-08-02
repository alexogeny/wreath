"""Saying it on the model instead of in a registration call.

The registry underneath is the data and this is sugar over it. Both spellings
produce the same `Classification`, and a test asserts that byte for byte,
because two declaration surfaces that can disagree are worse than one:

```python
class Photo(Model, table="photos"):
    id: Mapped[UUID] = column(Uuid, primary_key=True)
    owner_id: Mapped[Annotated[UUID, Subject()]] = column(Uuid, references=User.id)
    exif_gps: Mapped[Annotated[str | None, Personal(erase=Erase.NULL)]] = column(
        Text, nullable=True
    )
    taken_at: Mapped[datetime] = column(Timestamptz)     # not personal
```

is exactly

```python
privacy.classify(Photo, subject="owner_id", personal={"exif_gps": Erase.NULL})
```

**Why the annotation is worth having anyway.** A classification in a separate
call is a second place the schema is described, and it goes stale the way every
second description does -- the column is renamed, the call is not, and the
redaction quietly stops covering anything. Written on the column, it moves with
the column.

**Why the registration call stays.** A model somebody else's package declares
cannot be annotated by the application that deploys it, and that application is
the controller. So the explicit surface is not a legacy path; it is the one that
works for code you do not own, and neither replaces the other.

**Still no inference.** Nothing here looks at a column's name or its type. A
heuristic that flags `email` and misses `contact_string` produces a plan that is
confident and wrong, and a confident wrong plan is the failure this whole module
exists to prevent. Every marker is written by a person.

## Two levels, because a model has facts a column cannot carry

`Subject` and `Personal` are per-column and live in `Annotated`. Whether a
model's rows are *deleted* outright, and whether they are *exempt* from erasure
under a written reason, are facts about the whole table -- so they are declared
with `classified`, which is a `wreath.orm.Facet`: the ORM metaclass
validates any column names it carries when the class is created, and refuses two
declarations sharing one namespace, so "which of the two privacy declarations
wins" is never a question anybody has to answer at read time.

```python
class Ledger(Model, table="ledger"):
    _privacy = classified(exempt="retained seven years under tax law")
```
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args, get_type_hints

from ..orm.table import Facet, facet
from .model import Erase
from .registry import PrivacyDeclarationError, PrivacyRegistry

__all__ = [
    "Classified",
    "Personal",
    "Subject",
    "classified",
    "declare_model",
    "declare_registry",
]

#: The facet namespace. One string, referenced by both the writer and the
#: reader, because a namespace spelled twice is a namespace that can be spelled
#: differently.
NAMESPACE = "privacy"


@dataclass(frozen=True, slots=True)
class Subject:
    """This column says whose data the row is.

    On an ordinary model it is the foreign key to the subject, and it is the
    annotation form of `classify(model, subject="owner_id")`.

    On the subject model itself -- the person -- write `Subject(root=True)`,
    which is `privacy.subject(User, key="id")`. The distinction is
    declared rather than inferred: "this column references the subject" and
    "this table *is* the subject" are different claims, and a planner that
    guessed between them would pick the wrong root on any schema where a user
    references another user.

    Args:
        root: whether this model is the data subject rather than a table about
            one. Exactly one model per registry may say so.
        delete: whether erasing the subject deletes this model's rows outright,
            rather than emptying their classified columns.
    """

    root: bool = False
    delete: bool = False


@dataclass(frozen=True, slots=True)
class Personal:
    """This column holds the subject's personal data, and how erasure treats it.

    `erase` takes exactly what `classify(personal={...})` takes -- an
    `Erase` value or a `Pseudonymise` -- and is checked by the same
    code, so a disposition that claims to erase while leaving the subject
    identifiable is refused here in the same words.

    The default is `Erase.REDACT` rather than `Erase.NULL` because
    redaction works on a `NOT NULL` column and nulling does not; a default that
    was refused for half of all columns would be a default nobody could use.
    """

    erase: Any = Erase.REDACT


class Classified(Facet):
    """The `privacy` facet: what erasure does to this model's rows as a whole.

    Built by `classified`. Row-level rather than column-level, which is
    exactly the split `wreath.orm.Facet` exists for: whether a table's rows are
    deleted or retained is a fact about the table, and `Annotated` has nowhere
    to put it.
    """

    __slots__ = ("delete", "exempt")

    namespace = NAMESPACE

    def __init__(
        self,
        columns: tuple[str, ...] = (),
        *,
        delete: bool = False,
        exempt: Any = None,
    ) -> None:
        super().__init__(columns)
        self.delete = delete
        self.exempt = exempt


def classified(*, delete: bool = False, exempt: Any = None) -> Classified:
    """Declare model-level erasure behaviour beside the columns it is about.

    Args:
        delete: erasing the subject removes these rows outright.
        exempt: a written reason these rows survive an erasure whole -- a legal
            hold, a financial record, an audit trail. Refused when empty by the
            same check the registration call uses.
    """
    return Classified(delete=delete, exempt=exempt)


def declare_registry(registry: PrivacyRegistry, orm_registry: Any) -> int:
    """Read every annotated model in a compiled ORM registry. Returns the count.

    Called once, when a `Privacy` is handed a registry. Eagerly rather than at
    plan time, because the personal column names are published to the log-site
    layer and a log site is built at import: a name published after that site
    was built would not change it.
    """
    found = 0
    for spec in getattr(orm_registry, "specs", ()) or ():
        found += int(declare_model(registry, spec.model_type))
    return found


def declare_model(registry: PrivacyRegistry, model: type) -> bool:
    """Read one model's annotations and facet into `registry`.

    Returns:
        Whether the model declared anything. `False` for a model with no
        privacy markers at all, which is most of them.
    """
    markers = _markers(model)
    item = facet(model, NAMESPACE)
    if not markers and item is None:
        return False
    subject_column: str | None = None
    subject_marker: Subject | None = None
    personal: dict[str, Any] = {}
    for name, marker in markers:
        if isinstance(marker, Subject):
            if subject_column is not None:
                raise PrivacyDeclarationError(
                    f"{_name(model)} annotates {subject_column!r} and {name!r} both "
                    "with Subject(); a row is one subject's or it is a different "
                    "table. Keep the column the erasure matches on and drop the other"
                )
            subject_column, subject_marker = name, marker
            continue
        if name in personal:
            raise PrivacyDeclarationError(
                f"{_name(model)}.{name} carries two Personal() markers, which is "
                "two answers to what erasure does to it"
            )
        personal[name] = marker.erase
    delete = bool(getattr(item, "delete", False))
    exempt = getattr(item, "exempt", None)
    if subject_marker is not None and subject_marker.root:
        registry.subject(model, key=str(subject_column), delete=subject_marker.delete)
        if not personal and not delete and exempt is None:
            # The subject's own row is in scope by definition, so a root that
            # declares nothing else needs no classification. Making one anyway
            # would put an empty entry in the registry that reads as "somebody
            # classified this and found nothing".
            return True
        registry.classify(model, personal=personal, delete=delete, exempt=exempt)
        return True
    registry.classify(
        model,
        subject=subject_column,
        personal=personal,
        delete=delete or bool(subject_marker is not None and subject_marker.delete),
        exempt=exempt,
    )
    return True


def _markers(model: type) -> list[tuple[str, Any]]:
    """Every `Subject`/`Personal` in this model's annotations, with its column.

    Resolved with `typing.get_type_hints`, so `from __future__ import
    annotations` and a string annotation both work. A name that cannot be
    resolved is raised rather than skipped: a privacy marker nobody could read
    is exactly the silent gap this module exists to make loud.
    """
    try:
        hints = get_type_hints(model, include_extras=True)
    except NameError as error:
        raise PrivacyDeclarationError(
            f"{_name(model)}: its annotations cannot be resolved ({error}), so a "
            "privacy declaration written into one of them cannot be read. Import "
            "the name at module scope, or declare this model's classification "
            "with privacy.classify(...) instead"
        ) from error
    found: list[tuple[str, Any]] = []
    columns = getattr(model, "__wreath_column_map__", None) or {}
    for name, hint in hints.items():
        markers = [item for item in _metadata(hint) if isinstance(item, (Subject, Personal))]
        if not markers:
            continue
        if name not in columns:
            raise PrivacyDeclarationError(
                f"{_name(model)}.{name} carries a privacy marker and is not a mapped "
                f"column; it declares {', '.join(sorted(columns)) or 'none'}. An "
                "erasure writes columns, so a marker on anything else would be a "
                "declaration nothing could act on"
            )
        found.extend((name, marker) for marker in markers)
    return found


def _metadata(hint: Any) -> list[Any]:
    """Every `Annotated` extra in a hint, however deeply it is wrapped.

    `Mapped[Annotated[UUID, Subject()]]` and `Annotated[Mapped[UUID],
    Subject()]` both mean the same thing to a reader and neither is more
    correct, so both are read. The walk is over a type expression, which is a
    finite tree.
    """
    found: list[Any] = []
    stack = [hint]
    while stack:
        item = stack.pop()
        extras = getattr(item, "__metadata__", None)
        if extras:
            found.extend(extras)
            stack.append(item.__origin__)
            continue
        stack.extend(get_args(item))
    return found


def _name(model: type) -> str:
    return getattr(model, "__name__", str(model))
