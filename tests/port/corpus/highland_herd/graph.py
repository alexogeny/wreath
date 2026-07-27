"""A strawberry GraphQL surface over the same ormar models.

The original exposes most of its read API twice — once as REST, once as
GraphQL — with ``strawberry.auto`` doing the field declarations. Wreath derives
those from the ORM registry, so the type classes largely disappear and only the
computed fields are real work.
"""
import strawberry
from strawberry.fastapi import GraphQLRouter

from .models import Llama as LlamaModel


@strawberry.type
class Paddock:
    id: strawberry.auto
    name: strawberry.auto
    hectares: strawberry.auto


@strawberry.type
class Llama:
    id: strawberry.auto
    name: strawberry.auto
    grade: strawberry.auto
    fleece_kg: strawberry.auto

    @strawberry.field
    async def trek_count(self) -> int:
        return await LlamaModel.objects.filter(id=self.id).count()


@strawberry.input
class LlamaFilter:
    paddock_id: strawberry.auto
    min_grade: strawberry.auto


@strawberry.type
class Query:
    @strawberry.field
    async def llamas(self, where: LlamaFilter) -> list[Llama]:
        return await LlamaModel.objects.filter(grade__gte=where.min_grade).all()


schema = strawberry.Schema(query=Query)
graph_router = GraphQLRouter(schema)
