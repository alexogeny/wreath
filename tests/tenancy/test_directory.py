from __future__ import annotations

from typing import Any, cast

import pytest

from wreath import Request
from wreath.tenancy import (
    InMemoryTenantDirectory,
    Tenancy,
    TenancyError,
    TenancyMiddleware,
    Tenant,
    TenantHeader,
    TenantHostLabel,
    TenantNotBound,
    TenantNotReady,
    TenantSessionClaim,
    TenantStatus,
    TenantSuspended,
    UnknownTenant,
    cedar_context,
    check_enqueue_tenant,
    current_tenant,
    deprovision_tenant,
    telemetry_attributes,
    tenant_scope,
)


class _Request:
    """Just enough request for a source to read a name out of.

    `headers` is the **raw ASGI list of lowercase byte pairs** and `header()`
    is the accessor, because that is what a real `wreath.Request` is. An earlier
    version of this fake exposed a `dict`, so `TenantHeader` and
    `TenantHostLabel` were written against `headers.get(...)` -- which passed
    every test here and raised `AttributeError` on the first real request. A fake
    that is easier to use than the thing it stands in for tests itself.
    """

    def __init__(self, headers: dict[str, str] | None = None, session: dict | None = None):
        self.headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ]
        self.state = type("State", (), {"session": session})()

    def header(self, name: str | bytes, default: str | None = None) -> str | None:
        wanted = (name.lower() if isinstance(name, str) else name.decode()).lower()
        for key, value in self.headers:
            if key.decode("latin-1") == wanted:
                return value.decode("latin-1")
        return default


ACME = Tenant(key="acme", schema="tenant_acme", role="tenant_acme")


@pytest.mark.parametrize(
    ("source", "headers"),
    [
        (
            TenantHeader("X-Tenant", trusted=True),
            [(b"x-tenant", b"acme"), (b"x-tenant", b"globex")],
        ),
        (
            TenantHostLabel("example.com"),
            [(b"host", b"acme.example.com"), (b"host", b"globex.example.com")],
        ),
    ],
)
def test_duplicate_tenant_selectors_are_refused(
    source: TenantHeader | TenantHostLabel,
    headers: list[tuple[bytes, bytes]],
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
        },
        cast(Any, None),
    )
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME, Tenant("globex", "tenant_globex")]),
        source=source,
    )
    with pytest.raises(TenancyError, match="more than once"):
        tenancy.resolve_request(request)


def test_a_tenant_carries_identity_placement_and_lifecycle() -> None:
    assert ACME.key == "acme"
    assert ACME.status is TenantStatus.ACTIVE
    assert ACME.context().schema == "tenant_acme"


def test_a_tenant_snapshots_metadata_at_the_directory_boundary() -> None:
    metadata = {"plan": "free", "limits": {"seats": 2}}
    tenant = Tenant(key="mutable", schema="tenant_mutable", metadata=metadata)

    metadata["plan"] = "enterprise"
    metadata["limits"]["seats"] = 200

    assert tenant.metadata == {"plan": "free", "limits": {"seats": 2}}


def test_tenant_metadata_freezes_nested_sequences_and_refuses_cycles() -> None:
    nested = [{"scope": "read"}]
    tenant = Tenant(key="nested", schema="tenant_nested", metadata={"grants": nested})
    nested[0]["scope"] = "admin"
    nested.append({"scope": "write"})
    assert tenant.metadata == {"grants": ({"scope": "read"},)}

    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(TenancyError, match="must not contain cycles"):
        Tenant(key="cyclic", schema="tenant_cyclic", metadata=cyclic)

    tagged = Tenant(key="tagged", schema="tenant_tagged", metadata={"tags": {"a", "b"}})
    assert tagged.metadata["tags"] == frozenset({"a", "b"})


def test_a_directory_refuses_two_rows_for_the_same_external_tenant_key() -> None:
    with pytest.raises(TenancyError, match="duplicate tenant key"):
        InMemoryTenantDirectory(
            [
                Tenant(key="acme", schema="tenant_acme"),
                Tenant(key="acme", schema="tenant_globex"),
            ]
        )


def test_a_directory_update_cannot_remap_an_external_key_to_another_schema() -> None:
    directory = InMemoryTenantDirectory([ACME])
    with pytest.raises(TenancyError, match="cannot change its schema or role"):
        directory.add(Tenant(key="acme", schema="tenant_globex", role="tenant_globex"))


