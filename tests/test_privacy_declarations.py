"""What `classify` refuses, and the one place a classification changes behaviour.

Two halves. The refusals are the module's central claim made enforceable --
erasure is irreversible, so a disposition that leaves the subject identifiable
is rejected by name rather than accepted and reported as erasure. The
observability half is the leverage argument: declaring a column personal has to
change something without a second configuration file, or the declaration is
just documentation with a parser.
"""

from __future__ import annotations

import pytest

from wreath._flight_schema import CaptureDisposition
from wreath._logsite import declare, declare_personal
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text, Timestamp
from wreath.passes import Declared
from wreath.privacy import Erase, Privacy, PrivacyDeclarationError, Pseudonymise


class FakeDatabase:
    name = "main"


class Account(Model, table="accounts"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    email: Mapped[str] = column(Text)
    nickname: Mapped[str | None] = column(Text, nullable=True)
    balance: Mapped[int] = column(Int64)
    closed_at: Mapped[object] = column(Timestamp, nullable=True, index=True)
    opened_at: Mapped[object] = column(Timestamp, nullable=True)


@pytest.fixture
def orm() -> Registry:
    return Registry(FakeDatabase(), [Account], validate_schema="off")


@pytest.fixture
def privacy(orm: Registry) -> Privacy:
    return Privacy(orm)


# -- erasure is irreversible --------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["hash", "hashed", "sha256", "mask", "tokenize", "anonymise", "anonymize"],
)
def test_a_disposition_that_leaves_the_subject_identifiable_is_refused(
    privacy: Privacy, spelling: str
) -> None:
    """The central refusal, one case per way somebody spells it.

    Each message has to say *why* it is not erasure and what to write instead;
    a refusal that only says "no" gets worked around rather than fixed.
    """
    with pytest.raises(PrivacyDeclarationError) as caught:
        privacy.classify(Account, personal={"email": spelling})
    message = str(caught.value)
    assert "is not erasure" in message
    assert "Erase.NULL or Erase.REDACT" in message
    assert "Pseudonymise(Declared(" in message


def test_the_refusal_names_the_reason_a_hash_is_not_erasure(privacy: Privacy) -> None:
    """Not just the field name: every refusal message contains the field name.

    A test asserting only that would pass whichever branch fired, including the
    unknown-disposition fallthrough, so it would prove nothing about *which*
    refusal ran.
    """
    with pytest.raises(PrivacyDeclarationError, match="reversible by lookup"):
        privacy.classify(Account, personal={"email": "hash"})


def test_pseudonymise_is_accepted_only_with_a_written_reason(
    privacy: Privacy,
) -> None:
    with pytest.raises(PrivacyDeclarationError, match="still personal data"):
        privacy.classify(Account, personal={"email": Pseudonymise(reason=None)})


def test_pseudonymise_with_a_reason_is_accepted_and_kept(privacy: Privacy) -> None:
    privacy.classify(
        Account,
        personal={"email": Pseudonymise(Declared("billing reconciliation needs it"))},
    )


def test_declared_refuses_an_empty_reason_before_privacy_sees_it() -> None:
    """`wreath.passes.Declared` is reused rather than reimplemented.

    Two spellings of "a written reason, refused when empty" would drift, and
    the one that drifted would be the one nobody was looking at.
    """
    from wreath.passes import PassDeclarationError

    with pytest.raises(PassDeclarationError, match="needs a reason"):
        Declared("   ")


def test_an_unknown_disposition_is_refused_with_the_vocabulary(
    privacy: Privacy,
) -> None:
    with pytest.raises(PrivacyDeclarationError, match="unknown disposition"):
        privacy.classify(Account, personal={"email": "obliterate"})


# -- refusals that would otherwise fail at 3am --------------------------------


def test_null_on_a_not_null_column_is_refused_at_declaration(
    privacy: Privacy,
) -> None:
    with pytest.raises(PrivacyDeclarationError, match="is NOT NULL"):
        privacy.classify(Account, personal={"email": Erase.NULL})


def test_null_on_the_primary_key_is_refused_with_the_alternative(
    privacy: Privacy,
) -> None:
    with pytest.raises(PrivacyDeclarationError, match="is the primary key"):
        privacy.classify(Account, personal={"id": Erase.NULL})


