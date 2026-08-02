"""The findings' edges: what is reported, what is *not*, and what the notes say.

`tests/test_privacy_plan.py` proves each finding fires. This file proves the
other half, which is where a findings engine actually goes wrong: a report that
fires on everything is as useless as one that fires on nothing, and the note at
the bottom of a plan is the only part some readers get to. Every test here
pairs a case that must be reported with one that must not.

No database. Every assertion is about a decision this module makes.
"""

from __future__ import annotations

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.privacy import (
    Disposal,
    Erase,
    ErasureBlocked,
    Privacy,
    Pseudonymise,
    as_dict,
    build_graph,
)


class FakeDatabase:
    name = "main"


class Person(Model, table="people"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    email: Mapped[str] = column(Text)


def _registry(*models: type) -> Registry:
    return Registry(FakeDatabase(), [Person, *models], validate_schema="off")


# -- orphan risks: the edge, the parent, and the wording ----------------------


class Receipt(Model, table="receipts"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    person_id: Mapped[int | None] = column(
        Int64, references=Person.id, on_delete="set null", nullable=True
    )
    address: Mapped[str] = column(Text)


class Invoice(Model, table="invoices"):
    """A `SET DEFAULT` child: the other half of the orphaning pair."""

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    person_id: Mapped[int | None] = column(
        Int64, references=Person.id, on_delete="set default", nullable=True
    )
    payer: Mapped[str] = column(Text)


def test_a_set_default_edge_says_its_default_and_a_set_null_one_says_null() -> None:
    """Two referential actions, two different things happen to the child row.

    A reader who is told "set to NULL" for a `SET DEFAULT` edge goes looking
    for null rows and finds none, which is worse than being told nothing.
    """
    privacy = Privacy(_registry(Receipt, Invoice))
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Receipt, subject="person_id", personal={"address": Erase.REDACT})
    privacy.classify(Invoice, subject="person_id", personal={"payer": Erase.REDACT})

    details = {
        risk.edge.from_table: risk.detail for risk in privacy.plan("4711").orphan_risks
    }
    assert "sets public.receipts.person_id to NULL" in details["public.receipts"]
    assert "sets public.invoices.person_id to its default" in details["public.invoices"]


def test_an_orphaning_edge_onto_a_parent_nothing_deletes_is_not_a_risk() -> None:
    """A `SET NULL` edge is an ordinary schema choice until something deletes.

    Reporting every one of them would bury the real finding in noise, which is
    the way a findings engine stops being read.
    """
    privacy = Privacy(_registry(Receipt))
    privacy.subject(Person, key="id")  # anonymised, not deleted
    privacy.classify(Receipt, subject="person_id", personal={"address": Erase.REDACT})
    assert privacy.plan("4711").orphan_risks == ()


def test_a_blocking_edge_onto_a_deleted_parent_is_not_an_orphan_risk() -> None:
    """`NO ACTION` strands nothing; it refuses. Different finding, different fix."""

    class Kept(Model, table="kept_rows"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        note: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Kept))
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Kept, subject="person_id", personal={"note": Erase.REDACT})
    plan = privacy.plan("4711")
    assert plan.orphan_risks == ()
    assert len(plan.surviving_references) == 1


# -- surviving references: which parent, and which child ----------------------


def test_a_reference_to_a_parent_that_survives_is_not_reported() -> None:
    """The finding is about pointing at a row that goes away.

    Two edges from one surviving table, one to a deleted parent and one to a
    surviving one; only the first is a finding, and a check that looked only at
    the referential action would report both.
    """

    class Camera(Model, table="cameras"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        label: Mapped[str] = column(Text)

    class Photo(Model, table="photos"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id)
        camera_id: Mapped[int] = column(Int64, references=Camera.id)
        caption: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Camera, Photo))
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})

    plan = privacy.plan("4711")
    assert [item.edge.to_table for item in plan.surviving_references] == [
        "public.people"
    ]


