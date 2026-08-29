from __future__ import annotations

import contextlib
import dataclasses
import json
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest

from wreath.app import Wreath
from wreath.config import Env, Environment, Secret, SettingsError
from wreath.http_client import DestinationPolicy
from wreath.infra import (
    GapKind,
    InfrastructurePlan,
    Presence,
    infer,
    render_json,
    render_text,
    settings_keys,
)
from wreath.postgres import PoolConfig

DSN = "postgresql://trek@db.internal:5432/trek"
REPLICA = "postgresql://trek@replica.internal:5432/trek"


def build(**databases: str) -> Wreath:
    app = Wreath()
    for name, dsn in (databases or {"main": DSN}).items():
        app.postgres(name, dsn=dsn)
    return app


def plan_for(app: Wreath, **kwargs: object) -> InfrastructurePlan:
    return infer(app, application="trek.app:app", **kwargs)  # type: ignore[arg-type]


def test_a_registered_database_is_derived_with_its_endpoint_and_pools() -> None:
    plan = plan_for(build())
    (database,) = plan.databases
    assert database.name == "main"
    assert database.endpoint == "db.internal:5432"
    assert database.database == "trek"
    assert database.user == "trek"
    assert {(p.workload, p.min_size, p.max_size) for p in database.pools} == {
        ("read", 1, 10),
        ("write", 0, 10),
    }


@pytest.mark.parametrize(
    ("dsn", "endpoint", "database", "user"),
    [
        ("postgresql://trek@db.internal:5432/trek", "db.internal:5432", "trek", "trek"),
        # No port: PostgreSQL's default is the one the deployment has to open.
        ("postgresql://trek@db.internal/trek", "db.internal:5432", "trek", "trek"),
        # No user: the role comes from PGUSER or the process owner.
        ("postgresql://db.internal/trek", "db.internal:5432", "trek", None),
        # A local socket, which has no host at all.
        ("postgresql:///trek", "localhost:5432", "trek", None),
        # No database in the path: the role's own name is used.
        ("postgresql://trek@db.internal:5432/", "db.internal:5432", "", "trek"),
        # A pooler in front of it, which is the port a rule has to open.
        (
            "postgresql://trek@pgbouncer.internal:6432/trek",
            "pgbouncer.internal:6432",
            "trek",
            "trek",
        ),
    ],
)
def test_a_dsn_is_split_without_inventing_what_it_omits(
    dsn: str, endpoint: str, database: str, user: str | None
) -> None:
    app = Wreath()
    app.postgres("main", dsn=dsn)
    (found,) = plan_for(app).databases
    assert (found.endpoint, found.database, found.user) == (endpoint, database, user)


def test_a_password_never_reaches_the_plan() -> None:
    app = Wreath()
    app.postgres("main", dsn="postgresql://trek:hunter2@db.internal:5432/trek")
    plan = plan_for(app)
    assert "hunter2" not in render_text(plan)
    assert "hunter2" not in render_json(plan)


def test_a_workload_dsn_is_reported_as_a_second_endpoint_to_provision() -> None:
    app = Wreath()
    app.postgres("main", dsn=DSN, workload_dsns={"read": REPLICA})
    (database,) = plan_for(app).databases
    reads = next(pool for pool in database.pools if pool.workload == "read")
    writes = next(pool for pool in database.pools if pool.workload == "write")
    assert reads.endpoint == "replica.internal:5432"
    assert writes.endpoint == "db.internal:5432"


def test_a_listen_doorbell_is_counted_against_the_pool_it_holds() -> None:
    app = build()
    app.jobs("ingest", database="main")
    app.messaging("events", database="main")
    (database,) = plan_for(app).databases
    write = next(b for b in database.budgets if b.workload == "write")
    assert write.held == 2
    assert write.available == 8
    assert any("jobs runner 'ingest'" in holder for holder in write.holders)
    assert any("message bus 'events'" in holder for holder in write.holders)


def test_a_doorbell_is_counted_against_the_workload_it_actually_uses() -> None:
    app = build()
    app.jobs("ingest", database="main", workload="read")
    (database,) = plan_for(app).databases
    budgets = {budget.workload: budget for budget in database.budgets}
    assert budgets["read"].held == 1
    assert budgets["write"].held == 0
    assert budgets["write"].holders == ()