def test_a_directory_may_add_a_new_key_and_update_status_in_the_same_placement() -> None:
    directory = InMemoryTenantDirectory([ACME])
    directory.add(Tenant(key="globex", schema="tenant_globex"))
    directory.add(
        Tenant(
            key="acme",
            schema=ACME.schema,
            role=ACME.role,
            status=TenantStatus.SUSPENDED,
        )
    )
    assert directory.resolve("globex").schema == "tenant_globex"
    assert directory.resolve("acme").status is TenantStatus.SUSPENDED


def test_a_tenant_key_that_is_not_an_identifier_is_refused_at_construction() -> None:
    with pytest.raises(TenancyError, match="character"):
        Tenant(key="acme; drop schema public", schema="x", role="x")


def test_a_tenant_schema_that_is_not_an_identifier_is_refused_too() -> None:
    with pytest.raises(TenancyError, match="character"):
        Tenant(key="acme", schema="public; drop table users", role="x")


def test_a_directory_lookup_that_misses_refuses_rather_than_falling_back() -> None:
    with pytest.raises(UnknownTenant, match="no default tenant"):
        InMemoryTenantDirectory([]).resolve("ghost")


def test_a_suspended_tenant_cannot_be_bound_at_all() -> None:
    suspended = Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)
    with pytest.raises(TenantSuspended, match="suspended"):
        suspended.require_bindable()


def test_a_tenant_that_has_not_been_migrated_is_not_servable() -> None:
    provisioning = Tenant(key="new", schema="tenant_new", status=TenantStatus.PROVISIONING)
    with pytest.raises(TenantNotReady, match="still provisioning"):
        provisioning.require_bindable()


def test_a_retired_tenant_is_refused_with_its_own_message() -> None:
    retired = Tenant(key="old", schema="tenant_old", status=TenantStatus.RETIRED)
    with pytest.raises(TenantNotReady, match="retired"):
        retired.require_bindable()


def test_tenant_resolution_has_no_default_source() -> None:
    with pytest.raises(TenancyError, match="source="):
        Tenancy(directory=InMemoryTenantDirectory([ACME]))


@pytest.mark.parametrize("trusted", [1, "yes", object()])
def test_a_tenant_header_requires_the_exact_trusted_flag(trusted) -> None:
    with pytest.raises(TenancyError, match="trusted=True"):
        Tenancy(
            directory=InMemoryTenantDirectory([ACME]),
            source=TenantHeader("X-Tenant", trusted=trusted),
        )


@pytest.mark.parametrize("optional", [1, "yes", object()])
def test_optional_tenant_middleware_requires_an_exact_boolean(optional) -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]),
        source=TenantHeader("X-Tenant", trusted=True),
    )
    with pytest.raises(TenancyError, match="optional must be true or false"):
        TenancyMiddleware(tenancy, optional=optional)


def test_a_resolved_name_is_looked_up_and_never_used_as_a_schema() -> None:
    directory = InMemoryTenantDirectory([Tenant(key="acme", schema="t_7f3a", role="r_7f3a")])
    tenancy = Tenancy(directory=directory, source=TenantHeader("X-Tenant", trusted=True))
    assert tenancy.resolve_name("acme").schema == "t_7f3a"


def test_a_request_naming_no_tenant_is_refused_naming_the_source() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]),
        source=TenantHeader("X-Tenant", trusted=True),
    )
    with pytest.raises(UnknownTenant, match="X-Tenant"):
        tenancy.resolve_request(_Request())


def test_the_host_source_requires_its_suffix_and_refuses_the_apex() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantHostLabel("example.com")
    )
    assert tenancy.resolve_request(_Request({"host": "acme.example.com"})).key == "acme"
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request({"host": "example.com"}))


def test_the_host_source_refuses_an_empty_suffix_at_declaration_time() -> None:
    for suffix in ("", None, 7):
        with pytest.raises(TenancyError, match="non-empty DNS suffix"):
            TenantHostLabel(cast(Any, suffix))


def test_the_host_source_ignores_a_port_and_is_case_insensitive() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantHostLabel("example.com")
    )
    assert tenancy.resolve_request(_Request({"host": "ACME.example.com:8443"})).key == "acme"


