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
    privacy = Privacy(_registry(Receipt, Invoice))
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Receipt, subject="person_id", personal={"address": Erase.REDACT})
    privacy.classify(Invoice, subject="person_id", personal={"payer": Erase.REDACT})

    details = {risk.edge.from_table: risk.detail for risk in privacy.plan("4711").orphan_risks}
    assert "sets public.receipts.person_id to NULL" in details["public.receipts"]
    assert "sets public.invoices.person_id to its default" in details["public.invoices"]


def test_an_orphaning_edge_onto_a_parent_nothing_deletes_is_not_a_risk() -> None:
    privacy = Privacy(_registry(Receipt))
    privacy.subject(Person, key="id")  # anonymised, not deleted
    privacy.classify(Receipt, subject="person_id", personal={"address": Erase.REDACT})
    assert privacy.plan("4711").orphan_risks == ()


def test_a_blocking_edge_onto_a_deleted_parent_is_not_an_orphan_risk() -> None:

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


def test_a_reference_to_a_parent_that_survives_is_not_reported() -> None:

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
    assert [item.edge.to_table for item in plan.surviving_references] == ["public.people"]


def test_a_blocked_plan_names_every_kind_of_blocking_finding_it_has() -> None:

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


class Orphaned(Model, table="orphaned"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    contact_string: Mapped[str] = column(Text)


def test_an_unreachable_table_with_no_personal_columns_is_not_a_finding() -> None:
    privacy = Privacy(_registry(Orphaned))
    privacy.subject(Person, key="id")
    privacy.classify(Orphaned)
    plan = privacy.plan("4711")
    assert plan.unreachable == ()
    assert plan.blocked is False


def test_a_classified_model_outside_the_registry_says_which_gap_it_is() -> None:

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


def _notes(privacy: Privacy) -> str:
    return "\n".join(privacy.plan("4711").notes)


def test_a_clean_plan_carries_only_the_backup_note() -> None:

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
    privacy.classify(Ledger, personal={"payer": Erase.REDACT}, exempt="a financial record")
    text = _notes(privacy)
    assert "1 table(s) retain the subject's data" in text
    assert "1 classified table(s) hold personal data this traversal cannot reach" in text
    assert "1 foreign key(s) point at rows this plan deletes" in text
    assert "1 edge(s) would orphan personal data" in text


def test_the_notes_name_a_pseudonymised_column_and_only_a_pseudonymised_one() -> None:
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

    def _loop(*, deferrable: bool) -> Privacy:
        suffix = "def" if deferrable else "plain"

        class Household(Model, table=f"households_{suffix}"):
            id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
            head_id: Mapped[int | None] = column(Int64, nullable=True)
            label: Mapped[str] = column(Text)

        class Member(Model, table=f"members_{suffix}"):
            id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
            household_id: Mapped[int] = column(
                Int64, references=Household.id, deferrable=deferrable
            )
            person_id: Mapped[int] = column(Int64, references=Person.id, deferrable=deferrable)
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


def test_a_registry_with_no_compiled_models_refuses_rather_than_answering() -> None:

    class Empty:
        specs = ()

    with pytest.raises(ValueError, match="no compiled models"):
        build_graph(Empty())
    with pytest.raises(ValueError, match="no compiled models"):
        build_graph(object())


def test_an_edge_to_a_model_the_registry_does_not_compile_is_dropped() -> None:

    class Away(Model, table="away"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")

    class Here(Model, table="here"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        away_id: Mapped[int] = column(Int64, references=Away.id)
        person_id: Mapped[int] = column(Int64, references=Person.id)

    graph = build_graph(Registry(FakeDatabase(), [Person, Here], validate_schema="off"))
    assert [edge.to_table for edge, _target in graph.outbound[Here]] == ["public.people"]


def test_a_self_reference_does_not_make_a_table_wait_for_itself() -> None:
    from wreath._privacy.graph import order_children_first

    class Folder(Model, table="folders_order"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        parent_id: Mapped[int | None] = column(Int64, nullable=True)

    Folder.__wreath_column_map__["parent_id"].references = Folder.id
    graph = build_graph(Registry(FakeDatabase(), [Person, Folder], validate_schema="off"))
    ordered, cycles = order_children_first(graph, {Folder})
    assert ordered == [Folder]
    assert cycles == []


def test_an_export_plan_carries_no_blocked_or_digest_field() -> None:

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
        models.append(type(f"{name}{index}", (Model,), namespace, table=f"{name}_{index}"))
    for index, model in enumerate(models):
        target = models[(index + 1) % count]
        model.__wreath_column_map__["next_id"].references = target.id
    privacy = Privacy(_registry(*models))
    privacy.subject(Person, key="id")
    for model in models:
        privacy.classify(model, personal={"label": Erase.REDACT})
    return privacy, models


def test_two_separate_loops_are_two_components_rather_than_one() -> None:
    from wreath._privacy.graph import order_children_first

    first, left = _looped("ring_a", 2)
    second, right = _looped("ring_b", 3)
    graph = build_graph(Registry(FakeDatabase(), [Person, *left, *right], validate_schema="off"))
    ordered, cycles = order_children_first(graph, {*left, *right})
    assert ordered == []
    assert sorted(len(group) for group in cycles) == [2, 3]
    assert {model for group in cycles for model in group} == {*left, *right}
    assert all(
        len({graph.nodes[model].table.rsplit("_", 1)[0] for model in group}) == 1
        for group in cycles
    ), "a component must not mix the two rings"


def test_a_table_downstream_of_a_loop_is_not_part_of_it() -> None:
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
    from wreath._privacy.graph import order_children_first
    from wreath._privacy.planner import _cycle_findings

    _privacy, models = _looped("ring_d", 2)
    for model in models:
        model.__wreath_column_map__["next_id"].deferrable = True
    graph = build_graph(Registry(FakeDatabase(), [Person, *models], validate_schema="off"))
    _ordered, groups = order_children_first(graph, set(models))
    findings = _cycle_findings(graph, groups)
    assert [finding.deferrable for finding in findings] == [True]
    assert "every foreign key in the loop is DEFERRABLE" in findings[0].detail


def test_a_registry_whose_specs_are_none_is_refused_like_an_empty_one() -> None:

    class NeverCompiled:
        specs = None

    with pytest.raises(ValueError, match="no compiled models"):
        build_graph(NeverCompiled())


def test_an_export_lists_a_table_with_no_columns_unless_a_cascade_removes_it() -> None:

    class Bare(Model, table="bare_reached"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        owner_id: Mapped[int] = column(Int64, references=Person.id)

    privacy = Privacy(_registry(Bare))
    privacy.subject(Person, key="id")  # anonymised, so nothing cascades
    privacy.classify(Bare, subject="owner_id")
    text = privacy.render(privacy.access("4711"))
    assert "bare_reached" in text


def test_an_export_names_the_tables_it_cannot_reach() -> None:
    privacy = Privacy(_registry(Orphaned))
    privacy.subject(Person, key="id")
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    text = privacy.render(privacy.access("4711"))
    assert "Unreachable (1)" in text
    assert "Orphaned: no foreign-key path" in text
    assert text.count("  none.") == 1, "only the withheld section is empty"