def test_a_pool_filled_by_its_own_doorbell_is_a_gap() -> None:
    app = Wreath()
    app.postgres(
        "main",
        dsn=DSN,
        pools={"read": PoolConfig(), "write": PoolConfig(min_size=0, max_size=1)},
    )
    app.jobs("ingest", database="main")
    plan = plan_for(app)
    (gap,) = [gap for gap in plan.gaps if gap.kind is GapKind.CAPACITY]
    assert gap.subject == "main.write pool"
    assert "0 for requests" in gap.detail


def test_a_component_is_attributed_to_the_database_its_runner_named() -> None:
    app = build(main=DSN, analytics="postgresql://trek@warehouse.internal:5432/trek")
    app.jobs("ingest", database="analytics")
    plan = plan_for(app)
    by_name = {database.name: database for database in plan.databases}
    assert by_name["main"].components == ()
    (component,) = by_name["analytics"].components
    assert component.name == "jobs"
    assert component.declared_by == "app.jobs('ingest')"
    assert component.relations == ("jobs",)


def test_two_queues_claiming_one_component_report_it_once() -> None:
    app = build()
    app.jobs("ingest", database="main")
    app.jobs("reports", database="main")
    (database,) = plan_for(app).databases
    assert [component.name for component in database.components] == ["jobs"]
    assert database.components[0].declared_by == "app.jobs('ingest')"


def _with_durable_inbox(app: Wreath) -> Wreath:
    """A webhook hub whose inbox owns tables but which holds no database.

    The one shipped shape that has to fall back to "the only database": a hub is
    handed a session per call, so neither it nor its inbox ever sees a
    `Database` object.
    """
    from wreath.webhooks import HMACWebhookVerifier, PostgresWebhookInbox

    @contextlib.asynccontextmanager
    async def session() -> AsyncIterator[None]:
        yield None

    hub = app.webhooks("payments")
    hub.source(
        "stripe",
        path="/hooks/stripe",
        verifier=HMACWebhookVerifier({"v1": b"k" * 32}),
        inbox=PostgresWebhookInbox(),
        session_factory=session,
    )
    return app


def test_a_holder_with_no_database_of_its_own_falls_back_to_the_only_one() -> None:
    (database,) = plan_for(_with_durable_inbox(build())).databases
    (component,) = database.components
    assert component.name == "webhook-inbox"
    assert component.declared_by == "app.webhooks('payments')"


def test_two_databases_make_that_fallback_ambiguous_and_that_is_a_gap() -> None:
    app = _with_durable_inbox(
        build(main=DSN, archive="postgresql://trek@warehouse.internal:5432/archive")
    )
    plan = plan_for(app)
    assert [database.components for database in plan.databases] == [(), ()]
    (gap,) = [gap for gap in plan.gaps if gap.kind is GapKind.UNDERIVABLE]
    assert gap.subject == "webhook-inbox tables"
    assert "registers 2 (archive, main)" in gap.detail
    assert "wreath schema sql --component webhook-inbox" in gap.detail
    rows = {row.module: row for row in plan.subsystems}
    assert rows["wreath.webhooks"].presence is Presence.DECLARED


def test_a_subsystem_with_no_database_anywhere_claims_nothing() -> None:
    plan = plan_for(_with_durable_inbox(Wreath()))
    assert plan.databases == ()
    assert [gap for gap in plan.gaps if gap.kind is GapKind.UNDERIVABLE] == []
    rows = {row.module: row for row in plan.subsystems}
    assert rows["wreath.webhooks"].presence is Presence.DECLARED


def test_the_framework_makes_the_same_attribution() -> None:
    app = build(main=DSN, analytics="postgresql://trek@warehouse.internal:5432/trek")
    app.jobs("ingest", database="analytics")
    grouped = app._components_by_database(app.schema_components())
    assert list(grouped) == [app._databases["analytics"]]
    assert [claim.name for claim in grouped[app._databases["analytics"]]] == ["jobs"]


def test_a_local_object_store_requires_a_volume(tmp_path: Path) -> None:
    app = build()
    app.objects("media", backend="local", root=tmp_path / "media")
    (store,) = plan_for(app).object_stores
    assert store.backend == "local"
    assert store.root == str(tmp_path / "media")
    assert store.bucket is None
    assert any("survives a restart" in line for line in store.requires)


