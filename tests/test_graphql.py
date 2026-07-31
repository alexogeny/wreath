"""GraphQL: parsing limits, schema derivation, and execution over the ORM."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from orm.conftest import (
    FakeDatabase,
    Post,
    User,
    post_row,
    user_row,
)

from wreath._auth.requirements import PolicyRequirement
from wreath._graphql.parser import (
    GraphQLSyntaxError,
    Limits,
    parse,
)
from wreath._graphql.schema import policy_resource
from wreath.auth import Identity
from wreath.authorization import CedarAuthorizer, CedarPolicies, EntityUid
from wreath.graphql import GraphQL, ResolverError
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.request import Request

# --- parser: syntax ----------------------------------------------------------


def test_parses_fields_aliases_arguments_and_variables() -> None:
    document = parse("""
        query Get($id: Int!, $n: Int = 5) {
          user(id: $id) { id email }
          other: user(id: 2) { id }
        }
    """)
    operation = document.operation()
    assert operation.name == "Get"
    assert [v.name for v in operation.variables] == ["id", "n"]
    assert operation.variables[0].non_null is True
    assert operation.variables[1].default == 5
    fields = operation.selection_set.selections
    assert fields[0].name == "user" and fields[0].key == "user"
    assert fields[1].name == "user" and fields[1].key == "other"


def test_parses_shorthand_documents() -> None:
    document = parse("{ users { id } }")
    assert document.operation().operation == "query"


@pytest.mark.parametrize(
    "source",
    [
        "",                        # empty
        "{",                       # unterminated
        "{ }",                     # empty selection set
        "query { user(id: ) }",    # missing value
        'query { a(x: "unterminated) }',
        "subscription { x }",      # unsupported operation
        "fragment F { id }",       # missing `on`
        "fragment F on T { id } fragment F on T { id }",   # duplicate
    ],
)
def test_malformed_documents_are_refused(source: str) -> None:
    with pytest.raises(GraphQLSyntaxError):
        parse(source)


def test_comments_and_commas_are_ignored() -> None:
    document = parse("""
        # leading comment
        { users { id, email }  # trailing
        }
    """)
    assert document.complexity == 3


def test_block_and_escaped_strings() -> None:
    document = parse('{ a(x: "line\\nbreak", y: """\n  block\n  text\n""") { id } }')
    field = document.operation().selection_set.selections[0]
    arguments = {a.name: a.value for a in field.arguments}
    assert arguments["x"] == "line\nbreak"
    assert arguments["y"] == "block\ntext"


def test_directives_are_accepted_and_ignored() -> None:
    document = parse("{ users @include(if: true) { id @skip(if: false) } }")
    assert document.operation().selection_set.selections[0].name == "users"


# --- parser: safety limits ---------------------------------------------------


def test_depth_is_bounded() -> None:
    source = "{" + "a{" * 30 + "b" + "}" * 30 + "}"
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse(source, Limits(max_depth=10))
    assert caught.value.code == "depth"


def test_complexity_is_bounded() -> None:
    source = "{ " + " ".join(f"f{i}" for i in range(200)) + " }"
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse(source, Limits(max_complexity=50))
    assert caught.value.code == "complexity"


def test_alias_amplification_is_bounded() -> None:
    """A tiny document that asks for the same expensive field 100 times."""
    source = "{ " + " ".join(f"a{i}: user" for i in range(100)) + " }"
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse(source, Limits(max_aliases=20))
    assert caught.value.code == "aliases"


def test_the_step_budget_is_a_backstop() -> None:
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse("{ " + " ".join(f"f{i}" for i in range(5000)) + " }",
              Limits(max_complexity=100_000, max_steps=500,
                     max_document_bytes=1_000_000))
    assert caught.value.code == "steps"


def test_fragment_cycles_are_refused() -> None:
    """No selection-set depth limit bounds this: the cycle is in the fragment
    graph, and the document itself is three lines long."""
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse("""
            { user { ...A } }
            fragment A on User { ...B }
            fragment B on User { ...A }
        """)
    assert caught.value.code == "fragment_cycle"
    assert "A -> B -> A" in str(caught.value) or "B -> A -> B" in str(caught.value)


def test_self_referential_fragment_is_refused() -> None:
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse("{ user { ...A } } fragment A on User { ...A }")
    assert caught.value.code == "fragment_cycle"


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        Limits(max_depth=0)


def test_measured_depth_and_complexity_are_reported() -> None:
    document = parse("{ user { posts { title } } }")
    assert document.depth == 3
    assert document.complexity == 3


# --- schema ------------------------------------------------------------------


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def registry(database: FakeDatabase) -> Registry:
    return Registry(database, [User, Post])


def test_the_schema_is_derived_from_the_orm(registry: Registry) -> None:
    api = GraphQL(registry, models=[User, Post])
    sdl = api.sdl()
    assert "type User {" in sdl
    assert "email: String!" in sdl          # NOT NULL column -> non-null
    # Nullable column -> nullable, and a timestamp is a real scalar rather than
    # falling through to String. The absent `!` is what this line is checking.
    assert "created_at: DateTime\n" in sdl
    assert "scalar DateTime" in sdl         # ... and the SDL declares it
    assert "posts: [Post!]!" in sdl         # to-many relationship
    assert "author: User" in sdl            # to-one relationship
    assert "users(limit: Int, offset: Int)" in sdl


def test_narrowing_models_narrows_what_is_reachable(registry: Registry) -> None:
    """Exposure is opt-in, and a relationship cannot smuggle a model back in."""
    api = GraphQL(registry, models=[User])
    sdl = api.sdl()
    assert "type Post" not in sdl
    assert "posts:" not in sdl              # the relationship is dropped too


# --- retrieval columns -------------------------------------------------------
#
# Generated CRUD withholds a `Vector` and a `TsVector` from what it serializes,
# because a retrieval column indexes content rather than carrying it. GraphQL is
# derived from the same `ModelSpec` and filtered by the same functions, so it
# withholds them on the same rule -- otherwise fixing one surface would have
# left `{ doc { embedding search } }` answering what `GET /doc` no longer does.


def _doc_model():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text, TsVector, Vector

    class Doc(Model, table="graphql_docs"):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text)
        embedding: Mapped[list] = column(Vector(3), nullable=True)
        search: Mapped[bytes] = column(
            TsVector("english", sources=("title",)), index="gin"
        )

    return Doc


def _doc_row() -> list[Any]:
    return [1, "llamas", [1.0, 0.0, 0.0], b"'llama':1"]


def test_retrieval_columns_are_not_in_the_schema(database: FakeDatabase) -> None:
    """A page of twenty `Vector(1536)` rows is thirty thousand floats.

    And on this surface the client, not the server, chooses the page size.
    """
    Doc = _doc_model()
    api = GraphQL(Registry(database, [Doc]), models=[Doc])

    fields = api.schema.types["Doc"].fields
    assert set(fields) == {"id", "title"}
    assert "embedding" not in api.sdl() and "search" not in api.sdl()


@pytest.mark.asyncio
async def test_a_retrieval_column_cannot_be_queried(database: FakeDatabase) -> None:
    Doc = _doc_model()
    registry = Registry(database, [Doc])
    database.connection.script("graphql_docs", [_doc_row()])
    api = GraphQL(registry, models=[Doc])

    body = await api.run(
        "{ doc(id: 1) { embedding search } }", Session(registry, "read")
    )

    assert body["data"] is None
    assert "no field 'embedding'" in body["errors"][0]["message"]


@pytest.mark.asyncio
async def test_expose_puts_a_retrieval_column_back(database: FakeDatabase) -> None:
    """The same explicit, auditable keyword `crud_router(expose=...)` asks for.

    It widens only what may leave: nothing here widens what may be *written*,
    because no mutation is generated -- a GraphQL write is a resolver somebody
    wrote, and what it accepts is that resolver's business.
    """
    Doc = _doc_model()
    registry = Registry(database, [Doc])
    database.connection.script("graphql_docs", [_doc_row()])
    api = GraphQL(registry, models=[Doc], expose=("Doc.embedding",))

    body = await api.run(
        "{ doc(id: 1) { title embedding } }", Session(registry, "read")
    )

    assert body["data"]["doc"] == {"title": "llamas", "embedding": [1.0, 0.0, 0.0]}
    assert "search" not in api.schema.types["Doc"].fields   # not exposed, not there


# --- execution ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_flat_query_projects_selected_columns_only(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1, "a@b.c", "Ada")])
    api = GraphQL(registry, models=[User, Post])

    body = await api.run("{ user(id: 1) { id email } }", Session(registry, "read"))

    assert body == {"data": {"user": {"id": 1, "email": "a@b.c"}}}


@pytest.mark.asyncio
async def test_aliases_land_under_their_response_key(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1, "a@b.c")])
    api = GraphQL(registry, models=[User, Post])

    body = await api.run("{ me: user(id: 1) { address: email } }", Session(registry, "read"))

    assert body == {"data": {"me": {"address": "a@b.c"}}}


@pytest.mark.asyncio
async def test_a_list_root_is_always_bounded(
    registry: Registry, database: FakeDatabase
) -> None:
    """An unpaginated root field would be a client-requested table scan."""
    database.connection.script("users", [user_row(i) for i in range(1, 4)])
    api = GraphQL(registry, models=[User, Post], max_page_size=25)

    await api.run("{ users { id } }", Session(registry, "read"))

    sql, args = database.connection.calls[0]
    assert "LIMIT" in sql.upper()
    assert 25 in args


@pytest.mark.asyncio
async def test_a_client_cannot_raise_the_page_ceiling(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    api = GraphQL(registry, models=[User, Post], max_page_size=10)

    await api.run("{ users(limit: 10000) { id } }", Session(registry, "read"))

    _sql, args = database.connection.calls[0]
    assert 10 in args and 10000 not in args


@pytest.mark.asyncio
async def test_a_relationship_is_batched_not_resolved_per_parent(
    registry: Registry, database: FakeDatabase
) -> None:
    """The N+1 property: three users and their posts is two statements."""
    database.connection.script("users", [user_row(1), user_row(2), user_row(3)])
    database.connection.script("posts", [post_row(10, 1), post_row(11, 2)])
    api = GraphQL(registry, models=[User, Post])

    body = await api.run("{ users { id posts { title } } }", Session(registry, "read"))

    assert len(database.connection.calls) == 2      # not 1 + 3
    assert len(body["data"]["users"]) == 3


@pytest.mark.asyncio
async def test_unknown_fields_and_roots_are_reported_as_errors(
    registry: Registry, database: FakeDatabase
) -> None:
    api = GraphQL(registry, models=[User, Post])

    unknown_root = await api.run("{ nope { id } }", Session(registry, "read"))
    assert "unknown root field" in unknown_root["errors"][0]["message"]

    database.connection.script("users", [user_row(1)])
    unknown_field = await api.run(
        "{ user(id: 1) { nope } }", Session(registry, "read")
    )
    assert "no field" in unknown_field["errors"][0]["message"]


@pytest.mark.asyncio
async def test_a_limit_breach_is_returned_as_a_coded_error(
    registry: Registry
) -> None:
    api = GraphQL(registry, models=[User, Post], limits=Limits(max_depth=2))
    body = await api.run("{ user { posts { author { id } } } }", Session(registry, "read"))
    assert body["errors"][0]["extensions"]["code"] == "depth"


@pytest.mark.asyncio
async def test_variables_are_substituted_and_required_ones_enforced(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(7)])
    api = GraphQL(registry, models=[User, Post])

    ok = await api.run(
        "query Q($id: Int!) { user(id: $id) { id } }",
        Session(registry, "read"),
        variables={"id": 7},
    )
    assert ok["data"]["user"]["id"] == 7

    missing = await api.run(
        "query Q($id: Int!) { user(id: $id) { id } }", Session(registry, "read")
    )
    assert "required" in missing["errors"][0]["message"]


@pytest.mark.asyncio
async def test_fragments_expand_against_their_type_condition(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1, "a@b.c")])
    api = GraphQL(registry, models=[User, Post])

    body = await api.run(
        "{ user(id: 1) { ...F } } fragment F on User { id email }",
        Session(registry, "read"),
    )
    assert body["data"]["user"] == {"id": 1, "email": "a@b.c"}


@pytest.mark.asyncio
async def test_the_parse_cache_reuses_a_repeated_document(
    registry: Registry, database: FakeDatabase
) -> None:
    api = GraphQL(registry, models=[User, Post])
    first = api.parse("{ users { id } }")
    second = api.parse("{ users { id } }")
    assert first is second


# --- authorization -----------------------------------------------------------
#
# Two kinds of test live here, and the distinction is the point (ADR 0020, ADR
# 0024 item 4). The *integration* is proved against the shipped
# `CedarAuthorizer` and a real `CedarPolicies` policy set; the doubles below
# only cover properties that are GraphQL's own -- caching, `on_denied`, a
# resolver naming its own resource -- and they refuse anything the real provider
# would refuse, so they cannot go on being more capable than it.
#
# They were more capable, for the whole life of this file: every authorizer here
# was duck-typed as `authorize(request, resource: str)`, while
# `AuthorizationProvider.authorize` takes a `PolicyRequirement`. The executor
# passed a `str`, so the one authorizer wreath ships raised `AttributeError:
# 'str' object has no attribute 'resource'` on the first field it was asked
# about, and no test could see it.


def resource(policy: str) -> EntityUid:
    """The Cedar reference the executor hands an authorizer for `policy`.

    Derived from the production mapping rather than restated, so a change to it
    moves these expectations with it instead of silently disagreeing.
    """
    return policy_resource(policy)


class _RequirementOnly:
    """Base for the doubles: refuse what `AuthorizationProvider` refuses.

    A provider is asked with a `PolicyRequirement` and reads `.action` and
    `.resource` off it. A double that also accepted a bare string would admit
    the exact call the shipped authorizer rejects, which is how this file came
    to certify a broken integration.
    """

    def _requirement(self, requirement: Any) -> PolicyRequirement:
        if not isinstance(requirement, PolicyRequirement):
            raise TypeError(
                "an AuthorizationProvider is asked with a PolicyRequirement, "
                f"not {type(requirement).__name__!r}"
            )
        return requirement


class DenyField(_RequirementOnly):
    """An authorizer that refuses one field's resource."""

    def __init__(self, denied: str) -> None:
        self.denied = resource(denied)
        # `PolicyRequirement.resource` is declared `object | Callable`, because a
        # route may hand `@authorize` a per-request resolver; what arrives here
        # is always the already-built reference.
        self.seen: list[Any] = []
        self.actions: list[str] = []

    async def authorize(self, request: Any, requirement: Any) -> Any:
        requirement = self._requirement(requirement)
        self.seen.append(requirement.resource)
        self.actions.append(requirement.action)

        class Decision:
            allowed = requirement.resource != self.denied
            reason = "field is not readable"

        return Decision()


