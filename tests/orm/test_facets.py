"""Declarations that subsystems outside the ORM attach to a model.

`Facet` is one mechanism, not one per subsystem: the audit trail's
`audited(redact=...)` and a privacy classification are the same shape with
different payloads. What the ORM contributes -- and what each subsystem would
otherwise get wrong on its own -- is the two checks below: the column names are
validated when the class is created, and two facets cannot share a namespace.
"""

from __future__ import annotations

import pytest

from wreath.orm import Model, column, facet
from wreath.orm.errors import DeclarationError
from wreath.orm.table import Facet
from wreath.orm.types import Int64, Text


class Classified(Facet):
    """A stand-in for a subsystem's own facet, so this suite owns its fixtures."""

    __slots__ = ()
    namespace = "classified"


class Tagged(Facet):
    __slots__ = ()
    namespace = "tagged"


def test_a_facet_is_found_by_type_not_by_attribute_name():
    class Photo(Model, table="photos_facet_1"):
        id: int = column(Int64, primary_key=True)
        caption: str = column(Text)

        # The attribute name is documentation, exactly as for unique() and
        # index(). Two different names, both collected.
        _whatever_i_call_it = Classified(("caption",))

    assert facet(Photo, "classified").columns == ("caption",)


def test_a_model_with_no_facet_answers_none_rather_than_raising():
    class Plain(Model, table="photos_facet_2"):
        id: int = column(Int64, primary_key=True)

    # `None` rather than a raise: "not audited" is the ordinary answer for most
    # models, and a raise would put a try around every read.
    assert facet(Plain, "classified") is None


def test_two_subsystems_declare_side_by_side_without_colliding():
    class Photo(Model, table="photos_facet_3"):
        id: int = column(Int64, primary_key=True)
        caption: str = column(Text)

        _a = Classified(("caption",))
        _b = Tagged(())

    assert facet(Photo, "classified") is not None
    assert facet(Photo, "tagged") is not None


def test_a_facet_naming_a_column_the_model_does_not_declare_is_refused():
    # The check that pays for the whole mechanism. Without it a classification
    # naming a column renamed two migrations ago is not an error -- it is a
    # redaction that quietly stopped covering anything.
    with pytest.raises(DeclarationError, match="names a column 'exif' that .* does not declare"):

        class Photo(Model, table="photos_facet_4"):
            id: int = column(Int64, primary_key=True)
            caption: str = column(Text)

            _a = Classified(("exif",))


def test_the_refusal_names_the_namespace_and_the_columns_that_do_exist():
    with pytest.raises(DeclarationError, match="classified facet") as caught:

        class Photo(Model, table="photos_facet_5"):
            id: int = column(Int64, primary_key=True)
            caption: str = column(Text)

            _a = Classified(("nope",))

    # Both halves, so the message is actionable rather than merely correct.
    assert "caption, id" in str(caught.value)


def test_a_facet_with_no_namespace_is_refused_where_it_is_built():
    class Nameless(Facet):
        __slots__ = ()

    with pytest.raises(DeclarationError, match="must set a class-level namespace"):
        Nameless(())


def test_a_subclass_facet_replaces_the_one_it_inherits():
    class Base(Model):
        id: int = column(Int64, primary_key=True)
        caption: str = column(Text)

        _a = Classified(("caption",))

    class Narrower(Base, table="photos_facet_6"):
        _a = Classified(())

    # Replaced, not accumulated: a base's declaration is a default and a
    # subclass restating it means it. Two descriptions of one thing is a
    # question nobody should have to answer at read time.
    assert facet(Narrower, "classified").columns == ()
    assert len(Narrower.__wreath_proto_facets__) == 1


def test_an_inherited_facet_is_kept_when_the_subclass_declares_none():
    class Base(Model):
        id: int = column(Int64, primary_key=True)
        caption: str = column(Text)

        _a = Classified(("caption",))

    class Plain(Base, table="photos_facet_7"):
        pass

    assert facet(Plain, "classified").columns == ("caption",)


def test_an_inherited_facet_is_validated_against_the_subclass_columns():
    # The base is table-less, so nothing checked its column names there. The
    # subclass is where the model becomes real, and where the check has to run.
    class Base(Model):
        _a = Classified(("gone",))

    with pytest.raises(DeclarationError, match="names a column 'gone'"):

        class Photo(Base, table="photos_facet_8"):
            id: int = column(Int64, primary_key=True)