def test_an_s3_object_store_requires_a_bucket_and_a_lifecycle_rule() -> None:
    app = build()
    app.objects(
        "cards",
        backend="s3",
        bucket="trek-cards",
        region="eu-west-2",
        access_key="AKIAEXAMPLE",
        secret_key="secret",
    )
    (store,) = plan_for(app).object_stores
    assert store.backend == "s3"
    assert store.bucket == "trek-cards"
    assert store.region == "eu-west-2"
    assert store.host == "trek-cards.s3.eu-west-2.amazonaws.com"
    assert any("multipart" in line for line in store.requires)


def test_a_secret_key_never_reaches_the_plan() -> None:
    app = build()
    app.objects(
        "cards",
        backend="s3",
        bucket="trek-cards",
        region="eu-west-2",
        access_key="AKIAEXAMPLE",
        secret_key="a-real-looking-secret",
    )
    plan = plan_for(app)
    assert "a-real-looking-secret" not in render_json(plan)
    assert "AKIAEXAMPLE" not in render_json(plan)


def test_egress_is_derived_from_the_origin_a_client_is_pinned_to() -> None:
    app = build()
    app.http_client("forage", base_url="https://forage.example.com/v2")
    (rule,) = plan_for(app).egress
    assert rule.origin == "https://forage.example.com"
    assert rule.base_path == "/v2"
    assert rule.declared_by == "app.http_client('forage')"
    # A client always carries a policy, so the default one is a rule too: any
    # global host, on http or https, and nothing private, loopback or link-local.
    assert rule.destination == "schemes http/https; global addresses only"


def test_a_destination_policy_is_the_egress_rule_it_already_declares() -> None:
    app = build()
    app.http_client(
        "forage",
        base_url="https://forage.example.com",
        destination=DestinationPolicy(hosts=("forage.example.com",), ports=frozenset({443})),
    )
    (rule,) = plan_for(app).egress
    assert rule.destination is not None
    assert "hosts forage.example.com" in rule.destination
    assert "ports 443" in rule.destination
    assert "global addresses only" in rule.destination


def test_an_s3_store_contributes_its_own_egress_rule() -> None:
    app = build()
    app.objects(
        "cards",
        backend="s3",
        bucket="trek-cards",
        region="eu-west-2",
        access_key="AKIAEXAMPLE",
        secret_key="secret",
    )
    (rule,) = plan_for(app).egress
    assert rule.origin == "https://trek-cards.s3.eu-west-2.amazonaws.com"
    assert rule.declared_by == "app.objects('cards', backend='s3')"


def test_no_client_says_so_rather_than_printing_nothing() -> None:
    text = render_text(plan_for(build()))
    assert "none: this application pins no outbound HTTP client." in text


def test_the_listener_counts_the_routes_the_application_compiled() -> None:
    app = build()

    @app.get("/sightings")
    async def sightings(request: object) -> dict[str, int]:
        return {"count": 0}

    @app.post("/sightings")
    async def record(request: object) -> dict[str, int]:
        return {"count": 1}

    (listener,) = plan_for(app).listeners
    assert listener.routes == 2
    assert listener.methods == ("GET", "POST")
    assert listener.websocket_routes == 0


def test_every_shared_subsystem_is_listed_whether_or_not_it_is_used() -> None:
    plan = plan_for(build())
    modules = [row.module for row in plan.subsystems]
    for expected in (
        "wreath.jobs",
        "wreath.messaging",
        "wreath.session_store",
        "wreath.policy.ratelimit",
        "wreath.policy.idempotency",
        "wreath.workflows",
        "wreath.progress",
        "wreath.passes",
    ):
        assert expected in modules
    assert all(
        row.backing.startswith("PostgreSQL") or row.backing in {"in-process memory"}
        for row in plan.subsystems
    ), [row.backing for row in plan.subsystems]


def test_a_declared_queue_and_bus_report_the_database_they_share() -> None:
    app = build()
    app.jobs("ingest", database="main")
    app.messaging("events", database="main")
    plan = plan_for(app)
    rows = {row.module: row for row in plan.subsystems}
    assert rows["wreath.jobs"].presence is Presence.DECLARED
    assert rows["wreath.jobs"].detail == "runner 'ingest' on database 'main'"
    assert rows["wreath.messaging"].presence is Presence.DECLARED
    assert rows["wreath.messaging"].detail == "bus 'events' on database 'main'"
    text = render_text(plan)
    assert "There is no" in text
    assert "broker, no cache server and no second datastore" in text