def make_request(identity: Identity | None = None) -> Request:
    """A request an authorizer can read a principal off."""

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "path": "/graphql", "headers": []}, receive
    )
    if identity is not None:
        request._set_identity(identity)
    return request


#: A policy set naming GraphQL resources directly. `Query::"user"` and
#: `User::"id"` are readable by a reader; `User.email` is named by no permit, and
#: `resource is Mutation` forbids every write in one clause -- which is only
#: expressible because the resource is an entity reference rather than a string.
CEDAR_SOURCE = """
permit(principal in Role::"reader", action == Action::"read", resource == Query::"user");
permit(principal in Role::"reader", action == Action::"read", resource == User::"id");
forbid(principal, action, resource is Mutation);
"""


@pytest.fixture
def cedar() -> CedarAuthorizer:
    """The authorizer wreath actually ships, over a real policy set."""
    return CedarAuthorizer(engine=CedarPolicies(CEDAR_SOURCE))


@pytest.mark.asyncio
async def test_a_field_permitted_by_a_real_cedar_policy_is_served(
    registry: Registry, database: FakeDatabase, cedar: CedarAuthorizer
) -> None:
    """The documented integration, against the shipped authorizer.

    This is the regression guard for `AttributeError: 'str' object has no
    attribute 'resource'`: before the executor built a `PolicyRequirement`, this
    query answered `{"data": None, "errors": [{"code": "RESOLVER_ERROR"}]}` and
    incremented `resolver_errors`, because the adapter blew up on the first
    field. Proved red before it was made green.
    """
    database.connection.script("users", [user_row(1, "a@b.c")])
    api = GraphQL(registry, models=[User, Post], authorizer=cedar)

    body = await api.run(
        "{ user(id: 1) { id } }",
        Session(registry, "read"),
        request=make_request(Identity("alice", roles=frozenset({"reader"}))),
    )

    assert body == {"data": {"user": {"id": 1}}}
    assert api.resolver_errors == 0