def test_redact_on_a_non_textual_column_is_refused(privacy: Privacy) -> None:
    """A fixed marker has to be a value the column's type accepts.

    Writing `0` into an integer would look like data rather than an absence,
    which is the opposite of what redaction is for.
    """
    with pytest.raises(PrivacyDeclarationError, match="Erase.REDACT writes a text"):
        privacy.classify(Account, personal={"balance": Erase.REDACT})


def test_a_column_the_model_does_not_declare_is_refused_by_name(
    privacy: Privacy,
) -> None:
    with pytest.raises(PrivacyDeclarationError, match="is not a column on this model"):
        privacy.classify(Account, personal={"emial": Erase.REDACT})


def test_an_exemption_needs_a_reason(privacy: Privacy) -> None:
    with pytest.raises(PrivacyDeclarationError, match="needs a written reason"):
        privacy.classify(Account, exempt="")


def test_an_exemption_and_a_delete_are_refused_together(privacy: Privacy) -> None:
    """Two opposite instructions where one would silently win."""
    with pytest.raises(PrivacyDeclarationError, match="Declare one"):
        privacy.classify(
            Account,
            personal={"nickname": Erase.NULL},
            delete=True,
            exempt="kept for tax",
        )


# -- retention ----------------------------------------------------------------


def test_a_retention_window_over_an_unindexed_column_is_refused(
    privacy: Privacy,
) -> None:
    """A sweep runs on a schedule forever; an unindexed walk is a slow incident."""
    with pytest.raises(PrivacyDeclarationError, match="is not indexed"):
        privacy.retain(Account, after=86400, on="opened_at")


def test_a_non_positive_retention_window_is_refused(privacy: Privacy) -> None:
    with pytest.raises(PrivacyDeclarationError, match="positive number of"):
        privacy.retain(Account, after=0, on="closed_at")


def test_a_retention_pass_walks_by_the_clock_column_first(privacy: Privacy) -> None:
    """A clock-derived frontier has to be compared against a timestamp.

    Walking by the primary key with a `now() - interval` predicate would move
    the finish line while the walk ran; the frontier is the window.
    """
    privacy.retain(Account, after=90 * 86400, on="closed_at", reason="support policy")
    ((policy, walk),) = privacy.retention_passes()
    assert policy.after == 90 * 86400
    assert walk.units.keys[0].name == "closed_at"
    assert walk.units.keys[-1].name == "id"
    assert walk.frontier.after == 90 * 86400
    assert walk.frontier.recurring is True


def test_retention_states_the_absence_of_a_window_rather_than_omitting_it(
    privacy: Privacy,
) -> None:
    privacy.classify(Account, personal={"nickname": Erase.NULL})
    lines = privacy.retention()
    assert any("UNBOUNDED" in line and "Account" in line for line in lines)


def test_the_erasure_record_window_is_reported_unbounded_when_unset(
    orm: Registry,
) -> None:
    """No default, because the honest one is "as long as your oldest backup"."""
    assert any("erasure records: UNBOUNDED" in line for line in Privacy(orm).retention())
    stated = Privacy(orm, erasure_record_retain=30 * 86400).retention()
    assert any("erasure records: deleted 30d" in line for line in stated)


# -- the observability seam ---------------------------------------------------


def test_classifying_a_column_hashes_a_log_field_of_that_name(
    privacy: Privacy,
) -> None:
    """The leverage claim, made checkable.

    An `int` field defaults to RAW because "an integer is not a secret-bearing
    shape" -- which stops being true when the integer is *which person* the
    record is about.
    """
    declare_personal(frozenset())
    assert declare("owner_id", int).disposition is CaptureDisposition.RAW
    privacy.classify(Account, subject="id", personal={"nickname": Erase.NULL})
    assert declare("nickname", int).disposition is CaptureDisposition.HASHED
    assert declare("id", int).disposition is CaptureDisposition.HASHED
    declare_personal(frozenset())


def test_an_explicit_disposition_still_wins_over_the_classification(
    privacy: Privacy,
) -> None:
    """The classification changes the default, not a decision somebody made."""
    privacy.classify(Account, personal={"nickname": Erase.NULL})
    field = declare("nickname", int, CaptureDisposition.RAW)
    assert field.disposition is CaptureDisposition.RAW
    declare_personal(frozenset())


