from __future__ import annotations

import datetime

import pytest

from wreath.temporal import Instant, parse

MOMENT = "2026-07-26T09:30:00+00:00"


def test_an_instant_satisfies_the_timestamptz_column() -> None:
    from wreath.orm.types import TimestampTz

    moment = parse(MOMENT)
    assert TimestampTz.coerce(moment) is moment


def test_an_instant_is_refused_by_the_naive_timestamp_column() -> None:
    from wreath.orm.types import Timestamp

    with pytest.raises(TypeError, match="naive"):
        Timestamp.coerce(parse(MOMENT))


def test_a_plain_aware_datetime_still_works() -> None:
    from wreath.orm.types import TimestampTz

    value = datetime.datetime(2026, 7, 26, 9, 30, tzinfo=datetime.UTC)
    assert TimestampTz.coerce(value) is value


def test_a_response_containing_an_instant_serialises() -> None:
    from wreath._json import dumps

    assert dumps({"started_at": parse(MOMENT)}) == (b'{"started_at":"2026-07-26T09:30:00+00:00"}')


def test_a_plain_datetime_serialises_too() -> None:
    from wreath._json import dumps

    value = datetime.datetime(2026, 7, 26, 9, 30, tzinfo=datetime.UTC)
    assert dumps({"at": value}) == b'{"at":"2026-07-26T09:30:00+00:00"}'


def test_dates_and_times_and_durations_all_render() -> None:
    from wreath._json import dumps

    encoded = dumps(
        {
            "day": datetime.date(2026, 7, 26),
            "clock": datetime.time(9, 30),
            "every": datetime.timedelta(hours=2),
        }
    )
    assert b'"day":"2026-07-26"' in encoded
    assert b'"clock":"09:30:00"' in encoded
    assert b'"every":"PT2H"' in encoded


def test_temporal_values_nested_anywhere_are_found() -> None:
    from wreath._json import dumps

    encoded = dumps({"treks": [{"legs": [{"at": parse(MOMENT)}]}]})
    assert b"2026-07-26T09:30:00+00:00" in encoded


def test_something_genuinely_unserialisable_still_raises() -> None:
    from wreath._json import dumps

    with pytest.raises(TypeError, match="not JSON serializable"):
        dumps({"nope": object()})


def test_a_payload_with_no_temporal_values_is_untouched() -> None:
    from wreath._json import dumps

    assert dumps({"a": [1, 2, {"b": "c"}], "d": None}) == (b'{"a":[1,2,{"b":"c"}],"d":null}')


def test_the_existing_strictness_survives() -> None:
    from wreath._json import dumps

    with pytest.raises(ValueError):
        dumps(float("nan"))
    with pytest.raises(TypeError):
        dumps({1: "int-key"})


def test_a_path_or_query_parameter_parses_into_an_instant() -> None:
    from wreath.binding import _convert_scalar

    value = _convert_scalar(Instant, "2026-07-26T09:30:00Z", ("query", "since"))
    assert isinstance(value, Instant)
    assert value.tzinfo is not None


def test_a_naive_parameter_is_a_validation_error_not_an_assumption() -> None:
    from wreath.binding import ValidationError, _convert_scalar

    with pytest.raises(ValidationError) as caught:
        _convert_scalar(Instant, "2026-07-26T09:30:00", ("query", "since"))
    assert caught.value.errors[0]["type"] == "instant"


def test_an_unparseable_parameter_is_a_validation_error() -> None:
    from wreath.binding import ValidationError, _convert_scalar

    with pytest.raises(ValidationError):
        _convert_scalar(Instant, "yesterday-ish", ("query", "since"))


def test_an_optional_instant_parameter_works() -> None:
    from wreath.binding import _convert_scalar

    value = _convert_scalar(Instant | None, "2026-07-26T09:30:00Z", ("query", "since"))
    assert isinstance(value, Instant)


