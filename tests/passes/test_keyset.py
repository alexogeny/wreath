"""The keyset half: the SQL shape it emits, and the four refusals that keep it sound.

Every refusal here is a data-loss or a never-terminates bug that the declaration
can see. The tests assert the message names the fix, not just that something
raised -- an error that does not say what to do next is a stack trace with better
manners.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from wreath._passes import keyset
from wreath.passes import Key, PassDeclarationError


def _keys(*items: Key) -> tuple[Key, ...]:
    return items


# --- the shape of the comparison ---------------------------------------------


def test_a_composite_key_is_one_row_comparison_not_expanded_ors():
    keys = _keys(
        Key("herd_id", "int8", indexed=True),
        Key("id", "int8", unique=True),
    )
    sql = keyset.row_comparison(keys, ">", ["$1", "$2"])

    assert sql == "(herd_id, id) > ($1, $2)"
    # The hand-expanded form means the same thing and is planned as a bitmap-or
    # over two scans plus a sort. Emitting it would quietly cost the single
    # index descent the whole complexity argument rests on.
    assert " OR " not in sql


def test_a_single_column_key_needs_no_row_constructor():
    keys = _keys(Key("id", "int8", unique=True, indexed=True))

    assert keyset.row_comparison(keys, ">", ["$1"]) == "id > $1"


def test_a_comparison_needs_one_bind_per_key_column():
    keys = _keys(Key("a", "int8", indexed=True), Key("b", "int8", unique=True))

    with pytest.raises(PassDeclarationError, match="one bind per key column"):
        keyset.row_comparison(keys, ">", ["$1"])


def test_the_operators_follow_the_walk_direction():
    up = _keys(Key("id", "int8", unique=True, indexed=True))
    down = _keys(Key("id", "int8", unique=True, indexed=True, descending=True))

    assert keyset.after_operator(up) == ">"
    assert keyset.upto_operator(up) == "<="
    # Walking high to low, "past the cursor" is a smaller value and the frontier
    # is a floor rather than a ceiling.
    assert keyset.after_operator(down) == "<"
    assert keyset.upto_operator(down) == ">="


def test_the_order_clause_can_be_read_from_either_end():
    keys = _keys(Key("expires", "timestamptz", indexed=True), Key("key", "text", unique=True))

    assert keyset.order_clause(keys) == "expires, key"
    # Reversing is how the last key still inside a range is found in one index
    # descent rather than by counting rows.
    assert keyset.order_clause(keys, reverse=True) == "expires DESC, key DESC"


# --- the refusals -------------------------------------------------------------


def test_a_mixed_direction_key_is_refused_because_a_row_comparison_has_no_such_form():
    keys = _keys(
        Key("herd_id", "int8", indexed=True),
        Key("id", "int8", unique=True, descending=True),
    )

    with pytest.raises(PassDeclarationError) as error:
        keyset.refuse_unsound_key(keys, table="treks")

    assert "mixes directions" in str(error.value)
    assert "order every key column the same way" in str(error.value)


def test_a_key_with_no_index_on_its_leading_column_is_refused():
    keys = _keys(Key("grade", "text"), Key("id", "int8", unique=True))

    with pytest.raises(PassDeclarationError) as error:
        keyset.refuse_unsound_key(keys, table="treks")

    message = " ".join(str(error.value).split())
    assert "which has no index" in message
    # Without an index this is N/c sorts of N rows -- worse than the OFFSET
    # paging keyset walking exists to avoid, so it must not degrade silently.
    assert "worse than the OFFSET paging" in message


def test_a_key_that_cannot_be_proven_unique_is_refused_and_names_the_fix():
    keys = _keys(Key("expires", "timestamptz", indexed=True))

    with pytest.raises(PassDeclarationError) as error:
        keyset.refuse_unsound_key(keys, table="replays")

    message = str(error.value).replace("\n", " ")
    assert "silent data loss" in message
    assert "Append the primary key as a tiebreaker" in message


def test_a_repeated_key_column_is_refused():
    keys = _keys(Key("id", "int8", indexed=True, unique=True), Key("id", "int8"))

    with pytest.raises(PassDeclarationError, match="repeats a column"):
        keyset.refuse_unsound_key(keys, table="treks")


def test_a_sound_key_passes_every_refusal():
    keys = _keys(
        Key("expires", "timestamptz", indexed=True),
        Key("key", "text", unique=True),
    )

    keyset.refuse_unsound_key(keys, table="replays")  # no raise


def test_a_fixed_ceiling_over_an_unordered_key_is_refused():
    keys = _keys(Key("id", "uuid", indexed=True, unique=True))

    with pytest.raises(PassDeclarationError) as error:
        keyset.refuse_unmonotone_key(keys, table="treks", reason=None)

    message = str(error.value).replace("\n", " ")
    assert "assigned in increasing order" in message
    assert "monotone=" in message


def test_a_written_reason_is_the_way_past_the_monotone_refusal():
    keys = _keys(Key("id", "uuid", indexed=True, unique=True))

    # ULIDs and UUIDv7 really are monotone and nothing in a column declaration
    # can see it, so the escape is a sentence a reviewer reads rather than a flag.
    keyset.refuse_unmonotone_key(keys, table="treks", reason="UUIDv7 from the application")


def test_a_monotone_column_needs_no_reason():
    keys = _keys(Key("id", "int8", indexed=True, unique=True, monotone=True))

    keyset.refuse_unmonotone_key(keys, table="treks", reason=None)


def test_a_clock_frontier_over_a_non_timestamp_key_is_refused():
    keys = _keys(Key("id", "int8", indexed=True, unique=True))

    with pytest.raises(PassDeclarationError) as error:
        keyset.refuse_unclocked_key(keys, table="treks")

    assert "must be a timestamp" in str(error.value).replace("\n", " ")


@pytest.mark.parametrize(
    "sql_type", ["timestamptz", "timestamp", "timestamp with time zone"]
)
def test_every_timestamp_spelling_satisfies_a_clock_frontier(sql_type):
    keyset.refuse_unclocked_key(_keys(Key("expires", sql_type, indexed=True)), table="t")


# --- cursors ------------------------------------------------------------------


def test_a_cursor_round_trips_through_the_ledgers_json():
    keys = _keys(
        Key("expires", "timestamptz", indexed=True),
        Key("id", "uuid", unique=True),
    )
    stamp = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)
    identifier = uuid.uuid4()

    encoded = keyset.encode_cursor(keys, (stamp, identifier))
    # The ledger column is jsonb, so what goes in has to survive a round trip
    # through JSON and come back as the type the driver will bind.
    assert encoded == [stamp.isoformat(), str(identifier)]
    assert keyset.decode_cursor(keys, encoded) == (stamp, identifier)


def test_an_integer_cursor_comes_back_as_an_integer():
    keys = _keys(Key("id", "int8", indexed=True, unique=True))

    assert keyset.decode_cursor(keys, [42]) == (42,)


def test_a_cursor_of_the_wrong_width_is_refused_rather_than_guessed_at():
    keys = _keys(Key("a", "int8", indexed=True), Key("b", "int8", unique=True))

    with pytest.raises(PassDeclarationError, match="does not match a 2-column key"):
        keyset.decode_cursor(keys, [1])


def test_no_cursor_decodes_to_no_cursor():
    keys = _keys(Key("id", "int8", indexed=True, unique=True))

    assert keyset.decode_cursor(keys, None) is None


def test_a_key_column_must_be_a_plain_identifier():
    with pytest.raises(PassDeclarationError, match="plain SQL identifier"):
        Key("id; DROP TABLE treks", "int8")


def test_a_key_type_must_be_a_plain_sql_type_name():
    with pytest.raises(PassDeclarationError) as error:
        Key("id", "int8); DROP TABLE treks --")

    assert "not a plain SQL type name" in " ".join(str(error.value).split())