@pytest.mark.asyncio
async def test_a_field_no_cedar_policy_permits_is_denied_with_the_engines_reason(
    registry: Registry, database: FakeDatabase, cedar: CedarAuthorizer
) -> None:
    """The denial's shape: the engine's reason, and the path to the field.

    `User.email` is named by no permit, so Cedar's default-deny answers. The
    message is the engine's own `reason` -- not a wrapped one, and not the
    generic `"the resolver failed"` an exception out of the adapter produced.
    """
    database.connection.script("users", [user_row(1, "a@b.c")])
    api = GraphQL(registry, models=[User, Post], authorizer=cedar)

    body = await api.run(
        "{ user(id: 1) { id email } }",
        Session(registry, "read"),
        request=make_request(Identity("alice", roles=frozenset({"reader"}))),
    )

    assert body["data"] is None
    assert body["errors"] == [
        {"message": "no permit policy matched", "path": ["user", "email"]}
    ]
    assert api.resolver_errors == 0


@pytest.mark.asyncio
async def test_one_cedar_clause_denies_every_mutation(
    registry: Registry, database: FakeDatabase, cedar: CedarAuthorizer
) -> None:
    """`resource is Mutation` covers all writes, exactly as the guide claims.

    Only expressible because `Mutation.createUser` reaches the engine as
    `Mutation::"createUser"`. A bare string resource cannot be matched by type,
    so this clause could not have been written at all.
    """
    api = GraphQL(registry, models=[User, Post], authorizer=cedar)

    @api.mutation("createUser", returns="User")
    async def create_user(info: Any) -> Any:  # pragma: no cover - never reached
        raise AssertionError("the mutation must not run")

    body = await api.run(
        "mutation { createUser { id } }",
        Session(registry, "read"),
        request=make_request(Identity("alice", roles=frozenset({"reader"}))),
    )

    assert body["errors"] == [{"message": "explicit forbid", "path": ["createUser"]}]