def test_a_blocked_plan_names_every_kind_of_blocking_finding_it_has() -> None:
    """The refusal counts each kind, so an operator knows what to go and fix."""

    class Kept(Model, table="kept_two"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        note: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Kept))
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Kept, subject="person_id", personal={"note": Erase.REDACT})
    with pytest.raises(ErasureBlocked) as caught:
        privacy.prepare("4711")
    assert "0 unreachable classified table(s)" in str(caught.value)
    assert "0 blocking foreign-key cycle(s)" in str(caught.value)
    assert "1 surviving reference(s)" in str(caught.value)


# -- unreachable: classified, and holding something ---------------------------


class Orphaned(Model, table="orphaned"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    contact_string: Mapped[str] = column(Text)


def test_an_unreachable_table_with_no_personal_columns_is_not_a_finding() -> None:
    """"Classified" is not the trigger; *holding personal data* is.

    A model somebody registered to record that it holds nothing is exactly the
    thing that should not appear as a finding, and it is the shape a
    classification sweep produces most.
    """
    privacy = Privacy(_registry(Orphaned))
    privacy.subject(Person, key="id")
    privacy.classify(Orphaned)
    plan = privacy.plan("4711")
    assert plan.unreachable == ()
    assert plan.blocked is False


def test_a_classified_model_outside_the_registry_says_which_gap_it_is() -> None:
    """Unmapped and unconnected need different fixes, so they read differently."""

    class Elsewhere(Model, table="elsewhere"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        note: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Orphaned))
    privacy.subject(Person, key="id")
    privacy.classify(Elsewhere, personal={"note": Erase.REDACT})
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})

    reasons = {item.model: item.reason for item in privacy.plan("4711").unreachable}
    assert "not compiled into this ORM registry" in reasons["Elsewhere"]
    assert "no foreign-key path" in reasons["Orphaned"]
    assert [item.table for item in privacy.plan("4711").unreachable] == ["", "orphaned"]


# -- the notes: the part of the plan a hurried reader actually reads ----------


def _notes(privacy: Privacy) -> str:
    return "\n".join(privacy.plan("4711").notes)


def test_a_clean_plan_carries_only_the_backup_note() -> None:
    """Every other note is a finding, so a clean plan must not grow one."""

    class Photo(Model, table="photos_clean"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id)
        caption: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Photo))
    privacy.subject(Person, key="id")
    privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    notes = privacy.plan("4711").notes
    assert len(notes) == 1
    assert notes[0].startswith("Backups are out of scope")


def test_the_notes_name_each_finding_that_is_present() -> None:
    """One plan with four findings in it, and four sentences that say so."""
    class Kept(Model, table="kept_three"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        note: Mapped[Text] = column(Text)

    class Ledger(Model, table="ledger_notes"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int | None] = column(
            Int64, references=Person.id, on_delete="set null", nullable=True
        )
        payer: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Kept, Ledger, Receipt, Orphaned))
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Kept, subject="person_id", personal={"note": Erase.REDACT})
    privacy.classify(Receipt, subject="person_id", personal={"address": Erase.REDACT})
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    privacy.classify(
        Ledger, personal={"payer": Erase.REDACT}, exempt="a financial record"
    )
    text = _notes(privacy)
    assert "1 table(s) retain the subject's data" in text
    assert "1 classified table(s) hold personal data this traversal cannot reach" in text
    assert "1 foreign key(s) point at rows this plan deletes" in text
    assert "1 edge(s) would orphan personal data" in text


def test_the_notes_name_a_pseudonymised_column_and_only_a_pseudonymised_one() -> None:
    """The note exists to say "this is not erasure", so it must list the right ones."""
    from wreath.passes import Declared

    class Ticket(Model, table="tickets"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        body: Mapped[str] = column(Text)
        subject_line: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Ticket))
    privacy.subject(Person, key="id")
    privacy.classify(
        Ticket,
        subject="person_id",
        personal={
            "body": Pseudonymise(Declared("support needs ticket continuity")),
            "subject_line": Erase.REDACT,
        },
    )
    text = _notes(privacy)
    assert "Pseudonymised, not erased: tickets.body." in text
    assert "subject_line" not in text