def test_the_personal_name_set_replaces_rather_than_accumulates() -> None:
    """A registry that dropped a classification must stop claiming the name."""
    declare_personal(frozenset({"alpha"}))
    declare_personal(frozenset({"beta"}))
    assert declare("alpha", int).disposition is CaptureDisposition.RAW
    assert declare("beta", int).disposition is CaptureDisposition.HASHED
    declare_personal(frozenset())


# -- retention refusals and rendering -----------------------------------------


def test_a_retention_line_carries_the_reason_when_one_was_given(
    privacy: Privacy,
) -> None:
    privacy.retain(Account, after=7 * 86400, on="closed_at", reason="support policy")
    (line,) = [item for item in privacy.retention() if item.startswith("Account:")]
    assert line == "Account: rows deleted 7d after closed_at -- support policy"


def test_a_retention_line_without_a_reason_does_not_invent_a_separator(
    privacy: Privacy,
) -> None:
    privacy.retain(Account, after=3600, on="closed_at")
    (line,) = [item for item in privacy.retention() if item.startswith("Account:")]
    assert line == "Account: rows deleted 1h after closed_at"


def test_a_sub_hour_window_is_rendered_in_seconds(privacy: Privacy) -> None:
    privacy.retain(Account, after=90, on="closed_at")
    (line,) = [item for item in privacy.retention() if item.startswith("Account:")]
    assert "90s" in line


def test_a_model_with_no_primary_key_cannot_exist_to_be_walked(
    privacy: Privacy,
) -> None:
    """Where the invariant actually lives, so nobody re-adds a second guard.

    A keyset walk needs a unique ordering, and `wreath.privacy` used to check
    for one -- unreachable code, because `wreath.orm.Model` refuses a mapped
    model without a primary key at class creation. The check is gone; this
    records why, and goes red if the ORM ever stops refusing.
    """
    from wreath.orm.errors import DeclarationError

    with pytest.raises(DeclarationError, match="declares no primary-key column"):

        class Keyless(Model, table="keyless"):
            seen_at: Mapped[object] = column(Timestamp, nullable=True, index=True)


# -- the erasure surface refuses without a registry ---------------------------


def test_planning_without_any_registry_says_which_argument_is_missing() -> None:
    privacy = Privacy()
    privacy.subject(Account, key="id")
    with pytest.raises(ValueError, match="no ORM registry"):
        privacy.plan("4711")


# -- the checks on a model the registry cannot see ----------------------------


class NotAModel:
    """A plain class: no column map, no types, nothing to check against.

    Every column check answers "I cannot tell" for one of these rather than
    raising, deliberately: the planner refuses it later, where the message can
    say which registry it is missing from. These tests hold the *shape* of the
    checks -- each has to survive a model it knows nothing about.
    """

    __name__ = "NotAModel"


def test_a_classification_on_an_unmapped_class_is_carried_rather_than_refused(
    privacy: Privacy,
) -> None:
    privacy.classify(
        NotAModel, subject="whatever", personal={"anything": Erase.REDACT}
    )
    item = privacy._registry.classifications[NotAModel]
    assert (item.subject, item.personal) == ("whatever", {"anything": "redact"})


def test_a_null_disposition_on_an_unmapped_class_is_carried_too(
    privacy: Privacy,
) -> None:
    """`_require_nullable` has nothing to read, and must not invent an answer."""
    privacy.classify(NotAModel, personal={"anything": Erase.NULL})
    assert privacy._registry.classifications[NotAModel].personal == {
        "anything": "null"
    }


def test_a_disposition_that_is_not_a_string_at_all_is_refused(
    privacy: Privacy,
) -> None:
    """The vocabulary is small; anything outside it is named rather than coerced."""
    with pytest.raises(PrivacyDeclarationError, match="is not a disposition"):
        privacy.classify(Account, personal={"email": 42})


def test_null_on_a_non_textual_column_is_accepted_where_redact_is_not(
    privacy: Privacy,
) -> None:
    """The textual check belongs to `REDACT` alone.

    `REDACT` writes a text marker and needs a column that accepts one; `NULL`
    writes nothing and works on any nullable column. Applying the textual rule
    to both would refuse a perfectly good erasure of an integer.
    """
    with pytest.raises(PrivacyDeclarationError, match="Erase.REDACT writes a text"):
        privacy.classify(Account, personal={"closed_at": Erase.REDACT})
    privacy.classify(Account, personal={"closed_at": Erase.NULL})
    assert privacy._registry.classifications[Account].personal == {"closed_at": "null"}


