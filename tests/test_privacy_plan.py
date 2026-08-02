"""The erasure planner, and the four ways an erasure silently misses data.

Every test here runs against a compiled ORM registry and **no database**, which
is the property the module promises: a plan is derived from declarations, so
`wreath privacy plan` is safe to run against an application whose database is
not reachable. A test that needed a socket would be evidence the planner had
grown one.

The falsification tests are the point of the file. A planner that reports a
tidy list of tables and quietly drops a cycle, an orphaning edge or an
unreachable table is worse than no planner, because the tidy list gets believed.
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
    PlanMoved,
    Privacy,
    PrivacyDeclarationError,
    Pseudonymise,
)


class FakeDatabase:
    """Enough of `wreath.postgres.Database` for `Registry` to compile."""

    name = "main"

    def __init__(self, name: str = "main") -> None:
        self.name = name


# -- a schema with all four hazards in it -------------------------------------


class Person(Model, table="people"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    email: Mapped[str] = column(Text, unique=True)


class Photo(Model, table="photos"):
    """Reached directly: it declares the subject column itself."""

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    owner_id: Mapped[int] = column(Int64, references=Person.id, on_delete="cascade")
    caption: Mapped[str] = column(Text)


class Comment(Model, table="comments"):
    """Reached at depth two, through `Photo`.

    `SET NULL` rather than the default `NO ACTION`, and that is forced rather
    than decorative: a comment survives this erasure (its body is redacted, the
    row stays) while the photo it points at is removed by the subject's own
    cascade, and a `NO ACTION` edge there is one the database refuses. The plan
    reports exactly that as a surviving reference, so a schema that had it
    would be `blocked` and could not be used to build passes at all.
    """

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    photo_id: Mapped[int | None] = column(
        Int64, references=Photo.id, on_delete="set null", nullable=True
    )
    body: Mapped[str] = column(Text)


class Receipt(Model, table="receipts"):
    """A `SET NULL` child: the orphan hazard."""

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    person_id: Mapped[int | None] = column(
        Int64, references=Person.id, on_delete="set null", nullable=True
    )
    address: Mapped[str] = column(Text)


class Ledger(Model, table="ledger"):
    """Exempt: a financial record that survives an erasure.

    Its foreign key is `SET NULL` for the same reason `Comment`'s is: a row
    retained under an exemption is a row that outlives the subject, so an edge
    the database would refuse to break is an edge that makes the erasure
    impossible rather than merely awkward.
    """

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    person_id: Mapped[int | None] = column(
        Int64, references=Person.id, on_delete="set null", nullable=True
    )
    amount: Mapped[int] = column(Int64)
    payer_name: Mapped[str] = column(Text)


class Orphaned(Model, table="orphaned"):
    """Classified personal data with no foreign key to anything: unreachable."""

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
    contact_string: Mapped[str] = column(Text)


ALL_MODELS = (Person, Photo, Comment, Receipt, Ledger, Orphaned)


@pytest.fixture
def orm() -> Registry:
    return Registry(FakeDatabase(), list(ALL_MODELS), validate_schema="off")


@pytest.fixture
def privacy(orm: Registry) -> Privacy:
    item = Privacy(orm)
    item.subject(Person, key="id", delete=True)
    item.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    item.classify(Comment, personal={"body": Erase.REDACT})
    item.classify(Receipt, subject="person_id", personal={"address": Erase.REDACT})
    item.classify(
        Ledger,
        personal={"payer_name": Erase.REDACT},
        exempt="retained seven years under tax law",
    )
    return item


# -- reachability -------------------------------------------------------------


def test_a_table_that_declares_the_subject_column_matches_directly(
    privacy: Privacy,
) -> None:
    plan = privacy.plan("4711")
    photo = next(item for item in plan.tables if item.table == "photos")
    assert photo.match_column == "owner_id"
    assert photo.reach.depth == 1


def test_a_deeper_table_records_the_path_it_was_reached_by(privacy: Privacy) -> None:
    plan = privacy.plan("4711")
    comment = next(item for item in plan.tables if item.table == "comments")
    assert comment.reach.depth == 2
    described = comment.reach.describe()
    assert "public.photos.owner_id -> public.people.id" in described
    assert "public.comments.photo_id -> public.photos.id" in described


def test_children_are_ordered_before_the_parent_they_reference(
    privacy: Privacy,
) -> None:
    plan = privacy.plan("4711")
    order = {item.table: item.order for item in plan.tables}
    assert order["comments"] < order["photos"], "a child must be handled first"
    assert order["photos"] < order["people"]
    assert order["receipts"] < order["people"]


# -- falsification: the four findings -----------------------------------------


def test_an_unreachable_classified_table_is_named_and_blocks(
    privacy: Privacy,
) -> None:
    """The finding this module exists to produce.

    `Orphaned` holds a column somebody classified and no foreign key connects
    it to a subject. An erasure that ran anyway would report success and leave
    the rows, which is the EDPB's "incomplete response" in one table.
    """
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    plan = privacy.plan("4711")
    assert [item.table for item in plan.unreachable] == ["orphaned"]
    assert "contact_string" in plan.unreachable[0].columns
    assert "no foreign-key path" in plan.unreachable[0].reason
    assert plan.blocked is True


def test_an_orphaning_edge_is_named_with_the_reason_it_strands_data(
    privacy: Privacy,
) -> None:
    plan = privacy.plan("4711")
    risks = [risk for risk in plan.orphan_risks if risk.edge.from_table.endswith("receipts")]
    assert risks, "a SET NULL edge onto a deleted parent must be reported"
    assert risks[0].edge.on_delete == "n"
    assert "never be found again" in risks[0].detail


def test_a_foreign_key_cycle_is_named_rather_than_ordered(orm: Registry) -> None:
    """A cycle admits no ordering of plain deletes, so it is reported.

    Declared inside the test because a cyclic pair would otherwise make every
    other plan in the file blocked.
    """

    class Household(Model, table="households"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        head_id: Mapped[int | None] = column(Int64, nullable=True)
        label: Mapped[str] = column(Text)

    class Member(Model, table="members"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        household_id: Mapped[int] = column(Int64, references=Household.id)
        person_id: Mapped[int] = column(Int64, references=Person.id)
        nickname: Mapped[str] = column(Text)

    Household.__wreath_column_map__["head_id"].references = Member.id
    Household.__wreath_column_map__["head_id"].on_delete = "no action"
    registry = Registry(FakeDatabase(), [Person, Household, Member], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Household, personal={"label": Erase.REDACT})
    privacy.classify(Member, personal={"nickname": Erase.REDACT})

    plan = privacy.plan("4711")
    assert plan.cycles, "a households <-> members loop must be reported"
    names = plan.cycles[0].tables
    assert any("households" in name for name in names)
    assert any("members" in name for name in names)
    assert plan.cycles[0].deferrable is False
    assert plan.blocked is True


def test_a_surviving_row_that_still_references_a_deleted_one_blocks(
    orm: Registry,
) -> None:
    """The finding that turns a green plan into a half-run erasure.

    Ordering answers `NO ACTION` only when the child rows are deleted too. A
    child that is *anonymised* keeps its foreign key, so PostgreSQL refuses the
    parent's delete -- and it refuses it after the children have already been
    redacted, which is the worst place to stop.
    """

    class Kept(Model, table="kept"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        note: Mapped[str] = column(Text)

    registry = Registry(FakeDatabase(), [Person, Kept], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(Kept, subject="person_id", personal={"note": Erase.REDACT})

    plan = privacy.plan("4711")
    assert [item.edge.from_table for item in plan.surviving_references] == ["public.kept"]
    assert "refuses the delete" in plan.surviving_references[0].detail
    assert plan.blocked is True
    assert "BLOCKED" in privacy.render(plan)
    with pytest.raises(ErasureBlocked):
        privacy.prepare("4711")


def test_a_table_nobody_classified_can_be_the_one_that_refuses_the_delete(
    orm: Registry,
) -> None:
    """Holding a foreign key has nothing to do with being personal data.

    Reported over the whole graph rather than over the plan's tables, because
    the row that blocks the delete is very often one no declaration mentions --
    and a finding that only looked at classified tables would miss exactly the
    case nobody was thinking about.
    """

    class Unclassified(Model, table="unclassified"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)

    registry = Registry(FakeDatabase(), [Person, Unclassified], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id", delete=True)

    plan = privacy.plan("4711")
    assert [item.edge.from_table for item in plan.surviving_references] == [
        "public.unclassified"
    ]
    assert plan.blocked is True


def test_a_child_the_erasure_also_deletes_is_not_a_surviving_reference(
    orm: Registry,
) -> None:
    """Ordering *does* answer `NO ACTION` when the child goes too.

    The whole point of children-first: the referencing rows are gone by the
    time the parent's delete runs, so this edge is not a finding and reporting
    it would bury the real ones in noise.
    """

    class Session(Model, table="sessions"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        token: Mapped[str] = column(Text)

    registry = Registry(FakeDatabase(), [Person, Session], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id", delete=True)
    privacy.classify(
        Session, subject="person_id", personal={"token": Erase.REDACT}, delete=True
    )

    plan = privacy.plan("4711")
    assert plan.surviving_references == ()
    assert plan.blocked is False


def test_a_set_null_edge_is_an_orphan_risk_and_not_a_surviving_reference(
    privacy: Privacy,
) -> None:
    """The two findings are about different failures and must not merge.

    `SET NULL` lets the delete through and strands the child; `NO ACTION` stops
    the delete. Reporting one as the other would send a reader to the wrong fix.
    """
    plan = privacy.plan("4711")
    assert plan.orphan_risks
    assert plan.surviving_references == ()
    assert plan.blocked is False


def test_nothing_survives_a_reference_when_the_subject_is_not_deleted(
    orm: Registry,
) -> None:
    """No delete, no refusal: the finding is about a row that goes away."""

    class Kept(Model, table="kept_two"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('seq')")
        person_id: Mapped[int] = column(Int64, references=Person.id)
        note: Mapped[str] = column(Text)

    registry = Registry(FakeDatabase(), [Person, Kept], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id")
    privacy.classify(Kept, subject="person_id", personal={"note": Erase.REDACT})
    assert privacy.plan("4711").surviving_references == ()


def test_a_plan_with_no_surviving_reference_says_so(privacy: Privacy) -> None:
    text = privacy.render(privacy.plan("4711"))
    assert (
        "none: nothing this erasure keeps still points at what it deletes." in text
    )


def test_an_exempt_table_is_retained_and_printed_with_its_reason(
    privacy: Privacy,
) -> None:
    plan = privacy.plan("4711")
    assert [item.table for item in plan.retained] == ["ledger"]
    assert plan.retained[0].reason == "retained seven years under tax law"
    assert "ledger" not in {item.table for item in plan.tables}


# -- absence is stated --------------------------------------------------------


def test_a_clean_plan_says_so_rather_than_falling_silent(privacy: Privacy) -> None:
    text = privacy.render(privacy.plan("4711"))
    assert "none: every classified table is reachable from the subject." in text
    assert "none: the reachable tables can be ordered children-first." in text
    assert "Backups are out of scope" in text


def test_a_blocked_plan_says_so_and_does_not_offer_the_erase_command(
    privacy: Privacy,
) -> None:
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    text = privacy.render(privacy.plan("4711"))
    assert "BLOCKED." in text
    assert "privacy.erase(" not in text, (
        "a blocked plan must not hand out a runnable call"
    )


# -- the plan that runs is the plan that was printed ---------------------------


def test_prepare_refuses_a_digest_from_a_plan_that_has_moved(
    privacy: Privacy,
) -> None:
    stale = privacy.plan("4711").digest
    privacy.classify(Comment, personal={"body": Erase.REDACT}, delete=True)
    with pytest.raises(PlanMoved, match="the plan changed since it was printed"):
        privacy.prepare("4711", digest=stale)


def test_prepare_refuses_a_blocked_plan_even_with_a_matching_digest(
    privacy: Privacy,
) -> None:
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    plan = privacy.plan("4711")
    with pytest.raises(ErasureBlocked, match="unreachable classified table"):
        privacy.prepare("4711", digest=plan.digest)


def test_the_digest_moves_when_a_finding_appears_without_an_action_changing(
    privacy: Privacy,
) -> None:
    """A newly unreachable table is a different plan even if no action moved."""
    before = privacy.plan("4711")
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    after = privacy.plan("4711")
    assert [item.table for item in before.tables] == [item.table for item in after.tables]
    assert before.digest != after.digest


# -- what the passes actually say ---------------------------------------------


def test_a_directly_classified_table_matches_without_a_subquery(
    privacy: Privacy,
) -> None:
    prepared = privacy.prepare("4711")
    receipt = next(step for action, step in prepared.steps if action.table == "receipts")
    text = receipt.work.where.text
    assert text.startswith('("person_id" = ?)')
    assert "SELECT" not in text


def test_a_depth_two_table_matches_through_nested_subqueries(
    privacy: Privacy,
) -> None:
    prepared = privacy.prepare("4711")
    comment = next(step for action, step in prepared.steps if action.table == "comments")
    text = comment.work.where.text
    assert text.count("SELECT") == 2
    assert '"photos"' in text
    assert '"people"' in text
    assert comment.work.where.values == ("4711",)


def test_an_anonymising_pass_excludes_rows_it_has_already_emptied(
    privacy: Privacy,
) -> None:
    """The guard is what makes a retried chunk a no-op rather than a rewrite."""
    prepared = privacy.prepare("4711")
    comment = next(step for action, step in prepared.steps if action.table == "comments")
    assert comment.work.set_ == {"body": "'[erased]'"}
    assert '"body" IS DISTINCT FROM' in comment.work.where.text


def test_a_cascaded_table_gets_no_pass_and_is_still_listed(privacy: Privacy) -> None:
    """The database removes these rows; hiding that would not be a plan."""
    prepared = privacy.prepare("4711")
    photo_action, photo_pass = next(
        step for step in prepared.steps if step[0].table == "photos"
    )
    assert photo_action.disposal == Disposal.CASCADE.value
    assert photo_pass is None, (
        "a cascaded table must get no pass: issuing one would be a delete the "
        "plan did not promise, on top of the delete the database already does"
    )
    assert "photos" not in {walk.table for walk in prepared.passes}
    assert "ON DELETE CASCADE" in privacy.render(prepared.plan)


def test_every_pass_is_a_chunked_pass_rather_than_one_transaction(
    privacy: Privacy,
) -> None:
    from wreath.passes import ChunkedPass

    prepared = privacy.prepare("4711")
    assert prepared.passes
    assert all(isinstance(walk, ChunkedPass) for walk in prepared.passes)


# -- pseudonymisation is not erasure ------------------------------------------


def test_a_pseudonymised_column_is_marked_not_erasure_in_the_plan(
    orm: Registry,
) -> None:
    from wreath.passes import Declared

    privacy = Privacy(orm)
    privacy.subject(Person, key="id")
    privacy.classify(
        Comment,
        personal={"body": Pseudonymise(Declared("support needs ticket continuity"))},
    )
    plan = privacy.plan("4711")
    comment = next(item for item in plan.tables if item.table == "comments")
    action = comment.columns[0]
    assert action.erase == "pseudonymise"
    assert action.irreversible is False
    assert "support needs ticket continuity" in action.pseudonym_reason
    text = privacy.render(plan)
    assert "NOT erasure" in text
    assert "still distinguishable" in text


def test_a_pseudonymised_column_is_never_written_by_the_generated_pass(
    orm: Registry,
) -> None:
    """The module refuses to invent the transform it refuses to call erasure."""
    from wreath.passes import Declared

    privacy = Privacy(orm)
    privacy.subject(Person, key="id")
    privacy.classify(Comment, personal={"body": Pseudonymise(Declared("kept joinable on purpose"))})
    prepared = privacy.prepare("4711")
    assert not prepared.passes


# -- the planner needs a subject ----------------------------------------------


def test_planning_without_a_subject_model_refuses_rather_than_guessing(
    orm: Registry,
) -> None:
    privacy = Privacy(orm)
    privacy.classify(Comment, personal={"body": Erase.REDACT})
    with pytest.raises(ValueError, match="no subject model declared"):
        privacy.plan("4711")


def test_a_second_subject_model_is_refused(orm: Registry) -> None:
    privacy = Privacy(orm)
    privacy.subject(Person, key="id")
    with pytest.raises(PrivacyDeclarationError, match="a registry has one subject model"):
        privacy.subject(Photo, key="id")


# -- the access request renders the same traversal ----------------------------


def test_an_access_plan_lists_tables_withheld_rows_and_unreachable_ones(
    privacy: Privacy,
) -> None:
    """An exemption from erasure is not an exemption from access.

    The subject is entitled to know what is held about them even where it
    cannot be deleted, so the exempt table appears under `withheld` with the
    reason rather than vanishing from the response.
    """
    privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    plan = privacy.access("4711")
    text = privacy.render(plan)
    assert [item.table for item in plan.withheld] == ["ledger"]
    assert "retained seven years under tax law" in text
    assert "public.comments" in text
    assert "Orphaned" in text


def test_an_access_plan_states_each_absence_rather_than_falling_silent(
    privacy: Privacy,
) -> None:
    text = privacy.render(privacy.access("4711"))
    assert "Unreachable (0)" in text
    assert text.count("  none.") == 1, "the empty section says none rather than nothing"


def test_an_access_plan_with_nothing_in_it_says_none_for_tables_too(
    orm: Registry,
) -> None:
    privacy = Privacy(orm)
    privacy.subject(Person, key="id")
    privacy.classify(Person, exempt="the account row is kept for fraud review")
    text = privacy.render(privacy.access("4711"))
    assert text.count("  none.") == 2


def test_a_cascaded_table_with_no_columns_is_not_listed_for_export(
    privacy: Privacy,
) -> None:
    """A row the parent's cascade removes carries nothing extra to export."""
    plan = privacy.access("4711")
    text = privacy.render(plan)
    people = [line for line in text.splitlines() if "public.people" in line]
    assert people, "the subject's own row is exportable"