def test_a_blocking_cycle_note_and_a_deferrable_one_read_differently() -> None:
    """A deferrable loop is runnable and a plain one is not; the plan must not blur it."""

    def _loop(*, deferrable: bool) -> Privacy:
        suffix = "def" if deferrable else "plain"

        class Household(Model, table=f"households_{suffix}"):
            id: Mapped[int] = column(
                Int64, primary_key=True, server_default="nextval('seq')"
            )
            head_id: Mapped[int | None] = column(Int64, nullable=True)
            label: Mapped[str] = column(Text)

        class Member(Model, table=f"members_{suffix}"):
            id: Mapped[int] = column(
                Int64, primary_key=True, server_default="nextval('seq')"
            )
            household_id: Mapped[int] = column(
                Int64, references=Household.id, deferrable=deferrable
            )
            person_id: Mapped[int] = column(
                Int64, references=Person.id, deferrable=deferrable
            )
            nickname: Mapped[str] = column(Text)

        Household.__wreath_column_map__["head_id"].references = Member.id
        Household.__wreath_column_map__["head_id"].deferrable = deferrable
        privacy = Privacy(_registry(Household, Member))
        privacy.subject(Person, key="id")
        privacy.classify(Household, personal={"label": Erase.REDACT})
        privacy.classify(Member, personal={"nickname": Erase.REDACT})
        return privacy

    blocking = _loop(deferrable=False).plan("4711")
    assert blocking.cycles[0].deferrable is False
    assert "no ordering of plain deletes exists" in blocking.cycles[0].detail
    assert "BLOCKED: a foreign-key cycle" in "\n".join(blocking.notes)

    carried = _loop(deferrable=True).plan("4711")
    assert carried.cycles[0].deferrable is True
    assert "every foreign key in the loop is DEFERRABLE" in carried.cycles[0].detail
    assert "BLOCKED: a foreign-key cycle" not in "\n".join(carried.notes)
    assert carried.blocked is False


# -- the graph the findings are derived from ----------------------------------


def test_a_registry_with_no_compiled_models_refuses_rather_than_answering() -> None:
    """An empty graph would make every table unreachable and every plan wrong."""

    class Empty:
        specs = ()

    with pytest.raises(ValueError, match="no compiled models"):
        build_graph(Empty())
    with pytest.raises(ValueError, match="no compiled models"):
        build_graph(object())


def test_an_edge_to_a_model_the_registry_does_not_compile_is_dropped() -> None:
    """It cannot be walked, and `unmodelled_edges` reports the same hole live."""

    class Away(Model, table="away"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")

    class Here(Model, table="here"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        away_id: Mapped[int] = column(Int64, references=Away.id)
        person_id: Mapped[int] = column(Int64, references=Person.id)

    graph = build_graph(Registry(FakeDatabase(), [Person, Here], validate_schema="off"))
    assert [edge.to_table for edge, _target in graph.outbound[Here]] == ["public.people"]


def test_a_self_reference_does_not_make_a_table_wait_for_itself() -> None:
    """A table that references itself would otherwise never become orderable.

    Without the `parent is not child` clause the topological sort records the
    table as its own dependency, never places it, and reports a cycle where
    there is only a hierarchy.
    """
    from wreath._privacy.graph import order_children_first

    class Folder(Model, table="folders_order"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        parent_id: Mapped[int | None] = column(Int64, nullable=True)

    Folder.__wreath_column_map__["parent_id"].references = Folder.id
    graph = build_graph(Registry(FakeDatabase(), [Person, Folder], validate_schema="off"))
    ordered, cycles = order_children_first(graph, {Folder})
    assert ordered == [Folder]
    assert cycles == []


# -- the plan as data ---------------------------------------------------------


def test_an_export_plan_carries_no_blocked_or_digest_field() -> None:
    """`blocked` and `digest` are erasure properties; an export has neither.

    `dataclasses.asdict` drops properties, so both are re-added by hand -- and
    adding them to the wrong plan type would invent a verdict for a read.
    """

    class Photo(Model, table="photos_export"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id)
        caption: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Photo))
    privacy.subject(Person, key="id")
    privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})

    erasure = as_dict(privacy.plan("4711"))
    export = as_dict(privacy.access("4711"))
    assert {"blocked", "digest"} <= erasure.keys()
    assert not {"blocked", "digest"} & export.keys()