def test_progress_is_declared_only_when_the_queue_was_given_a_registry() -> None:
    from wreath.progress import ProgressRegistry

    plain = build()
    plain.jobs("ingest", database="main")
    assert {row.module: row for row in plan_for(plain).subsystems}[
        "wreath.progress"
    ].presence is Presence.ABSENT

    watched = build()
    watched.jobs("ingest", database="main", progress=ProgressRegistry())
    row = {row.module: row for row in plan_for(watched).subsystems}["wreath.progress"]
    assert row.presence is Presence.DECLARED
    assert row.detail == "watched queue 'ingest'"


def test_a_chunked_pass_is_declared_by_the_runner_that_drives_it() -> None:
    from wreath.passes import ChunkedPass, DutyCycle, Key, Purge, Rows, Sealed, Table

    plain = build()
    plain.jobs("ingest", database="main")
    assert {row.module: row for row in plan_for(plain).subsystems}[
        "wreath.passes"
    ].presence is Presence.ABSENT

    driving = build()
    runner = driving.jobs("ingest", database="main")
    runner.drive(
        ChunkedPass(
            "purge_sightings",
            over=Table("sightings"),
            # `Sealed()` measures its frontier against the database clock, so the
            # leading key has to be a timestamp -- the pass refuses otherwise.
            units=Rows(
                key=(
                    Key("seen_at", "timestamptz", indexed=True),
                    Key("id", "int8", unique=True),
                ),
                limit=100,
                within="2s",
            ),
            frontier=Sealed(),
            work=Purge(),
            pace=DutyCycle(1.0),
        ),
        # `Sealed()` re-derives its frontier, so the pass runs in cycles and the
        # runner refuses to drive it without something to start the next one.
        cron="*/5 * * * *",
    )
    row = {row.module: row for row in plan_for(driving).subsystems}["wreath.passes"]
    assert row.presence is Presence.DECLARED
    assert row.detail == "driven by runner 'ingest'"


def test_an_unregisterable_subsystem_is_unobservable_not_absent() -> None:
    rows = {row.module: row for row in plan_for(build()).subsystems}
    assert rows["wreath.rooms"].presence is Presence.UNOBSERVABLE
    assert "RoomRegistry" in rows["wreath.rooms"].detail
    assert rows["wreath.messaging"].presence is Presence.ABSENT


def test_a_middleware_owned_table_is_reported_as_the_same_postgresql() -> None:
    from wreath.policy import HttpPolicy
    from wreath.policy.ratelimit import PostgresRateLimitStore, RateLimitPolicy

    app = build()
    store = PostgresRateLimitStore(app._databases["main"])
    app.configure_http_policy(
        HttpPolicy(rate_limit=RateLimitPolicy(limit=10, window=1.0, store=store))
    )
    plan = plan_for(app)
    rows = {row.module: row for row in plan.subsystems}
    assert rows["wreath.policy.ratelimit"].presence is Presence.DECLARED
    assert rows["wreath.policy.ratelimit"].detail == "HttpPolicy on database 'main'"
    # The other two middleware-owned subsystems must stay absent: one store's
    # claim must not be read as a claim by all three.
    assert rows["wreath.session_store"].presence is Presence.ABSENT
    assert rows["wreath.policy.idempotency"].presence is Presence.ABSENT
    (database,) = plan.databases
    assert "ratelimit" in {component.name for component in database.components}


def test_the_framework_collects_a_middleware_owned_table() -> None:
    from wreath.policy import HttpPolicy
    from wreath.policy.ratelimit import PostgresRateLimitStore, RateLimitPolicy

    app = build()
    store = PostgresRateLimitStore(app._databases["main"])
    middleware = RateLimitPolicy(limit=10, window=1.0, store=store)
    app.configure_http_policy(HttpPolicy(rate_limit=middleware))
    assert not hasattr(middleware, "component")
    assert middleware.schema_owners == (store,)
    assert [claim.name for claim in app.schema_components()] == ["ratelimit"]
    grouped = app._components_by_database(app.schema_components())
    assert list(grouped) == [app._databases["main"]]


@dataclass
class Database:
    host: str
    port: int = 5432


@dataclass
class Settings:
    dsn: str
    token: Secret[str]
    database: Database
    region: Annotated[str, Env("AWS_REGION")]
    debug: bool = False


