"""Transport-neutral contracts reuse binding's compiled validation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from wreath.binding import ValidationError
from wreath.contracts import Contract, compile_contract


@dataclass(frozen=True, slots=True)
class Order:
    id: uuid.UUID
    quantity: int


@dataclass(frozen=True, slots=True)
class Message:
    text: str


def test_contract_validates_and_uses_the_native_json_shape() -> None:
    contract = Contract(Order, name="orders.created", version=2)
    identifier = uuid.uuid4()
    encoded = contract.encode_json({"id": str(identifier), "quantity": 3})
    assert encoded == f'{{"id":"{identifier}","quantity":3}}'.encode()
    assert contract.decode_json(encoded) == Order(identifier, 3)
    assert contract.name == "orders.created"
    assert contract.version == 2


def test_contract_reports_the_same_field_errors_as_request_binding() -> None:
    contract = compile_contract(Order)
    with pytest.raises(ValidationError) as caught:
        contract.decode_json(b'{"id":"not-a-uuid","extra":1}')
    assert {tuple(error["loc"][-1:]) for error in caught.value.errors} == {
        ("id",),
        ("quantity",),
        ("extra",),
    }


def test_contract_decodes_text_and_limits_its_utf8_wire_size() -> None:
    payload = '{"text":"é"}'
    assert Contract(Message).decode_json(payload) == Message("é")
    with pytest.raises(ValueError, match="max_bytes"):
        Contract(Message, max_bytes=len(payload)).decode_json(payload)


def test_contract_refuses_bad_declarations_and_oversized_wire_data() -> None:
    with pytest.raises(TypeError, match="dataclass"):
        Contract(dict)
    with pytest.raises(ValueError, match="max_bytes"):
        Contract(Order, max_bytes=8).decode_json(b"{}" * 5)