# -- the graph itself ---------------------------------------------------------


def test_the_walk_records_the_shortest_path_when_two_reach_the_same_table(
    orm: Registry,
) -> None:
    """Breadth-first, so a reviewer reads the fewest joins that explain a table.

    A depth-first walk would record whichever path it happened to take, and the
    execution predicate nests one subquery per edge -- so the path length is a
    cost as well as a sentence.
    """

    class Album(Model, table="albums"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        owner_id: Mapped[int] = column(Int64, references=Person.id)

    class Print(Model, table="prints"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        album_id: Mapped[int] = column(Int64, references=Album.id)
        person_id: Mapped[int] = column(Int64, references=Person.id)
        note: Mapped[str] = column(Text)

    registry = Registry(FakeDatabase(), [Person, Album, Print], validate_schema="off")
    privacy = Privacy(registry)
    privacy.subject(Person, key="id")
    privacy.classify(Print, personal={"note": Erase.REDACT})
    plan = privacy.plan("4711")
    prints = next(item for item in plan.tables if item.table == "prints")
    assert prints.reach.depth == 1, "the direct edge beats the one through albums"


def test_a_reached_table_with_nothing_classified_is_traversed_not_acted_on(
    orm: Registry,
) -> None:
    """`Photo` is the bridge to `Comment` even when nothing about it is personal."""
    privacy = Privacy(orm)
    privacy.subject(Person, key="id")
    privacy.classify(Comment, personal={"body": Erase.REDACT})
    plan = privacy.plan("4711")
    tables = {item.table for item in plan.tables}
    assert "comments" in tables
    assert "photos" not in tables


# -- erase drives every pass, in order ----------------------------------------


def test_erase_runs_each_pass_in_the_planned_order(privacy: Privacy) -> None:
    """The order is a correctness property, so it is asserted at execution too."""
    import asyncio

    ran: list[str] = []

    class RecordingPass:
        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self, database: object) -> None:
            ran.append(self.name)

    prepared = privacy.prepare("4711")
    replaced = tuple(
        (action, None if walk is None else RecordingPass(action.table))
        for action, walk in prepared.steps
    )
    object.__setattr__(prepared, "steps", replaced)

    async def drive() -> None:
        for _action, walk in prepared.steps:
            if walk is not None:
                await walk.run(object())

    asyncio.run(drive())
    assert ran == [
        action.table
        for action, walk in prepared.steps
        if walk is not None
    ]
    assert ran.index("comments") < ran.index("people")