def test_settings_keys_are_the_keys_environment_bind_actually_reads() -> None:
    derived = settings_keys(Settings, prefix="TREK")
    required = {key.key: "x" for key in derived if key.required}
    required["TREK_DATABASE__PORT"] = "5432"
    bound = Environment(required).bind(Settings, prefix="TREK")
    assert bound.region == "x"
    for missing in list(required):
        partial = {key: value for key, value in required.items() if key != missing}
        try:
            Environment(partial).bind(Settings, prefix="TREK")
        except SettingsError as error:
            assert missing in {entry["key"] for entry in error.errors}
        else:
            if missing != "TREK_DATABASE__PORT":  # the one field with a default
                pytest.fail(f"binding succeeded without {missing}")


def test_settings_keys_follow_the_nesting_and_alias_rules() -> None:
    derived = {key.field: key for key in settings_keys(Settings, prefix="TREK")}
    assert derived["dsn"].key == "TREK_DSN"
    assert derived["database.host"].key == "TREK_DATABASE__HOST"
    assert derived["region"].key == "AWS_REGION"
    assert derived["token"].secret is True
    assert derived["token"].annotation == "Secret[str]"
    assert derived["debug"].required is False


def test_a_settings_field_with_no_supplier_is_a_gap_named_by_key() -> None:
    plan = plan_for(
        build(),
        settings=[(Settings, "trek.config:Settings", "TREK")],
        supplied={"TREK_DSN": "deploy.env", "AWS_REGION": "deploy.env"},
    )
    missing = {gap.subject for gap in plan.gaps if gap.kind is GapKind.SETTINGS_KEY}
    assert missing == {"TREK_TOKEN", "TREK_DATABASE__HOST"}
    (gap,) = [gap for gap in plan.gaps if gap.subject == "TREK_TOKEN"]
    assert "trek.config:Settings requires token (Secret[str])" in gap.detail
    assert "TREK_TOKEN" in render_text(plan)


def test_a_defaulted_field_is_supplied_by_its_own_default() -> None:
    (contract,) = plan_for(build(), settings=[(Settings, "trek.config:Settings", "TREK")]).settings
    debug = next(key for key in contract.keys if key.field == "debug")
    assert debug.supplied_by == "default"


def test_a_supplied_key_beats_the_fields_own_default() -> None:
    (contract,) = plan_for(
        build(),
        settings=[(Settings, "trek.config:Settings", "TREK")],
        supplied={"TREK_DEBUG": "deploy.env"},
    ).settings
    debug = next(key for key in contract.keys if key.field == "debug")
    assert debug.supplied_by == "deploy.env"


def test_a_settings_model_with_no_prefix_reads_the_bare_field_names() -> None:
    derived = {key.field: key.key for key in settings_keys(Settings)}
    assert derived["dsn"] == "DSN"
    assert derived["database.host"] == "DATABASE__HOST"
    assert derived["region"] == "AWS_REGION"


def test_a_default_factory_counts_as_a_default() -> None:
    @dataclass
    class WithFactory:
        hosts: list[str] = dataclasses.field(default_factory=list)

    (key,) = settings_keys(WithFactory, prefix="TREK")
    assert key.required is False
    assert key.annotation == "list[str]"


def test_an_unresolvable_annotation_refuses_where_bind_refuses() -> None:
    @dataclass
    class Broken:
        value: NoSuchTypeAnywhere  # noqa: F821  -- deliberately unresolvable

    with pytest.raises(TypeError, match="unresolvable annotation"):
        settings_keys(Broken, prefix="TREK")
    with pytest.raises(TypeError, match="unresolvable annotation"):
        Environment({}).bind(Broken, prefix="TREK")


def test_a_supplied_key_no_field_reads_is_reported_too() -> None:
    plan = plan_for(
        build(),
        settings=[(Settings, "trek.config:Settings", "TREK")],
        supplied={"TREK_DSN": "deploy.env", "TREK_DNS": "deploy.env"},
        dotenv_keys={"TREK_DSN": "deploy.env", "TREK_DNS": "deploy.env"},
    )
    unread = [gap for gap in plan.gaps if gap.kind is GapKind.UNREAD_KEY]
    assert [gap.subject for gap in unread] == ["TREK_DNS"]
    assert plan.settings[-1].unread == ("TREK_DNS",)