def test_openapi_describes_an_instant_as_a_date_time_string() -> None:
    from wreath.openapi import _openapi_schema
    from wreath.typegen.model import TypeRef

    assert _openapi_schema(TypeRef("string", "date-time")) == {
        "type": "string",
        "format": "date-time",
    }


def test_a_plain_string_still_has_no_format() -> None:
    from wreath.openapi import _openapi_schema
    from wreath.typegen.model import STRING

    assert _openapi_schema(STRING) == {"type": "string"}


def test_the_ir_maps_an_instant_to_a_formatted_string() -> None:
    from wreath.typegen.inspect import _SCALARS

    assert _SCALARS[Instant].kind == "string"
    assert _SCALARS[Instant].name == "date-time"


def test_a_datetime_annotation_maps_the_same_way() -> None:
    from wreath.typegen.inspect import _SCALARS

    assert _SCALARS[datetime.datetime].name == "date-time"
    assert _SCALARS[datetime.date].name == "date"


def test_typescript_renders_a_date_time_as_a_readable_alias() -> None:
    from wreath.typegen.typescript_renderer import ts_type

    assert ts_type(("string", "date-time", (), ())) == "IsoDateTime"
    assert ts_type(("string", None, (), ())) == "string"


def test_graphql_exposes_real_temporal_scalars() -> None:
    from wreath._graphql.schema import _SCALARS

    assert _SCALARS["timestamptz"] == "DateTime"
    assert _SCALARS["timestamp"] == "DateTime"
    assert _SCALARS["date"] == "Date"


def test_the_other_graphql_scalars_are_unchanged() -> None:
    from wreath._graphql.schema import _SCALARS

    assert _SCALARS["uuid"] == "ID"
    assert _SCALARS["jsonb"] == "JSON"
    assert _SCALARS["text"] == "String"


def test_a_custom_scalar_is_declared_in_the_sdl() -> None:
    from wreath._graphql.schema import ObjectType, Schema, SchemaField

    trek = ObjectType(name="Trek", spec=None)
    trek.fields["started_at"] = SchemaField(
        name="started_at", type_name="DateTime", non_null=True, is_list=False
    )
    sdl = Schema(registry=None, types={"Trek": trek}, roots={}).sdl()

    assert "scalar DateTime" in sdl
    assert sdl.index("scalar DateTime") < sdl.index("type Trek")


def test_a_schema_with_no_custom_scalars_declares_none() -> None:
    from wreath._graphql.schema import ObjectType, Schema, SchemaField

    llama = ObjectType(name="Llama", spec=None)
    llama.fields["name"] = SchemaField(
        name="name", type_name="String", non_null=True, is_list=False
    )
    assert "scalar" not in Schema(registry=None, types={"Llama": llama}, roots={}).sdl()


def test_the_graphql_client_maps_the_new_scalars_to_real_types() -> None:
    from wreath._graphql.typegen import _SCALAR_REFS

    assert _SCALAR_REFS["DateTime"].kind == "string"
    assert _SCALAR_REFS["DateTime"].name == "date-time"
    assert _SCALAR_REFS["Date"].name == "date"


def test_the_request_reports_the_callers_preferred_locale() -> None:
    from wreath.request import Request

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"accept-language", b"fr-CA,fr;q=0.9,en;q=0.8")],
    }
    assert Request(scope, receive).locale == "fr-CA"


def test_the_request_keeps_the_large_locale_header_path() -> None:
    from wreath.request import Request

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    locale = "x-" + "a" * 300
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"accept-language", locale.encode())],
    }
    assert Request(scope, receive).locale == locale


def test_a_caller_with_no_preference_gets_the_default() -> None:
    from wreath.request import Request

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
    }
    assert Request(scope, receive).locale == "en"


def test_a_malformed_accept_language_does_not_raise() -> None:
    from wreath.request import Request

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"accept-language", b";;;q=")],
    }
    assert Request(scope, receive).locale == "en"