@pytest.mark.asyncio
async def test_the_real_authorizer_denies_an_anonymous_caller(
    registry: Registry, database: FakeDatabase, cedar: CedarAuthorizer
) -> None:
    """`CedarAuthorizer` refuses without a principal, and GraphQL surfaces it.

    A GraphQL endpoint is one route: nothing has necessarily run an
    authentication backend by the time a field is authorized, so the provider's
    own anonymous refusal is the one that has to hold.
    """
    database.connection.script("users", [user_row(1, "a@b.c")])
    api = GraphQL(registry, models=[User, Post], authorizer=cedar)

    body = await api.run(
        "{ user(id: 1) { id } }", Session(registry, "read"), request=make_request()
    )

    assert body["errors"] == [{"message": "anonymous", "path": ["user"]}]


def test_a_policy_resource_is_a_cedar_entity_reference() -> None:
    """The one place the `Type.field` -> `Type::"field"` mapping is pinned.

    Everything else derives from `policy_resource`, so this is where a change to
    it has to be stated on purpose.
    """
    assert policy_resource("User.email") == EntityUid("User", "email")
    assert policy_resource("Mutation.createUser") == EntityUid("Mutation", "createUser")
    # A policy already written as a reference is used verbatim, both spellings.
    assert policy_resource('Billing::"read"') == EntityUid("Billing", "read")
    assert policy_resource("Billing::read") == EntityUid("Billing", "read")


def test_a_policy_no_engine_could_read_is_refused_at_declaration(
    registry: Registry
) -> None:
    """A bare name is a startup error, not a resolver failure on first use."""
    api = GraphQL(registry, models=[User, Post])

    with pytest.raises(ValueError, match="not a usable authorization resource"):

        @api.field("User", "balance", returns="Int", policy="balance")
        async def balance(users: Any, info: Any) -> Any:
            return []

    with pytest.raises(ValueError, match="not a usable authorization resource"):

        @api.query("search", returns="User", is_list=True, policy="search")
        async def search(info: Any) -> Any:
            return []


def test_an_unnamed_action_is_refused(registry: Registry) -> None:
    """`Action::""` matches no policy, so every field would deny with no cause."""
    with pytest.raises(ValueError, match="action is required"):
        GraphQL(registry, models=[User], action="")


@pytest.mark.asyncio
async def test_field_access_is_checked_against_the_authorizer(
    registry: Registry, database: FakeDatabase
) -> None:
    """The protocol boundary: one requirement per resource, action included."""
    database.connection.script("users", [user_row(1, "a@b.c")])
    authorizer = DenyField("User.email")
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    body = await api.run("{ user(id: 1) { id email } }", Session(registry, "read"))

    assert body["data"] is None
    assert "not readable" in body["errors"][0]["message"]
    assert resource("User.id") in authorizer.seen
    assert set(authorizer.actions) == {"read"}


@pytest.mark.asyncio
async def test_the_action_the_authorizer_is_asked_about_can_be_named(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    authorizer = DenyField("User.nothing")
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer, action="view")

    await api.run("{ user(id: 1) { id } }", Session(registry, "read"))
    assert set(authorizer.actions) == {"view"}


@pytest.mark.asyncio
async def test_an_allowed_field_passes_the_authorizer(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    authorizer = DenyField("User.nothing")
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    body = await api.run("{ user(id: 1) { id } }", Session(registry, "read"))
    assert body["data"]["user"]["id"] == 1


# --- introspection is off by default ----------------------------------------


@pytest.mark.asyncio
async def test_introspection_is_off_by_default(registry: Registry) -> None:
    """A schema dump is reconnaissance; it should not be on by accident."""
    router = GraphQL(registry, models=[User]).router()
    methods = {(m, r.path) for r in router.routes for m in r.methods}
    assert ("GET", "/graphql") not in methods
    assert ("POST", "/graphql") in methods


@pytest.mark.asyncio
async def test_introspection_can_be_enabled(registry: Registry) -> None:
    router = GraphQL(registry, models=[User], introspection=True).router()
    methods = {(m, r.path) for r in router.routes for m in r.methods}
    assert ("GET", "/graphql") in methods


# --- typegen integration -----------------------------------------------------


def test_graphql_and_rest_share_one_model_set(registry: Registry) -> None:
    """The whole point: one TypeScript `User`, not two.

    A type the REST inspector already emitted is reused rather than duplicated,
    so a client gets `useGetUser()` and the GraphQL root returning the same
    interface.
    """
    from wreath._graphql.typegen import merge_into
    from wreath.typegen.model import ApiModel, Field, Model, TypeRef

    rest = ApiModel(
        title="t",
        version="1",
        models=(Model(name="User", fields=(Field("id", TypeRef("integer"), True),)),),
    )
    api = GraphQL(registry, models=[User, Post])
    merged = merge_into(rest, api.schema)

    names = [model.name for model in merged.models]
    assert names.count("User") == 1          # not duplicated
    assert "Post" in names                   # the GraphQL-only type is added
    # The REST definition won, so nothing downstream sees a changed User.
    user = next(m for m in merged.models if m.name == "User")
    assert user.fields == rest.models[0].fields


def test_each_root_field_becomes_a_typed_operation(registry: Registry) -> None:
    from wreath._graphql.typegen import graphql_operations

    operations = {op.id: op for op in graphql_operations(GraphQL(registry).schema)}

    singular = operations["graphqlUser"]
    assert singular.response_body.kind == "reference"
    assert singular.response_body.name == "User"
    assert [p.wire_name for p in singular.parameters] == ["id"]

    plural = operations["graphqlUsers"]
    assert plural.response_body.kind == "array"
    assert plural.response_body.arguments[0].name == "User"
    assert {p.wire_name for p in plural.parameters} == {"limit", "offset"}


def test_relationship_fields_reference_their_target_type(registry: Registry) -> None:
    from wreath._graphql.typegen import graphql_models

    models = {model.name: model for model in graphql_models(GraphQL(registry).schema)}
    posts = next(f for f in models["User"].fields if f.wire_name == "posts")
    assert posts.type.kind == "array"
    assert posts.type.arguments[0].name == "Post"


def test_document_size_is_bounded_before_any_scanning() -> None:
    """The cheapest limit: parse cost scales with length, so cap the length.

    Rejected on `len()` alone, before a character is tokenized, so an oversized
    document costs nothing to refuse.
    """
    huge = "{ " + "a " * 100_000 + "}"
    with pytest.raises(GraphQLSyntaxError) as caught:
        parse(huge, Limits(max_document_bytes=4096))
    assert caught.value.code == "document_size"


def test_a_document_at_the_size_limit_is_accepted() -> None:
    source = "{ users { id } }"
    assert parse(source, Limits(max_document_bytes=len(source))) is not None


# --- custom and chained resolvers -------------------------------------------


@pytest.mark.asyncio
async def test_a_batched_resolver_sees_the_whole_level(
    registry: Registry, database: FakeDatabase
) -> None:
    """Batched by default: application code cannot reintroduce the N+1."""
    database.connection.script("users", [user_row(1), user_row(2), user_row(3)])
    api = GraphQL(registry, models=[User, Post])
    calls: list[int] = []

    @api.field("User", "shout", returns="String")
    async def shout(users, info):
        calls.append(len(users))
        return [u.email.upper() for u in users]

    body = await api.run("{ users { id shout } }", Session(registry, "read"))

    assert calls == [3]                       # one call for three users
    assert body["data"]["users"][0]["shout"] == "A@B.C"


@pytest.mark.asyncio
async def test_each_resolve_is_a_flight_phase_carrying_the_levels_width(
    registry: Registry, database: FakeDatabase
) -> None:
    """"Per-field latency in the Flight Recorder" -- against the real seam.

    The module docstring promises `RESOLVER` phases "without wiring an
    exporter", and nothing asked for one. The marker is the recorder's own
    `ContextVar`, so binding it is the whole integration; the dependency count
    is the level's width, which is what distinguishes a slow field from a wide
    one.
    """
    from wreath._flight_markers import PH_RESOLVER, phase_marker

    database.connection.script("users", [user_row(1), user_row(2), user_row(3)])
    api = GraphQL(registry, models=[User, Post])
    phases: list[tuple[int, int]] = []

    @api.field("User", "shout", returns="String")
    async def shout(users, info):
        return [u.email.upper() for u in users]

    token = phase_marker.set(
        lambda phase, dependency, coverage, duration: phases.append((phase, dependency))
    )
    try:
        await api.run("{ users { id shout } }", Session(registry, "read"))
    finally:
        phase_marker.reset(token)

    assert {phase for phase, _ in phases} == {PH_RESOLVER}
    # One phase for the column read (no level width) and one for the resolver,
    # which saw all three users.
    assert sorted(dependency for _, dependency in phases) == [0, 3]


@pytest.mark.asyncio
async def test_a_per_object_resolver_is_available_when_batching_makes_no_sense(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1), user_row(2)])
    api = GraphQL(registry, models=[User, Post])

    @api.field("User", "tag", returns="String", batch=False)
    async def tag(user, info):
        return f"u{user.id}"

    body = await api.run("{ users { tag } }", Session(registry, "read"))
    assert [row["tag"] for row in body["data"]["users"]] == ["u1", "u2"]


