"""GraphQL: a mounted schema, its types and its resolvers."""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

GRAPHQL: dict[str, tuple[str, str, str, str]] = {
    # `wreath.graphql` shipped after this catalog was first written; leaving the
    # old "no equivalent" verdict in place told porters to keep a dependency
    # they can now delete, which is the specific way a porting tool goes stale.
    "graphql.mount": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        "Wreath ships GraphQL: GraphQL(registry, models=[...]) mounted with .router(). The difference is where the schema comes from -- wreath builds it from the ORM models you name, instead of from types you declare.",
    ),
    # A strawberry type that mirrors a model is a *deletion* — wreath derives the
    # object type from the ORM registry. But "mirrors a model" has to be proved,
    # not assumed, and two things break it. A type that lists a subset of the
    # columns is a deliberately narrowed surface, and the derived type exposes
    # every column of the model, so deleting the class WIDENS the public schema.
    # And strawberry camel-cases field names by default while wreath emits the
    # column name verbatim (`_graphql/schema.py` uses `column.python_name`), so a
    # snake_case field is a wire rename every client would see.
    "graphql.type": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        "Wreath builds the GraphQL type from the ORM model, so this class usually just goes away; name the model in GraphQL(models=[...]) instead. It was not deleted for you because deleting it here would change the schema -- the note in brackets says how.",
    ),
    "graphql.type_dataclass": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        "This plain output type becomes a native @dataclass(kw_only=True) and is registered with GraphQL(dataclasses=[...]). The porter removes the Strawberry decorator; add the class to that explicit schema allowlist where the endpoint is assembled.",
    ),
    "graphql.type_mirror": (
        "graphql",
        "other",
        TRANSLATED,
        "This class lists exactly the columns of the model of the same name, so it can be deleted -- name the model in GraphQL(models=[...]) instead and wreath builds the same type, with the same field names on the wire.",
    ),
    "graphql.resolver": (
        "graphql",
        "other",
        NEEDS_REVIEW,
        'A computed field becomes api.field("Type", "name", returns=...). One difference to plan for: your resolver is called once for the whole level with every parent object, not once per object, so it returns a list.',
    ),
}