def test_the_text_rendering_states_every_absence_and_every_presence() -> None:
    """Both directions of the renderer, because a silence reads as "nothing wrong"."""

    class Photo(Model, table="photos_render"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id)
        caption: Mapped[str] = column(Text)

    class Ledger(Model, table="ledger_render"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        payer: Mapped[str] = column(Text)

    privacy = Privacy(_registry(Photo))
    privacy.subject(Person, key="id")
    privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    clean = privacy.render(privacy.plan("4711"))
    assert "none: no table claims an exemption from this erasure." in clean
    assert "Ready. Quote this digest" in clean

    with_exemption = Privacy(_registry(Photo, Ledger))
    with_exemption.subject(Person, key="id")
    with_exemption.classify(Ledger, personal={"payer": Erase.REDACT}, exempt="tax law")
    text = with_exemption.render(with_exemption.plan("4711"))
    assert "Retained under exemption (1)" in text
    assert "reason:  tax law" in text
    assert "none: no table claims an exemption" not in text


def test_the_export_rendering_states_every_absence_and_every_presence() -> None:
    """The subject-access view has its own three sections and its own silences."""

    class Photo(Model, table="photos_export_two"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id, on_delete="cascade")
        caption: Mapped[str] = column(Text)

    class Bare(Model, table="bare_export"):
        """Cascaded and carrying nothing: nothing extra to export."""

        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id, on_delete="cascade")

    privacy = Privacy(_registry(Photo, Bare))
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    privacy.classify(Bare, subject="owner_id")

    plan = privacy.access("4711")
    assert {item.table for item in plan.tables} >= {"photos_export_two", "bare_export"}
    assert (
        next(item for item in plan.tables if item.table == "bare_export").disposal
        == Disposal.CASCADE.value
    )
    text = privacy.render(plan)
    assert "photos_export_two" in text, "a cascaded table with columns is still exported"
    assert "bare_export" not in text, "a cascaded table with nothing carries nothing"
    assert text.count("  none.") == 2, "withheld and unreachable both say none"


# -- the graph walk that finds the cycles -------------------------------------


def _looped(name: str, count: int) -> tuple[Privacy, list[type]]:
    """A ring of `count` tables, each referencing the next, all reachable."""
    models: list[type] = []
    for index in range(count):
        namespace = {
            "__annotations__": {},
            "id": column(Int64, primary_key=True, server_default="nextval('seq')"),
            "person_id": column(Int64, references=Person.id),
            "next_id": column(Int64, nullable=True),
            "label": column(Text),
        }
        models.append(
            type(f"{name}{index}", (Model,), namespace, table=f"{name}_{index}")
        )
    for index, model in enumerate(models):
        target = models[(index + 1) % count]
        model.__wreath_column_map__["next_id"].references = target.id
    privacy = Privacy(_registry(*models))
    privacy.subject(Person, key="id")
    for model in models:
        privacy.classify(model, personal={"label": Erase.REDACT})
    return privacy, models


