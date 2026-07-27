"""A herd-management service in the shape of a mature FastAPI/ormar codebase.

Structurally modelled on a real production tree — the proportions are what
matter, not the domain. Where the original had hundreds of `.objects.` calls
across a dozen query verbs, a cachetools layer in front of reference lookups,
`arrow` for every timestamp, a strawberry GraphQL surface over the same models,
and an Alembic history mixing ordinary DDL with row-rewriting data migrations,
so does this — at one tenth the size and with every identifier re-themed.

Never imported or executed: the codemod reads it as source.
"""
