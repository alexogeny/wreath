from __future__ import annotations

from typing import Any

from wreath.app import Wreath
from wreath.http_client import DestinationPolicy
from wreath.orm import Model, column
from wreath.orm.types import Int64, Text, Vector


class Sighting(Model, table="sightings", schema="trek"):
    """One ORM model, carrying an extension type so the plan has one to report."""

    id: int = column(Int64, primary_key=True)
    label: str = column(Text)
    embedding: Any = column(Vector(3))


def bare() -> Wreath:
    """No database, no store, no client, no route. Every section is empty."""
    return Wreath()


def rich(root: str) -> Wreath:
    """Every surface stage 1 derives, including the awkward spelling of each.

    Deliberately awkward, because the ordinary spellings are the ones already
    covered: a database whose reads go to a replica beside one with no user and
    no port in its DSN, a path-style S3 endpoint beside a local root, and three
    outbound clients that between them cover a default http port, a default
    https port, and neither.

    `root` is a parameter because `LocalObjectStore` creates its directory at
    registration: a hard-coded path would either need to exist on every machine
    or make this fixture the reason a test suite writes outside its own tree.
    """
    app = Wreath()
    app.postgres(
        "main",
        dsn="postgresql://trek@db.internal:5432/trek",
        workload_dsns={"read": "postgresql://trek@replica.internal:5432/trek"},
    )
    app.postgres("archive", dsn="postgresql://warehouse.internal/archive")
    app.orm(database="main", models=[Sighting], validate_schema="off")
    app.jobs("ingest", database="main")
    app.messaging("events", database="main")
    app.objects("scratch", backend="local", root=root)
    app.objects(
        "cards",
        backend="s3",
        bucket="trek-cards",
        region="eu-west-2",
        endpoint="minio.internal:9000",
        scheme="http",
        access_key="AKIAEXAMPLE",
        secret_key="a-secret-that-must-never-be-rendered",
    )
    app.http_client("forage", base_url="https://forage.example.com/v2")
    app.http_client("legacy", base_url="http://legacy.internal")
    app.http_client(
        "metrics",
        base_url="http://metrics.internal:8086",
        destination=DestinationPolicy(
            hosts=("metrics.internal",), ports=frozenset({8086}), allow_private=True
        ),
    )

    @app.get("/sightings")
    async def sightings(request: object) -> dict[str, int]:
        return {"count": 0}

    @app.post("/sightings")
    async def record(request: object) -> dict[str, int]:
        return {"count": 1}

    return app