@pytest.mark.asyncio
async def test_a_resolver_can_be_synchronous(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    api = GraphQL(registry, models=[User, Post])

    @api.field("User", "half", returns="Int")
    def half(users, info):
        return [u.id // 2 for u in users]

    body = await api.run("{ users { half } }", Session(registry, "read"))
    assert body["data"]["users"][0]["half"] == 0


@pytest.mark.asyncio
async def test_requires_chains_a_relationship_before_the_computed_field(
    registry: Registry, database: FakeDatabase
) -> None:
    """The chaining story: declare the dependency, get it batched, in order."""
    database.connection.script("users", [user_row(1), user_row(2)])
    database.connection.script("posts", [post_row(10, 1), post_row(11, 1)])
    api = GraphQL(registry, models=[User, Post])

    @api.field("User", "postCount", returns="Int", requires=["posts"])
    async def post_count(users, info):
        return [len(u.posts) for u in users]

    body = await api.run("{ users { id postCount } }", Session(registry, "read"))

    assert [row["postCount"] for row in body["data"]["users"]] == [2, 0]
    # The dependency was loaded, but not emitted: asking for a computed field
    # must never silently widen the response.
    assert "posts" not in body["data"]["users"][0]
    assert len(database.connection.calls) == 2      # still batched


@pytest.mark.asyncio
async def test_a_resolver_chain_runs_in_dependency_order(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    api = GraphQL(registry, models=[User, Post])
    order: list[str] = []

    @api.field("User", "c", returns="Int", requires=["b"])
    async def c(users, info):
        order.append("c")
        return [3]

    @api.field("User", "b", returns="Int", requires=["a"])
    async def b(users, info):
        order.append("b")
        return [2]

    @api.field("User", "a", returns="Int")
    async def a(users, info):
        order.append("a")
        return [1]

    # Selected in reverse dependency order on purpose.
    body = await api.run("{ users { c b a } }", Session(registry, "read"))
    assert order == ["a", "b", "c"]
    assert body["data"]["users"][0] == {"c": 3, "b": 2, "a": 1}


def test_a_dependency_cycle_is_a_startup_error(registry: Registry) -> None:
    api = GraphQL(registry, models=[User])

    @api.field("User", "x", returns="Int", requires=["y"])
    async def x(users, info):
        return [1]

    @api.field("User", "y", returns="Int", requires=["x"])
    async def y(users, info):
        return [1]

    with pytest.raises(ResolverError, match="cycle"):
        api.validate()


def test_an_unknown_dependency_is_a_startup_error(registry: Registry) -> None:
    api = GraphQL(registry, models=[User])

    @api.field("User", "x", returns="Int", requires=["nope"])
    async def x(users, info):
        return [1]

    with pytest.raises(ResolverError, match="not a field of User"):
        api.validate()


def test_a_resolver_on_an_unexposed_type_is_refused(registry: Registry) -> None:
    api = GraphQL(registry, models=[User])
    with pytest.raises(ResolverError, match="not exposed|no type named"):

        @api.field("Post", "x", returns="Int")
        async def x(posts, info):
            return []


def test_duplicate_resolvers_are_refused(registry: Registry) -> None:
    api = GraphQL(registry, models=[User])

    @api.field("User", "x", returns="Int")
    async def x(users, info):
        return []

    with pytest.raises(ResolverError, match="already registered"):

        @api.field("User", "x", returns="Int")
        async def x2(users, info):
            return []


@pytest.mark.asyncio
async def test_a_batch_resolver_returning_the_wrong_count_is_an_error(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1), user_row(2)])
    api = GraphQL(registry, models=[User, Post])

    @api.field("User", "bad", returns="Int")
    async def bad(users, info):
        return [1]                     # two users, one value

    body = await api.run("{ users { bad } }", Session(registry, "read"))
    assert "returned 1 values for 2 objects" in body["errors"][0]["message"]


# --- custom roots and mutations ---------------------------------------------


@pytest.mark.asyncio
async def test_a_custom_root_field_needs_no_backing_table(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1, "ada@b.c")])
    api = GraphQL(registry, models=[User, Post])

    @api.query("search", returns="User", is_list=True)
    async def search(info):
        assert info.arguments["term"] == "ada"
        return await info.session.fetch(User.select())

    body = await api.run(
        '{ search(term: "ada") { email } }', Session(registry, "read")
    )
    assert body["data"]["search"][0]["email"] == "ada@b.c"