def test_declaring_the_same_subject_model_twice_is_not_a_second_subject(
    privacy: Privacy,
) -> None:
    """Restating a declaration is not a conflict; a *different* model is."""
    privacy.subject(Account, key="id")
    privacy.subject(Account, key="id", delete=True)
    assert privacy._registry.subject_delete is True


# -- the fragments the erasure interpolates -----------------------------------


def test_a_column_name_that_is_not_an_identifier_is_refused_before_it_is_spliced(
    orm: Registry,
) -> None:
    """A column name reaches the statement by interpolation, so it is checked.

    Every one of them came from a model declaration rather than from a request,
    which is why this is a belt on top of braces -- but the statement text is
    assembled here, so the check belongs here too.
    """
    from wreath._privacy.execute import predicate_for
    from wreath._privacy.graph import build_graph
    from wreath._privacy.model import Reach, TableAction
    from wreath._privacy.registry import PrivacyRegistry

    action = TableAction(
        model="Account",
        schema="public",
        table="accounts",
        disposal="anonymise",
        reach=Reach(()),
        match_column="id; DROP TABLE accounts",
    )
    with pytest.raises(ValueError, match="is not a plain SQL identifier"):
        predicate_for(action, build_graph(orm), PrivacyRegistry(), "4711")


def test_a_null_column_is_set_to_null_and_guarded_on_being_set(
    orm: Registry,
) -> None:
    """The two dispositions write different SQL and different re-run guards."""
    privacy = Privacy(orm)
    privacy.subject(Account, key="id")
    privacy.classify(
        Account, personal={"nickname": Erase.NULL, "email": Erase.REDACT}
    )
    walk = privacy.prepare("4711").passes[0]
    assert walk.work.set_ == {"nickname": "NULL", "email": "'[erased]'"}
    assert '"nickname" IS NOT NULL' in walk.work.where.text
    assert "\"email\" IS DISTINCT FROM '[erased]'" in walk.work.where.text


def test_preparing_against_a_registry_the_plan_did_not_come_from_is_refused(
    orm: Registry,
) -> None:
    """The plan and the registry must describe the same application.

    A plan built against one registry and executed against another would either
    walk the wrong table or silently skip one, and neither failure announces
    itself.
    """
    from wreath._privacy.execute import ErasureBlocked
    from wreath._privacy.execute import prepare as _prepare

    privacy = Privacy(orm)
    privacy.subject(Account, key="id")
    privacy.classify(Account, personal={"email": Erase.REDACT})
    plan = privacy.plan("4711")

    class Other(Model, table="other_accounts"):
        id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
        email: Mapped[str] = column(Text)

    other = Registry(FakeDatabase(), [Other], validate_schema="off")
    with pytest.raises(ErasureBlocked, match="the plan and the registry disagree"):
        _prepare(privacy._registry, other, "4711", plan=plan)


# -- what the retention report says, and what it deliberately does not --------


def test_a_model_with_a_window_is_not_also_listed_as_unbounded(
    orm: Registry,
) -> None:
    """One line per model. Two would read as a policy that contradicts itself."""
    privacy = Privacy(orm)
    privacy.classify(Account, personal={"email": Erase.REDACT})
    privacy.retain(Account, after=90 * 86400, on="closed_at", reason="policy")
    lines = [line for line in privacy.retention() if line.startswith("Account:")]
    assert lines == ["Account: rows deleted 90d after closed_at -- policy"]


def test_a_classified_model_with_no_personal_columns_is_not_a_finding(
    orm: Registry,
) -> None:
    """`UNBOUNDED` means "personal data with no window", not "no window"."""
    privacy = Privacy(orm)
    privacy.classify(Account, subject="id")
    assert not [line for line in privacy.retention() if line.startswith("Account:")]


def test_personal_data_with_no_window_is_named_rather_than_omitted(
    orm: Registry,
) -> None:
    """The finding a reader is looking for; a silence reads as "no problem"."""
    privacy = Privacy(orm)
    privacy.classify(Account, personal={"email": Erase.REDACT})
    assert (
        "Account: UNBOUNDED -- holds personal data (email) with no declared "
        "retention window"
    ) in privacy.retention()
