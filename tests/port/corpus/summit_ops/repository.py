"""DynamoDB-as-primary persistence — no ORM; a hand-rolled repository.

Idiom: Abstract/Dynamo/InMemory repository trio, a pydantic->UpdateExpression builder,
and a float->Decimal quantizer (boto3 rejects float).
"""
from __future__ import annotations

import abc
from decimal import ROUND_HALF_UP, Decimal

import boto3
from pydantic import BaseModel

from summit_dynamo import table_for  # in-house helper (anonymized)


def quantize_floats(value):
    if isinstance(value, float):
        return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if isinstance(value, dict):
        return {k: quantize_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [quantize_floats(v) for v in value]
    return value


def build_update_expression(model: BaseModel) -> dict:
    """pydantic model -> Dynamo UpdateExpression + expression-attribute maps."""
    data = quantize_floats(model.model_dump(exclude_none=True))
    names, values, sets = {}, {}, []
    for i, (field, val) in enumerate(data.items()):
        names[f"#f{i}"] = field
        values[f":v{i}"] = val
        sets.append(f"#f{i} = :v{i}")
    return {
        "UpdateExpression": "SET " + ", ".join(sets),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


class ExpeditionRepository(abc.ABC):
    @abc.abstractmethod
    async def list(self, *, limit: int): ...

    @abc.abstractmethod
    async def put(self, model): ...


class DynamoExpeditionRepository(ExpeditionRepository):
    def __init__(self, table_name: str) -> None:
        self._table = boto3.resource("dynamodb").Table(table_name)

    async def list(self, *, limit: int):
        return self._table.scan(Limit=limit).get("Items", [])

    async def put(self, model):
        self._table.update_item(Key={"id": model.id}, **build_update_expression(model))


class InMemoryExpeditionRepository(ExpeditionRepository):
    def __init__(self) -> None:
        self._store: dict = {}

    async def list(self, *, limit: int):
        return list(self._store.values())[:limit]

    async def put(self, model):
        self._store[model.id] = model


def dynamo_repository() -> ExpeditionRepository:
    return DynamoExpeditionRepository(table_for("expeditions"))