def test_the_host_source_refuses_a_lookalike_suffix() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantHostLabel("example.com")
    )
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request({"host": "acme.example.com.evil.test"}))


def test_the_session_source_reads_state_the_server_wrote() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantSessionClaim("tenant")
    )
    assert tenancy.resolve_request(_Request(session={"tenant": "acme"})).key == "acme"


def test_resolution_checks_status_as_well_as_existence() -> None:
    directory = InMemoryTenantDirectory(
        [Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)]
    )
    tenancy = Tenancy(directory=directory, source=TenantHeader("X-Tenant", trusted=True))
    with pytest.raises(TenantSuspended):
        tenancy.resolve_request(_Request({"X-Tenant": "acme"}))


def test_nothing_is_bound_outside_a_scope() -> None:
    with pytest.raises(TenantNotBound):
        current_tenant()


def test_a_scope_binds_and_restores_rather_than_clearing() -> None:
    other = Tenant(key="globex", schema="tenant_globex")
    with tenant_scope(ACME):
        with tenant_scope(other):
            assert current_tenant().key == "globex"
        assert current_tenant().key == "acme"


def test_a_scope_refuses_a_tenant_that_may_not_serve() -> None:
    suspended = Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)
    with pytest.raises(TenantSuspended), tenant_scope(suspended):
        pass  # pragma: no cover - the scope refuses before the body runs


def test_a_scope_given_a_name_needs_a_directory_to_resolve_it_in() -> None:
    with pytest.raises(TenancyError, match="directory"), tenant_scope("acme"):
        pass  # pragma: no cover - refused before the body runs


def test_the_cedar_context_carries_the_tenant_separately_from_organisations() -> None:
    with tenant_scope(ACME):
        assert cedar_context() == {"tenant": "acme"}
    assert cedar_context() == {}


def test_telemetry_attributes_carry_the_tenant() -> None:
    with tenant_scope(ACME):
        assert telemetry_attributes() == {"tenant": "acme"}


def test_work_enqueued_inside_a_scope_inherits_that_tenant() -> None:
    with tenant_scope(ACME):
        assert check_enqueue_tenant(None) == "acme"
        assert check_enqueue_tenant("") == "acme"


def test_an_explicit_tenant_matching_the_scope_is_accepted() -> None:
    with tenant_scope(ACME):
        assert check_enqueue_tenant("acme") == "acme"


def test_an_explicit_tenant_contradicting_the_scope_is_refused() -> None:
    with tenant_scope(ACME), pytest.raises(TenancyError, match="cannot tell which"):
        check_enqueue_tenant("globex")


def test_outside_a_scope_an_explicit_tenant_is_passed_through() -> None:
    assert check_enqueue_tenant("globex") == "globex"
    assert check_enqueue_tenant(None) == ""


async def test_deprovision_requires_the_exact_force_confirmation() -> None:
    class OccupiedConnection:
        def __init__(self) -> None:
            self.executed = []

        async def fetch(self, *args):
            return [("items",)]

        async def execute(self, sql):
            self.executed.append(sql)

    connection = OccupiedConnection()
    with pytest.raises(TenancyError, match="force must be true or false"):
        await deprovision_tenant(connection, ACME, force=cast(Any, 1))
    assert connection.executed == []


async def test_deprovision_checks_an_occupied_schema_unless_force_is_exactly_true() -> None:
    class Connection:
        def __init__(self) -> None:
            self.fetches = 0
            self.executed = []

        async def fetch(self, *args):
            self.fetches += 1
            return [("items",)]

        async def execute(self, sql):
            self.executed.append(sql)

    guarded = Connection()
    with pytest.raises(TenancyError, match="not empty"):
        await deprovision_tenant(guarded, ACME, force=False)
    assert guarded.fetches == 1
    assert guarded.executed == []

    confirmed = Connection()
    await deprovision_tenant(confirmed, ACME, force=True)
    assert confirmed.fetches == 0
    assert confirmed.executed == [
        'DROP SCHEMA IF EXISTS "tenant_acme" CASCADE',
        'DROP OWNED BY "tenant_acme" CASCADE',
        'DROP ROLE IF EXISTS "tenant_acme"',
    ]