def test_an_unread_key_needs_a_contract_to_be_unread_against() -> None:
    plan = plan_for(
        build(),
        supplied={"TREK_DNS": "deploy.env"},
        dotenv_keys={"TREK_DNS": "deploy.env"},
    )
    assert plan.gaps == ()


def test_no_settings_model_says_the_contract_is_unchecked() -> None:
    plan = plan_for(build())
    assert any("environment contract is unchecked" in note for note in plan.notes)
    assert "not checked: no settings model was named." in render_text(plan)


def test_the_unchecked_notes_disappear_once_both_halves_are_given() -> None:
    plan = plan_for(
        build(),
        settings=[(Settings, "trek.config:Settings", "TREK")],
        supplied={"TREK_DSN": "deploy.env"},
    )
    assert not any("environment contract is unchecked" in note for note in plan.notes)
    assert not any("No environment supplier" in note for note in plan.notes)
    assert any("TelemetryConfig" in note for note in plan.notes)


def test_a_missing_supplier_is_called_out_rather_than_left_implicit() -> None:
    plan = plan_for(build(), settings=[(Settings, "trek.config:Settings", "TREK")])
    assert any("No environment supplier was named" in note for note in plan.notes)


def test_the_plan_is_a_frozen_dataclass_tree() -> None:
    plan = plan_for(build())
    assert dataclasses.is_dataclass(plan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.application = "other"  # type: ignore[misc]


def test_json_carries_the_derived_available_connections() -> None:
    app = build()
    app.jobs("ingest", database="main")
    data = json.loads(render_json(plan_for(app)))
    write = next(
        budget for budget in data["databases"][0]["budgets"] if budget["workload"] == "write"
    )
    assert write["held"] == 1
    assert write["available"] == 9


def test_infer_refuses_something_that_is_not_an_application() -> None:
    with pytest.raises(TypeError, match="not a built wreath application"):
        infer(object(), application="nope:app")


def test_infer_refuses_a_half_shaped_impostor() -> None:

    class OnlyDatabases:
        _databases: dict[str, object] = {}

    class OnlyComponents:
        def schema_components(self) -> tuple[object, ...]:
            return ()

    for impostor in (OnlyDatabases(), OnlyComponents()):
        with pytest.raises(TypeError, match="not a built wreath application"):
            infer(impostor, application="nope:app")


_FORBIDDEN_SDKS = (
    "boto3",
    "botocore",
    "aiobotocore",
    "s3transfer",
    "google.cloud",
    "azure",
    "requests",
    "urllib3",
    "httpx",
)


def test_inference_imports_no_cloud_sdk() -> None:
    program = (
        "import sys, json\n"
        "import wreath.infra, wreath.infra.cli\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
        f"{ {name.split('.')[0] for name in _FORBIDDEN_SDKS}!r} )))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == []


def test_inference_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("wreath infra infer must not touch the network")

    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, refuse)

    app = build(main=DSN, analytics=REPLICA)
    app.jobs("ingest", database="main")
    app.messaging("events", database="main")
    app.http_client("forage", base_url="https://forage.example.com")
    app.objects(
        "cards",
        backend="s3",
        bucket="trek-cards",
        region="eu-west-2",
        access_key="AKIAEXAMPLE",
        secret_key="secret",
    )
    plan = plan_for(app, settings=[(Settings, "trek.config:Settings", "TREK")])
    assert render_text(plan)
    assert json.loads(render_json(plan))


def test_inference_reports_the_series_and_entity_tables_it_used_to_miss() -> None:
    app = build()
    app.jobs("ingest", database="main")
    app.messaging("events", database="main")
    app.series(database="main")
    app.entities("things", database="main", bus="events")

    plan = plan_for(app)
    (database,) = plan.databases
    inferred = {component.name for component in database.components}
    assert inferred == {"jobs", "messaging", "series", "entity"}


def test_a_series_store_is_attributed_to_the_database_it_settles_on() -> None:
    app = build(main=DSN, analytics=REPLICA)
    app.series(database="analytics")

    plan = plan_for(app)
    by_name = {database.name: database for database in plan.databases}
    assert [c.name for c in by_name["analytics"].components] == ["series"]
    assert by_name["main"].components == ()
    (component,) = by_name["analytics"].components
    assert component.declared_by == "app.series(database='analytics')"