def test_two_separate_loops_are_two_components_rather_than_one() -> None:
    """Each loop is its own finding, and merging them names the wrong tables.

    Driven through `order_children_first` directly rather than through a plan,
    because every unorderable group in a *plan* hangs off the one subject root
    and is therefore connected through it. Two genuinely disjoint loops are a
    property of the ordering, so that is where they are asserted.
    """
    from wreath._privacy.graph import order_children_first

    first, left = _looped("ring_a", 2)
    second, right = _looped("ring_b", 3)
    graph = build_graph(
        Registry(FakeDatabase(), [Person, *left, *right], validate_schema="off")
    )
    ordered, cycles = order_children_first(graph, {*left, *right})
    assert ordered == []
    assert sorted(len(group) for group in cycles) == [2, 3]
    assert {model for group in cycles for model in group} == {*left, *right}
    assert all(
        len({graph.nodes[model].table.rsplit("_", 1)[0] for model in group}) == 1
        for group in cycles
    ), "a component must not mix the two rings"


def test_a_table_downstream_of_a_loop_is_not_part_of_it() -> None:
    """Reached *from* a loop is not in it, and the fix is a different edge."""
    from wreath._privacy.graph import order_children_first

    _privacy, models = _looped("ring_c", 2)

    class Downstream(Model, table="ring_c_downstream"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        ring_id: Mapped[int] = column(Int64, references=models[0].id)
        note: Mapped[str] = column(Text)

    graph = build_graph(
        Registry(FakeDatabase(), [Person, *models, Downstream], validate_schema="off")
    )
    ordered, cycles = order_children_first(graph, {*models, Downstream})
    assert ordered == [Downstream], "the only table nothing in the loop blocks"
    assert [sorted(graph.nodes[model].table for model in group) for group in cycles] == [
        ["ring_c_0", "ring_c_1"]
    ]


def test_the_cycle_detail_counts_only_the_edges_inside_the_loop() -> None:
    """An edge out of the loop must not decide whether the loop is deferrable.

    Both members reference `people` with a plain foreign key while the loop's
    own edges are deferrable: counting the outward ones would report the loop
    as blocking and refuse an erasure that can run.
    """
    from wreath._privacy.graph import order_children_first
    from wreath._privacy.planner import _cycle_findings

    _privacy, models = _looped("ring_d", 2)
    for model in models:
        model.__wreath_column_map__["next_id"].deferrable = True
    graph = build_graph(
        Registry(FakeDatabase(), [Person, *models], validate_schema="off")
    )
    _ordered, groups = order_children_first(graph, set(models))
    findings = _cycle_findings(graph, groups)
    assert [finding.deferrable for finding in findings] == [True]
    assert "every foreign key in the loop is DEFERRABLE" in findings[0].detail


def test_a_registry_whose_specs_are_none_is_refused_like_an_empty_one() -> None:
    """`specs=None` is a registry that was never compiled, not one with no models."""

    class NeverCompiled:
        specs = None

    with pytest.raises(ValueError, match="no compiled models"):
        build_graph(NeverCompiled())


# -- the export view's own silences -------------------------------------------


def test_an_export_lists_a_table_with_no_columns_unless_a_cascade_removes_it() -> None:
    """Two reasons a table can carry nothing, and only one of them is skipped.

    A reached table with nothing classified is still the subject's row and is
    exported; a cascaded one with nothing classified is gone by then. Collapsing
    the two would drop a table out of a subject-access response.
    """

    class Bare(Model, table="bare_reached"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id)

    privacy = Privacy(_registry(Bare))
    privacy.subject(Person, key="id")  # anonymised, so nothing cascades
    privacy.classify(Bare, subject="owner_id")
    text = privacy.render(privacy.access("4711"))
    assert "bare_reached" in text


def test_an_export_names_the_tables_it_cannot_reach() -> None:
    """An unreachable table is a gap in a subject-access response too.

    The subject is entitled to know the answer is incomplete, so the section
    lists them rather than saying none.
    """
    privacy = Privacy(_registry(Orphaned))
    privacy.subject(Person, key="id")
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    text = privacy.render(privacy.access("4711"))
    assert "Unreachable (1)" in text
    assert "Orphaned: no foreign-key path" in text
    assert text.count("  none.") == 1, "only the withheld section is empty"
