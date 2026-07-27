"""GraphQL: parsing limits, schema derivation, and execution over the ORM."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from orm.conftest import (  # noqa: E402
    FakeDatabase,
    Post,
    User,
    post_row,
    user_row,
)

from wreath._graphql.parser import (  # noqa: E402
    GraphQLSyntaxError,
    Limits,
    parse,
)
from wreath.graphql import GraphQL, ResolverError  # noqa: E402
from wreath.orm.registry import Registry  # noqa: E402
from wreath.orm.session import Session  # noqa: E402

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


class DenyField:
    """An authorizer that refuses one `Type.field` resource."""

    def __init__(self, denied: str) -> None:
        self.denied = denied
        self.seen: list[str] = []

    async def authorize(self, request: Any, resource: str) -> Any:
        self.seen.append(resource)

        class Decision:
            allowed = resource != self.denied
            reason = "field is not readable"

        return Decision()


@pytest.mark.asyncio
async def test_field_access_is_checked_against_the_app_authorizer(
    registry: Registry, database: FakeDatabase
) -> None:
    """One policy language: the resource is `Type.field`, as for REST routes."""
    database.connection.script("users", [user_row(1, "a@b.c")])
    authorizer = DenyField("User.email")
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    body = await api.run("{ user(id: 1) { id email } }", Session(registry, "read"))

    assert body["data"] is None
    assert "not readable" in body["errors"][0]["message"]
    assert "User.id" in authorizer.seen


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


class RecordingAuthorizer:
    def __init__(self, denied: set[str] | None = None) -> None:
        self.denied = denied or set()
        self.asked: list[str] = []

    async def authorize(self, request: Any, resource: str) -> Any:
        self.asked.append(resource)

        class Decision:
            allowed = resource not in self.denied
            reason = f"{resource} denied"

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
    assert authorizer.asked.count("User.email") == 1


@pytest.mark.asyncio
async def test_root_fields_are_authorized_too(
    registry: Registry, database: FakeDatabase
) -> None:
    """`Query.users` is a resource, so a policy can deny a whole entry point."""
    authorizer = RecordingAuthorizer(denied={"Query.users"})
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    body = await api.run("{ users { id } }", Session(registry, "read"))
    assert "Query.users denied" in body["errors"][0]["message"]


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
    authorizer = RecordingAuthorizer(denied={"billing.read"})
    api = GraphQL(registry, models=[User, Post], authorizer=authorizer)

    @api.field("User", "balance", returns="Int", policy="billing.read")
    async def balance(users, info):
        return [0 for _ in users]

    body = await api.run("{ users { balance } }", Session(registry, "read"))
    assert "billing.read denied" in body["errors"][0]["message"]


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