@pytest.mark.asyncio
async def test_mutations_run_and_are_namespaced_separately(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(9, "new@b.c")])
    api = GraphQL(registry, models=[User, Post])

    @api.mutation("createUser", returns="User")
    async def create_user(info):
        return (await info.session.fetch(User.select()))[0]

    body = await api.run(
        'mutation { createUser(email: "new@b.c") { id email } }',
        Session(registry, "read"),
    )
    assert body["data"]["createUser"] == {"id": 9, "email": "new@b.c"}

    # A mutation is not reachable as a query, and vice versa.
    as_query = await api.run("{ createUser { id } }", Session(registry, "read"))
    assert "unknown root field" in as_query["errors"][0]["message"]


@pytest.mark.asyncio
async def test_a_resolver_returning_objects_is_projected(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1, "a@b.c")])
    api = GraphQL(registry, models=[User, Post])

    @api.field("User", "twin", returns="User")
    async def twin(users, info):
        return list(users)          # each user is its own twin

    body = await api.run("{ users { id twin { email } } }", Session(registry, "read"))
    assert body["data"]["users"][0]["twin"] == {"email": "a@b.c"}


# --- tighter authorization ---------------------------------------------------


class RecordingAuthorizer(_RequirementOnly):
    """Counts what it was asked, and refuses a call the real provider would.

    `denied` names `Type.field` policies; they are converted through the
    production mapping so this double cannot drift from what the executor sends.
    """

    def __init__(self, denied: set[str] | None = None) -> None:
        self.denied = {resource(policy) for policy in denied or set()}
        self.asked: list[Any] = []

    async def authorize(self, request: Any, requirement: Any) -> Any:
        requirement = self._requirement(requirement)
        self.asked.append(requirement.resource)

        class Decision:
            allowed = requirement.resource not in self.denied
            reason = f"{requirement.resource} denied"

        return Decision()


@pytest.mark.asyncio
async def test_authorization_is_asked_once_per_resource_per_request(
    registry: Registry, database: FakeDatabase
) -> None:
    """The same field under three aliases is one decision, not three."""
    database.connection.script("users", [user_row(1)])
    authorizer = RecordingAuthorizer()
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    await api.run(
        "{ users { a: email b: email c: email } }", Session(registry, "read")
    )
    assert authorizer.asked.count(resource("User.email")) == 1


@pytest.mark.asyncio
async def test_root_fields_are_authorized_too(
    registry: Registry, database: FakeDatabase
) -> None:
    """`Query.users` is a resource, so a policy can deny a whole entry point."""
    authorizer = RecordingAuthorizer(denied={"Query.users"})
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    body = await api.run("{ users { id } }", Session(registry, "read"))
    assert 'Query::"users" denied' in body["errors"][0]["message"]


@pytest.mark.asyncio
async def test_on_denied_null_returns_null_instead_of_failing_the_query(
    registry: Registry, database: FakeDatabase
) -> None:
    """Partial results: the rest of the query still answers."""
    database.connection.script("users", [user_row(1, "a@b.c")])
    authorizer = RecordingAuthorizer(denied={"User.email"})
    api = GraphQL(
        registry, models=[User, Post], authorizer=authorizer, on_denied="null"
    )

    body = await api.run("{ users { id email } }", Session(registry, "read"))
    assert body["data"]["users"][0] == {"id": 1, "email": None}
    assert "errors" not in body


@pytest.mark.asyncio
async def test_a_resolver_can_declare_its_own_policy_resource(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    authorizer = RecordingAuthorizer(denied={'Billing::"read"'})
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    @api.field("User", "balance", returns="Int", policy='Billing::"read"')
    async def balance(users, info):
        return [0 for _ in users]

    body = await api.run("{ users { balance } }", Session(registry, "read"))
    assert 'Billing::"read" denied' in body["errors"][0]["message"]


def test_on_denied_must_be_a_known_mode(registry: Registry) -> None:
    with pytest.raises(ValueError, match="on_denied"):
        GraphQL(registry, models=[User], on_denied="explode")


def test_resolvers_cannot_be_added_after_the_endpoint_is_serving(
    registry: Registry
) -> None:
    api = GraphQL(registry, models=[User])
    api.router()                    # freezes

    with pytest.raises(ResolverError, match="before the endpoint serves"):

        @api.field("User", "late", returns="Int")
        async def late(users, info):
            return []


def test_the_sdl_shows_resolvers_custom_roots_and_mutations(
    registry: Registry
) -> None:
    api = GraphQL(registry, models=[User, Post])

    @api.field("User", "postCount", returns="Int", non_null=True, requires=["posts"])
    async def post_count(users, info):
        return []

    @api.query("search", returns="User", is_list=True)
    async def search(info):
        return []

    @api.mutation("createUser", returns="User")
    async def create_user(info):
        return None

    sdl = api.sdl()
    assert "postCount: Int!" in sdl
    assert "search: [User!]!" in sdl
    assert "type Mutation {" in sdl
    assert "createUser: User" in sdl


# --- the session the endpoint opens ------------------------------------------
#
# `wreath mutant` survived `expression.take-branch` on `"write" if mutating else
# "read"` in *both* directions, which means nothing asserted which pool a
# request opens. `_session`'s own docstring records why that matters: without
# `mutating`, every request -- mutations included -- opened a read session, "so
# a registered mutation ran against whatever the read pool points at, which on a
# replica is a failed write and on a single database is an invisible non-issue
# until the day a replica is added". The fix shipped; its regression test did
# not.


@pytest.mark.asyncio
async def test_a_mutation_opens_a_write_session_and_a_query_opens_a_read_one(
    registry: Registry,
) -> None:
    api = GraphQL(registry, models=[User])

    @api.mutation("touch", returns="Int")
    async def touch(info):
        return 1

    api.validate()
    request = Request({"type": "http", "method": "POST", "path": "/graphql",
                       "headers": []}, None)

    session, close = await api._session(
        request, None, mutating=api._is_mutation("mutation { touch }")
    )
    try:
        assert session.workload == "write"
    finally:
        await close()

    session, close = await api._session(
        request, None, mutating=api._is_mutation("query { users { id } }")
    )
    try:
        assert session.workload == "read"
    finally:
        await close()


@pytest.mark.asyncio
async def test_a_supplied_session_factory_is_used_and_closed_by_its_owner(
    registry: Registry,
) -> None:
    """Both shapes a factory may return, because the endpoint must not care.

    A factory that hands back a session directly owns closing it -- `close` is
    `None` -- and one that hands back an async context manager is exited by the
    endpoint. Getting this backwards leaks a connection per request.
    """
    api = GraphQL(registry, models=[User])
    api.validate()
    request = Request({"type": "http", "method": "POST", "path": "/graphql",
                       "headers": []}, None)

    plain = Session(registry, "read")
    session, close = await api._session(request, lambda r: plain, mutating=True)
    assert session is plain
    assert close is None                       # the factory's caller closes it
    await plain.close()

    exited: list[bool] = []

    class _Managed:
        def __init__(self) -> None:
            self.session = Session(registry, "read")

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *exc):
            exited.append(True)
            await self.session.close()
            return False

    session, close = await api._session(request, lambda r: _Managed(), mutating=True)
    assert close is not None
    await close()
    assert exited == [True]


