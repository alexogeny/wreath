"""Strawberry GraphQL surface mounted at /graphql (no wreath equivalent yet)."""
import strawberry


@strawberry.type
class LlamaNode:
    id: str
    name: str
    temperament: str


@strawberry.type
class Query:
    @strawberry.field
    async def llama(self, id: str) -> LlamaNode:
        from .models import Llama

        row = await Llama.objects.get(id=id)
        return LlamaNode(
            id=str(row.id), name=row.name, temperament=row.temperament or "unknown"
        )


schema = strawberry.Schema(query=Query)
