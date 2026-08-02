"""`Annotated[..., Subject()]` is sugar, and the sugar must not be a second answer.

Two declaration surfaces that can disagree are worse than one, so the tests
that matter here are the equivalence ones: the same schema declared both ways
produces the same `Classification` objects and the same plan digest, which is
the value `wreath privacy erase` refuses on. Everything else in this file is
about the refusals -- a marker that names something that is not a column, two
markers that answer one question twice.

No database, and no ORM change: the markers live in `Annotated`, the row-level
facts live in a `wreath.orm.Facet`, and the registry underneath is untouched.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.passes import Declared
from wreath.privacy import (
    Erase,
    Personal,
    Privacy,
    PrivacyDeclarationError,
    Pseudonymise,
    Subject,
    classified,
)


class FakeDatabase:
    name = "main"


def _declared_by_hand() -> Privacy:
    """The explicit surface, exactly as `wreath.privacy`'s docstring shows it."""

    class Person(Model, table="people"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        email: Mapped[str] = column(Text)

    class Photo(Model, table="photos"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        owner_id: Mapped[int | None] = column(
            Int64, references=Person.id, on_delete="set null", nullable=True
        )
        caption: Mapped[str] = column(Text)

    class Ledger(Model, table="ledger"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        person_id: Mapped[int | None] = column(
            Int64, references=Person.id, on_delete="set null", nullable=True
        )
        payer_name: Mapped[str] = column(Text)

    registry = Registry(FakeDatabase(), [Person, Photo, Ledger], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    privacy.classify(
        Ledger,
        personal={"payer_name": Erase.REDACT},
        exempt="retained seven years under tax law",
    )
    return privacy


def _declared_on_the_models() -> Privacy:
    """The same schema, said on the columns it is about."""

    class Person(Model, table="people"):
        id: Mapped[Annotated[int, Subject(root=True, delete=True)]] = column(
            Int64, primary_key=True, server_default="nextval('s')"
        )
        email: Mapped[str] = column(Text)

    class Photo(Model, table="photos"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        owner_id: Mapped[Annotated[int | None, Subject()]] = column(
            Int64, references=Person.id, on_delete="set null", nullable=True
        )
        caption: Mapped[Annotated[str, Personal(erase=Erase.REDACT)]] = column(Text)

    class Ledger(Model, table="ledger"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        person_id: Mapped[int | None] = column(
            Int64, references=Person.id, on_delete="set null", nullable=True
        )
        payer_name: Mapped[Annotated[str, Personal()]] = column(Text)

        _privacy = classified(exempt="retained seven years under tax law")

    registry = Registry(FakeDatabase(), [Person, Photo, Ledger], validate_schema="off")
    return Privacy(registry)


def _classifications(privacy: Privacy) -> dict[str, tuple]:
    """Every registration, keyed by model name and comparable across registries.

    The `Classification` holds the model class, which two registries cannot
    share, so the comparison is over everything else -- which is all of the
    behaviour.
    """
    return {
        item.model.__name__: (item.subject, item.personal, item.delete, item.exempt)
        for item in privacy._registry.classifications.values()
    }


def test_both_declaration_surfaces_register_the_same_thing() -> None:
    """The property that makes one of them sugar rather than a second answer."""
    by_hand = _declared_by_hand()
    on_models = _declared_on_the_models()
    assert _classifications(on_models) == _classifications(by_hand)
    assert (
        on_models._registry.subject_key,
        on_models._registry.subject_delete,
    ) == (by_hand._registry.subject_key, by_hand._registry.subject_delete)
    assert on_models._registry.subject_model.__name__ == "Person"


def test_both_surfaces_produce_the_same_plan_digest() -> None:
    """The digest is what `erase` refuses on, so it is the equivalence that ships.

    Comparing the registrations proves the declarations match; comparing the
    digest proves the *plans* do, which is the value an operator quotes and the
    one a mismatch would refuse.
    """
    assert _declared_on_the_models().plan("4711").digest == (
        _declared_by_hand().plan("4711").digest
    )


def test_the_annotated_form_reaches_the_generated_passes() -> None:
    """Sugar that stopped at the registry would be sugar that erased nothing."""
    prepared = _declared_on_the_models().prepare("4711")
    photo = next(step for action, step in prepared.steps if action.table == "photos")
    assert photo.work.set_ == {"caption": "'[erased]'"}
    assert photo.work.where.text.startswith('("owner_id" = ?)')


def test_a_personal_marker_defaults_to_redaction() -> None:
    """`Personal()` with no argument still has to mean something definite."""

    class Note(Model, table="notes"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Mapped[Annotated[str, Personal()]] = column(Text)

    privacy = Privacy()
    assert privacy.declare(Note) is True
    assert privacy._registry.classifications[Note].personal == {"body": "redact"}


def test_a_model_with_no_markers_declares_nothing() -> None:
    """Most models are this one, and an empty registration is not nothing."""

    class Plain(Model, table="plain"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        label: Mapped[str] = column(Text)

    privacy = Privacy()
    assert privacy.declare(Plain) is False
    assert privacy._registry.classifications == {}


def test_a_root_subject_with_nothing_else_records_no_empty_classification() -> None:
    """The subject's own row is in scope by definition, not by classification."""

    class Person(Model, table="people"):
        id: Mapped[Annotated[int, Subject(root=True)]] = column(
            Int64, primary_key=True, server_default="nextval('s')"
        )

    privacy = Privacy()
    privacy.declare(Person)
    assert privacy._registry.subject_model is Person
    assert privacy._registry.classifications == {}


def test_a_root_subject_that_is_also_personal_gets_both_registrations() -> None:
    """The person's own row is in scope *and* can hold classified columns.

    Two registry calls from one model, and the early return for a bare root
    must not swallow the second.
    """

    class Person(Model, table="people_two"):
        id: Mapped[Annotated[int, Subject(root=True)]] = column(
            Int64, primary_key=True, server_default="nextval('s')"
        )
        email: Mapped[Annotated[str, Personal(erase=Erase.REDACT)]] = column(Text)

    privacy = Privacy()
    privacy.declare(Person)
    assert privacy._registry.subject_model is Person
    assert privacy._registry.classifications[Person].personal == {"email": "redact"}


def test_a_subject_marker_can_declare_that_the_row_goes_too() -> None:
    """`Subject(delete=True)` on a child is `classify(..., delete=True)`."""

    class Session(Model, table="sessions_two"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        owner_id: Mapped[Annotated[int, Subject(delete=True)]] = column(Int64)

    privacy = Privacy()
    privacy.declare(Session)
    item = privacy._registry.classifications[Session]
    assert (item.subject, item.delete) == ("owner_id", True)


def test_the_marker_is_read_through_either_spelling_of_annotated() -> None:
    """`Mapped[Annotated[X, m]]` and `Annotated[Mapped[X], m]` mean the same thing.

    Neither is more correct to a reader, and a form that silently declared
    nothing would be the exact failure this module exists to prevent.
    """

    class Outside(Model, table="outside"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Annotated[Mapped[str], Personal(erase=Erase.REDACT)] = column(Text)

    privacy = Privacy()
    privacy.declare(Outside)
    assert privacy._registry.classifications[Outside].personal == {"body": "redact"}


def test_a_marker_on_something_that_is_not_a_column_is_refused() -> None:
    """An erasure writes columns, so a marker anywhere else acts on nothing."""

    class Loose(Model, table="loose"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Mapped[str] = column(Text)
        elsewhere: Annotated[str, Personal()] = "not a column"

    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="is not a mapped column"):
        privacy.declare(Loose)


def test_two_subject_markers_on_one_model_are_refused() -> None:
    """A row belongs to one subject, or the table is two tables."""

    class Twice(Model, table="twice"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        owner_id: Mapped[Annotated[int, Subject()]] = column(Int64)
        author_id: Mapped[Annotated[int, Subject()]] = column(Int64)

    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="both\nwith Subject|both with"):
        privacy.declare(Twice)


def test_two_personal_markers_on_one_column_are_refused() -> None:
    """Two answers to what erasure does to one column is not a declaration."""

    class Both(Model, table="both"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Mapped[
            Annotated[str, Personal(erase=Erase.REDACT), Personal(erase=Erase.RETAIN)]
        ] = column(Text)

    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="two Personal"):
        privacy.declare(Both)


def test_an_annotated_disposition_goes_through_the_same_refusals() -> None:
    """The sugar must not be a way past the check that makes the module honest."""

    class Sneaky(Model, table="sneaky"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        email: Mapped[Annotated[str, Personal(erase="hash")]] = column(Text)

    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="is not erasure"):
        privacy.declare(Sneaky)


def test_an_annotated_pseudonymisation_still_needs_its_written_reason() -> None:
    class Ticket(Model, table="tickets"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Mapped[Annotated[str, Personal(erase=Pseudonymise(Declared("kept joinable")))]] = (
            column(Text)
        )

    privacy = Privacy()
    privacy.declare(Ticket)
    disposition = privacy._registry.classifications[Ticket].personal["body"]
    assert isinstance(disposition, Pseudonymise)
    assert disposition.text == "kept joinable"


def test_a_facet_exemption_with_no_reason_is_refused() -> None:
    """The refusal lives in the registry, so both surfaces get it identically."""

    class Held(Model, table="held"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Mapped[Annotated[str, Personal()]] = column(Text)

        _privacy = classified(exempt="   ")

    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="needs a written reason"):
        privacy.declare(Held)


def test_a_facet_delete_reaches_the_registration() -> None:
    class Session(Model, table="sessions"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        owner_id: Mapped[Annotated[int, Subject()]] = column(Int64)

        _privacy = classified(delete=True)

    privacy = Privacy()
    privacy.declare(Session)
    item = privacy._registry.classifications[Session]
    assert (item.subject, item.delete) == ("owner_id", True)


def test_an_unresolvable_annotation_is_raised_rather_than_skipped() -> None:
    """A marker nobody could read is the silent gap this module exists to find."""

    class Broken(Model, table="broken"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Mapped[str] = column(Text)

    Broken.__annotations__["body"] = "Nonexistent"
    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="cannot be resolved"):
        privacy.declare(Broken)


def test_a_marker_on_a_class_the_orm_never_mapped_names_what_it_has() -> None:
    """A plain class has no columns, and the refusal has to say so rather than "".

    The declaration surface is deliberately usable against a model somebody
    else's package declares, so "this is not a model at all" is a mistake that
    reaches here rather than being impossible.
    """

    class Loose:
        __name__ = "Loose"
        __annotations__ = {"body": Annotated[str, Personal()]}

    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="it declares none"):
        privacy.declare(Loose)


def test_an_unrelated_annotated_extra_is_not_a_privacy_declaration() -> None:
    """`Annotated` is shared ground; reading every extra would claim other people's.

    A column carrying somebody else's metadata must produce no classification
    at all, not an empty one -- an empty registration reads as "somebody
    classified this and found nothing".
    """

    class Tagged(Model, table="tagged"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        body: Mapped[Annotated[str, "documentation, not a marker"]] = column(Text)

    privacy = Privacy()
    assert privacy.declare(Tagged) is False
    assert privacy._registry.classifications == {}


def test_the_refusal_lists_the_columns_the_model_does_declare() -> None:
    """A marker on a near-miss name is a typo, and the fix is in the message."""

    class Photo(Model, table="photos_typo"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        caption: Mapped[str] = column(Text)
        elsewhere: Annotated[str, Personal()] = "not a column"

    privacy = Privacy()
    with pytest.raises(PrivacyDeclarationError, match="it declares caption, id"):
        privacy.declare(Photo)