def test_the_cache_bound_is_dead_at_the_default_limits(registry: Registry) -> None:
    """`MAX_CACHED_QUERY_CHARS` and `max_document_bytes` are the same number.

    `parse` refuses a document longer than `max_document_bytes` before anything
    else, so at the defaults nothing can be *long enough to skip the cache and
    short enough to parse* -- the "parsed and not cached" branch is unreachable.
    That is safe (the stricter check wins) but it means the memory bound the
    docstring describes does nothing until a deployment raises the parse limit,
    which is what the test below covers. `wreath mutant` found this by widening
    `MAX_CACHED_QUERY_CHARS` past reach and seeing nothing object.
    """
    from wreath._graphql.parser import Limits
    from wreath.graphql import MAX_CACHED_QUERY_CHARS

    assert MAX_CACHED_QUERY_CHARS == Limits().max_document_bytes

    api = GraphQL(registry, models=[User])
    api.validate()
    too_long = "query {" + " " * MAX_CACHED_QUERY_CHARS + " users { id } }"
    with pytest.raises(GraphQLSyntaxError, match="longer than"):
        api.parse(too_long)


def test_a_document_too_large_to_cache_is_parsed_but_not_remembered(
    registry: Registry,
) -> None:
    """The branch itself, on a deployment that raised the parse limit.

    The cache key is the client's own text, so counting entries let a few large
    documents hold far more memory than the count suggested. Past the bound a
    document is parsed and simply not remembered -- it is not refused.
    """
    from wreath._graphql.parser import Limits
    from wreath.graphql import MAX_CACHED_QUERY_CHARS

    api = GraphQL(
        registry, models=[User],
        limits=Limits(max_document_bytes=MAX_CACHED_QUERY_CHARS * 4),
    )
    api.validate()

    small = "query { users { id } }"
    assert api.parse(small) is api.parse(small)          # cached: same object back

    large = "query {" + " " * MAX_CACHED_QUERY_CHARS + " users { id } }"
    assert len(large) > MAX_CACHED_QUERY_CHARS
    first, second = api.parse(large), api.parse(large)
    assert first is not second                           # parsed twice, never held
    assert first.complexity == second.complexity         # and parsed correctly


# --- complexity is weighed, not counted --------------------------------------
#
# `cost=` on `field()`, `query()` and `mutation()` was threaded through three
# layers and read by nothing: `wreath mutant` dropped the keyword at every one
# of those call sites and no test objected, because the parser counted
# selections and the schema's weights were inert. `_graphql/cost.py` is the
# second pass that reads them; these are its tests.


def _weighed(api: GraphQL, source: str) -> int:
    from wreath._graphql.cost import weigh

    document = api.parse(source)
    return weigh(
        api._schema, document, document.operation(),
        max_complexity=api._limits.max_complexity,
    )


@pytest.mark.asyncio
async def test_a_declared_cost_is_charged_and_a_plain_selection_is_not(
    registry: Registry,
) -> None:
    """The whole point: two documents of the same *shape*, priced differently."""
    api = GraphQL(registry, models=[User])

    @api.field("User", "cheap", returns="Int")
    async def cheap(users, info):
        return [1 for _ in users]

    @api.field("User", "expensive", returns="Int", cost=50)
    async def expensive(users, info):
        return [1 for _ in users]

    api.validate()
    # `users` is a derived list root and declares 10; `id` is a column at 1.
    baseline = _weighed(api, "{ users { id cheap } }")
    assert baseline == 10 + 1 + 1
    assert _weighed(api, "{ users { id expensive } }") == 10 + 1 + 50
    # Identical selection counts, a 49-point difference. Before this pass they
    # were the same number.
    assert api.parse("{ users { id cheap } }").complexity == api.parse(
        "{ users { id expensive } }"
    ).complexity


@pytest.mark.asyncio
async def test_a_document_over_budget_is_refused_the_way_the_parser_refuses(
    registry: Registry,
) -> None:
    """One `code` for both refusals, so one client handler covers them.

    The parser's selection count passes this document easily -- it is four
    fields. The weights are what refuse it, which is the case `cost=` exists
    for and the case that could not be caught while parsing.
    """
    from wreath._graphql.parser import Limits

    api = GraphQL(registry, models=[User], limits=Limits(max_complexity=40))

    @api.field("User", "expensive", returns="Int", cost=50)
    async def expensive(users, info):
        return [1 for _ in users]

    api.validate()
    source = "{ users { id expensive } }"
    assert api.parse(source).complexity <= 40          # the parser is content

    body = await api.run(source, Session(registry, "read"))
    assert body["errors"][0]["extensions"]["code"] == "complexity"
    assert "costs more than 40" in body["errors"][0]["message"]
    assert "not a count of selections" in body["errors"][0]["message"]
    assert "data" not in body                          # nothing ran


