from __future__ import annotations

import pytest

from wreath._native import _core
from wreath._scim import filters as scim_filters
from wreath.binding import _compile_plan
from wreath.signatures import SignatureError
from wreath.xml import Element, Limits


def test_xml_native_parse_materializes_only_the_public_element() -> None:
    limits = Limits()
    result = _core.xml_parse(
        b"<root><child value='1'>text</child></root>",
        Element,
        limits.max_bytes,
        limits.max_depth,
        limits.max_elements,
        limits.max_attributes,
        limits.max_attribute_bytes,
    )

    assert isinstance(result, Element)
    assert result.children[0].tag == "child"


def test_scim_filter_compiles_to_an_operation_owned_native_plan() -> None:
    node = scim_filters.parse('userName sw "mar"')
    plan = scim_filters._compile(node)

    assert type(plan).__name__ == "PyCapsule"
    assert _core.scim_matches(plan, {"userName": "Mara"}, scim_filters._TYPES) is True


def test_signature_headers_compile_to_an_operation_owned_native_plan() -> None:
    plan = _core.signature_compile_pair(
        'sig1=("@method" "content-type");created=1;keyid="k"',
        "sig1=:AQID:",
        SignatureError,
        65_536,
        64,
    )

    params, raw_signature, covered = _core.signature_plan_facts(plan)

    assert params == {"created": 1, "keyid": "k"}
    assert raw_signature == b"\x01\x02\x03"
    assert covered == ("@method", "content-type")


def test_orm_hydration_constants_compile_to_a_reusable_native_plan() -> None:
    class Model:
        pass

    class Spec:
        model_type = Model

    class RowPlan:
        key = ((0, None),)
        cells = ((0, None),)

    plan = _core.orm_compile_hydrate_plan(Spec(), RowPlan(), ())

    assert type(plan).__name__ == "PyCapsule"


def test_validation_tape_compiles_to_an_operation_owned_native_plan() -> None:
    source = _compile_plan(list[int], frozenset())
    plan = _core.compile_validation_plan(source)

    result, errors = _core.run_validation(plan, [1, 2, 3], ("body",))

    assert type(plan).__name__ == "PyCapsule"
    assert result == [1, 2, 3]
    assert errors == []
    with pytest.raises(TypeError, match="requires a compiled native plan"):
        _core.run_validation(source, [1, 2, 3], ("body",))