@pytest.mark.asyncio
async def test_a_document_within_budget_still_runs(
    registry: Registry, database: FakeDatabase
) -> None:
    """The other half: the pass must not refuse what it should admit."""
    from wreath._graphql.parser import Limits

    database.connection.script("users", [user_row(1, "a@b.c", "Ada")])
    api = GraphQL(registry, models=[User], limits=Limits(max_complexity=40))
    api.validate()

    body = await api.run("{ user(id: 1) { id email } }", Session(registry, "read"))
    assert body == {"data": {"user": {"id": 1, "email": "a@b.c"}}}


@pytest.mark.asyncio
async def test_cost_is_additive_rather_than_multiplied_by_a_list(
    registry: Registry,
) -> None:
    """A list field does not multiply its children.

    Fan-out is what the declaration is *for*, and a multiplier read off a
    `limit` argument would make the budget depend on a value the client picks.
    """
    api = GraphQL(registry, models=[User, Post])
    api.validate()

    single = _weighed(api, "{ user(id: 1) { id } }")
    listed = _weighed(api, "{ users { id } }")
    assert single == 1 + 1          # a single-row root is an ordinary read
    assert listed == 10 + 1         # a list root declares 10, not 10 x anything
    # And `limit` does not enter into it: the budget must not depend on a value
    # the client picks. `max_page_size` is what bounds that.
    assert _weighed(api, "{ users(limit: 1000) { id } }") == listed

    # A relationship declares 5, because it fans out a whole level -- which is
    # the shape `cost=` exists for, charged once rather than per parent row.
    assert _weighed(api, "{ users { id posts { title } } }") == 10 + 1 + 5 + 1


@pytest.mark.asyncio
async def test_a_mutation_is_weighed_against_the_mutation_roots(
    registry: Registry,
) -> None:
    """Mutations live in their own namespace, and so do their weights."""
    api = GraphQL(registry, models=[User])

    @api.mutation("cheapWrite", returns="Int", cost=1)
    async def cheap_write(info):
        return 1

    @api.mutation("costlyWrite", returns="Int", cost=99)
    async def costly_write(info):
        return 1

    api.validate()
    assert _weighed(api, "mutation { cheapWrite }") == 1
    assert _weighed(api, "mutation { costlyWrite }") == 99
    # A query naming a mutation root resolves to nothing and costs nothing --
    # the executor reports the unknown field.
    assert _weighed(api, "{ costlyWrite }") == 0


@pytest.mark.asyncio
async def test_root_fragments_are_matched_against_the_operation_type(
    registry: Registry,
) -> None:
    """A root spread uses Query or Mutation, not one fixed root type."""
    api = GraphQL(registry, models=[User])

    @api.mutation("costlyWrite", returns="Int", cost=99)
    async def costly_write(info):
        return 1

    api.validate()
    assert _weighed(
        api,
        "query { ...Read } fragment Read on Query { users { id } }",
    ) == 10 + 1
    assert _weighed(
        api,
        "mutation { ...Write } "
        "fragment Write on Mutation { costlyWrite }",
    ) == 99


@pytest.mark.asyncio
async def test_fragments_are_expanded_before_they_are_weighed(
    registry: Registry,
) -> None:
    """A cost hidden behind a spread is still a cost.

    The weigher shares `execute._flatten` precisely so it cannot disagree with
    the executor about which fields run; a separate implementation is how a
    document gets billed for work it does not do, or not billed for work it
    does.
    """
    api = GraphQL(registry, models=[User])

    @api.field("User", "expensive", returns="Int", cost=50)
    async def expensive(users, info):
        return [1 for _ in users]

    api.validate()
    inline = _weighed(api, "{ users { id expensive } }")
    spread = _weighed(api, "{ users { id ...F } } fragment F on User { expensive }")
    assert spread == inline == 10 + 1 + 50
    nested = _weighed(
        api, "{ users { ... on User { expensive } } }"
    )
    assert nested == 10 + 50


@pytest.mark.asyncio
async def test_an_unknown_field_or_fragment_costs_nothing_and_keeps_its_own_error(
    registry: Registry,
) -> None:
    """A typo must not come back as `complexity`.

    The executor names the field or fragment it could not find; answering with
    a budget refusal instead would send whoever wrote the query looking at the
    wrong thing entirely.
    """
    api = GraphQL(registry, models=[User])
    api.validate()

    assert _weighed(api, "{ nope { id } }") == 0
    assert _weighed(api, "{ users { ...Missing } }") == 10

    body = await api.run("{ nope { id } }", Session(registry, "read"))
    assert body["errors"][0].get("extensions", {}).get("code") != "complexity"
    assert "nope" in body["errors"][0]["message"]      # named, as it should be


def test_an_unknown_declared_result_type_costs_only_its_root() -> None:
    """A stale declaration stays the executor's schema error, not a cost crash."""
    from wreath._graphql.cost import weigh

    document = parse("{ ghost { id } }")
    schema = SimpleNamespace(
        roots={"ghost": SimpleNamespace(cost=7, type_name="Ghost")},
        mutations={},
        type_of=lambda _name: None,
    )
    assert weigh(
        schema,
        document,
        document.operation(),
        max_complexity=100,
    ) == 7


def test_costing_tolerates_missing_object_selection_sets(
    registry: Registry,
) -> None:
    """The cost pass does not dereference a selection set the client omitted."""
    api = GraphQL(registry, models=[User, Post])
    api.validate()

    assert _weighed(api, "{ user(id: 1) }") == 1
    assert _weighed(api, "{ users { posts } }") == 10 + 5


@pytest.mark.asyncio
async def test_an_unresolvable_operation_is_left_for_the_executor(
    registry: Registry,
) -> None:
    """Two operations and no name is "which one?", not "too expensive"."""
    api = GraphQL(registry, models=[User])
    api.validate()

    source = "query A { users { id } } query B { users { id } }"
    body = await api.run(source, Session(registry, "read"))
    assert body["errors"][0].get("extensions", {}).get("code") != "complexity"
