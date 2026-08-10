"""First-party migration contracts exercised by the camera-trap station."""

from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def station_root() -> Path:
    return Path(__file__).parent / "corpus" / "camera_trap_station"


def _rules(station_root: Path, name: str) -> list[str]:
    report = port.analyze(station_root)
    return [
        finding.rule_id
        for finding in report.findings
        if finding.file == name
    ]


def _ported(station_root: Path, tmp_path: Path) -> dict[str, str]:
    output = tmp_path / "ported"
    port.port_tree(station_root, output, opinionated=True)
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in output.rglob("*.py")
    }


def test_manager_ownership_and_manager_patches_name_the_session_seam(
    station_root: Path,
) -> None:
    assert "orm.manager_value" in _rules(station_root, "repository.py")
    assert "orm.manager_patch" in _rules(station_root, "test_repository.py")


def test_relation_free_broad_load_is_exact_but_a_graph_load_is_not(
    station_root: Path,
) -> None:
    rules = _rules(station_root, "repository.py")
    assert "orm.query.select_all_exact" in rules
    assert "orm.query.select_all" in rules


def test_get_or_create_expands_onto_existing_session_primitives(
    station_root: Path, tmp_path: Path
) -> None:
    source = _ported(station_root, tmp_path)["repository.py"]
    assert "await session.fetch_one(SurveyRole.select().where(" in source
    assert "SurveyRole.name == name" in source
    assert "SurveyRole.description == description" in source
    assert "await session.create(SurveyRole, name=name, description=description)" in source
    assert "def _wreath_" not in source


def test_projection_and_redundant_literal_validator_use_dataclasses(
    station_root: Path, tmp_path: Path
) -> None:
    rules = _rules(station_root, "dtos.py")
    assert "pydantic.get_pydantic_exact" in rules
    assert "pydantic.validator_literal" in rules
    assert rules.count("pydantic.field_metadata_exact") == 2
    source = _ported(station_root, tmp_path)["dtos.py"]
    assert "model_dataclass(Camera" in source
    assert "field_validator" not in source
    assert "def distance_unit" not in source
    assert "Annotated[int, Field(ge=0, le=100, description=" in source
    assert 'Annotated[str, Field(alias="cameraLabel", max_length=80)]' in source
    assert "confidence: " in source and " = 0" in source


def test_an_ordered_first_query_is_fully_emitted(
    station_root: Path, tmp_path: Path
) -> None:
    assert "orm.query.filter_exact" in _rules(station_root, "repository.py")
    source = _ported(station_root, tmp_path)["repository.py"]
    assert ".order_by(StationReading.id.desc()).limit(1)" in source
    assert "await session.fetch_one(" in source


def test_nested_eager_list_and_terminal_get_use_one_wreath_query(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ridge"
    root.mkdir()
    (root / "models.py").write_text(
        "import ormar\n"
        "class Trail(ormar.Model):\n"
        "    ormar_config = db.copy(tablename='trail')\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "class Station(ormar.Model):\n"
        "    ormar_config = db.copy(tablename='station')\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    trail: Trail = ormar.ForeignKey(Trail)\n"
        "class Camera(ormar.Model):\n"
        "    ormar_config = db.copy(tablename='camera')\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    serial: str = ormar.String(max_length=80)\n"
        "    station: Station = ormar.ForeignKey(Station)\n",
        encoding="utf-8",
    )
    (root / "queries.py").write_text(
        "async def camera(serial):\n"
        "    return await (\n"
        "        Camera.objects.filter(serial=serial)\n"
        "        .select_related(['station', 'station__trail'])\n"
        "        .get()\n"
        "    )\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    emitted = (output / "queries.py").read_text(encoding="utf-8")
    assert "async def camera(serial, *, session: Session):" in emitted
    assert "Camera.station.selectin()" in emitted
    assert "Camera.station.selectin(Station.trail.selectin())" in emitted
    assert "await session.require_one(Camera.select()" in emitted
    assert ".objects" not in emitted
    compile(emitted, str(output / "queries.py"), "exec")


def test_literal_values_projection_preserves_dictionary_keys(
    tmp_path: Path,
) -> None:
    root = tmp_path / "llama_census"
    root.mkdir()
    (root / "models.py").write_text(
        "import ormar\n"
        "class Trek(ormar.Model):\n"
        "    ormar_config = db.copy(tablename='trek')\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "class Llama(ormar.Model):\n"
        "    ormar_config = db.copy(tablename='llama')\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    trek: Trek = ormar.ForeignKey(Trek)\n",
        encoding="utf-8",
    )
    (root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/llamas')\n"
        "async def llama_rows():\n"
        "    direct = await Llama.objects.values(['id', 'trek'])\n"
        "    filtered = await Llama.objects.filter(id=7).values(['id'])\n"
        "    narrowed = await Llama.objects.filter(id=8).fields(['trek']).values()\n"
        "    tuples = await Llama.objects.filter(id=9).values_list('id')\n"
        "    return direct, filtered, narrowed, tuples\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    emitted = (output / "routes.py").read_text(encoding="utf-8")
    assert "Llama.select(Llama.id, Llama.trek_id)" in emitted
    assert "Llama.select(Llama.id).where(Llama.id == 7)" in emitted
    assert "Llama.select(Llama.trek_id).where(Llama.id == 8)" in emitted
    assert "Llama.select(Llama.id).where(Llama.id == 9)" in emitted
    assert "[(_row_4.id,) for _row_4 in await session.fetch(" in emitted
    assert "{'id': _row.id, 'trek': _row.trek_id}" in emitted
    assert "for _row in await session.fetch(" in emitted
    assert ".objects" not in emitted
    compile(emitted, str(output / "routes.py"), "exec")


@pytest.mark.parametrize(
    "tail",
    [
        ".fields(['id']).values(names)",
        ".fields(['id']).values",
        ".fields(['id']).values(extra=names)",
        ".values()",
    ],
)
def test_unresolved_projection_after_fields_stays_visible(
    tmp_path: Path, tail: str
) -> None:
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/llamas')\n"
        "async def llama_rows(names):\n"
        f"    return Llama.objects.filter(id=7){tail}\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert ".objects.filter(id=7)" in emitted
    assert "[orm.query.filter]" in emitted


def test_explicit_model_order_expression_stays_explicit(tmp_path: Path) -> None:
    root = tmp_path / "camera_readings"
    root.mkdir()
    (root / "models.py").write_text(
        "import ormar\n"
        "class CameraReading(ormar.Model):\n"
        "    ormar_config = db.copy(tablename='camera_reading')\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    ridge: str = ormar.String(max_length=80)\n",
        encoding="utf-8",
    )
    (root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/latest')\n"
        "async def latest(ridge):\n"
        "    return await CameraReading.objects.filter(ridge=ridge)"
        ".order_by(CameraReading.id.desc()).first()\n"
        "@router.get('/page')\n"
        "async def page(ridge):\n"
        "    return await CameraReading.objects.filter(ridge=ridge)"
        ".paginate(page=2, page_size=5).all()\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    emitted = (output / "routes.py").read_text(encoding="utf-8")
    assert ".order_by(CameraReading.id.desc()).limit(1)" in emitted
    assert ".limit(5).offset((2 - 1) * 5)" in emitted
    assert "await session.fetch_one(" in emitted
    assert ".objects" not in emitted
    compile(emitted, str(output / "routes.py"), "exec")


def test_outbound_http_and_manual_oidc_name_managed_first_party_targets(
    station_root: Path, tmp_path: Path,
) -> None:
    assert "ext.httpx" in _rules(station_root, "outbound.py")
    outbound = _ported(station_root, tmp_path)["outbound.py"]
    assert "HTTPClient('client', base_url=station_url" in outbound
    assert "ClientTimeout(total=10.0)" in outbound
    assert 'await client.get("/forecast", headers=' in outbound
    assert "loads(response.body)" in outbound
    assert "httpx.AsyncClient" not in outbound
    assert "import httpx" not in outbound
    assert "[ext.httpx]" not in outbound
    auth_rules = _rules(station_root, "auth.py")
    assert "auth.oidc_manual" in auth_rules
    assert "auth.jwt" not in auth_rules


def test_one_shot_dynamic_http_origin_is_split_without_an_httpx_adapter(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ridge.py"
    source.write_text(
        "import httpx\n"
        "async def reading(url, payload):\n"
        "    async with httpx.AsyncClient() as ridge:\n"
        "        response = await ridge.post(url, json=payload)\n"
        "        return response.status_code, response.json()\n",
        encoding="utf-8",
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "import httpx" not in emitted
    assert "_ridge_url = urlsplit(str(url))" in emitted
    assert "base_url=urlunsplit((_ridge_url.scheme, _ridge_url.netloc" in emitted
    assert "ridge.post(urlunsplit(('', '', _ridge_url.path or '/'" in emitted
    assert "body=dumps(payload)" in emitted
    assert "return response.status, loads(response.body)" in emitted


def test_process_task_and_joined_fanout_are_distinguished(station_root: Path) -> None:
    rules = _rules(station_root, "background.py")
    assert rules.count("bg.asyncio_loop") == 1
    assert rules.count("bg.asyncio_joined") == 2


def test_external_scheduler_points_at_durable_jobs(station_root: Path) -> None:
    assert "ext.boto3_scheduler" in _rules(station_root, "scheduler.py")


def test_http_controls_drop_the_wildcard_host_noop(
    station_root: Path, tmp_path: Path
) -> None:
    rules = _rules(station_root, "middleware.py")
    assert "mw.cors" in rules
    assert "mw.trustedhost_noop" in rules
    source = _ported(station_root, tmp_path)["middleware.py"]
    assert "CorsPolicy" in source
    assert "TrustedHost" not in source
    assert "allowed_hosts" not in source


def test_state_middleware_names_explicit_state_ownership(station_root: Path) -> None:
    assert "mw.state" in _rules(station_root, "custom_middleware.py")


def test_authentication_override_points_at_test_client_identity(
    station_root: Path,
) -> None:
    rules = _rules(station_root, "test_api.py")
    assert "test.dependency_override_auth" in rules
    assert "test.client_local" in rules


@pytest.mark.parametrize(
    "client",
    ["cloudwatch", "logs"],
)
def test_observability_clients_are_not_generic_aws_findings(
    tmp_path: Path, client: str
) -> None:
    source = tmp_path / "telemetry.py"
    source.write_text(
        f'import boto3\nclient = boto3.client("{client}")\n',
        encoding="utf-8",
    )
    assert [finding.rule_id for finding in port.analyze(source).findings] == [
        "ext.boto3_observability"
    ]


@pytest.mark.parametrize("client", ["cognito-idp", "cognito-identity"])
def test_identity_clients_are_not_generic_aws_findings(
    tmp_path: Path, client: str
) -> None:
    source = tmp_path / "identity.py"
    source.write_text(
        f'import boto3\nclient = boto3.client("{client}")\n',
        encoding="utf-8",
    )
    assert [finding.rule_id for finding in port.analyze(source).findings] == [
        "ext.boto3_identity"
    ]


def test_plain_jwt_decode_stays_distinct_from_oidc(tmp_path: Path) -> None:
    source = tmp_path / "token.py"
    source.write_text(
        "from jose import jwt\nclaims = jwt.decode(token, key, algorithms=['HS256'])\n",
        encoding="utf-8",
    )
    assert "auth.jwt" in [finding.rule_id for finding in port.analyze(source).findings]


def test_named_and_starred_task_joins_observe_the_created_task(tmp_path: Path) -> None:
    source = tmp_path / "fanout.py"
    source.write_text(
        "import asyncio\n"
        "async def one():\n"
        "    task = asyncio.create_task(load())\n"
        "    await asyncio.gather(task)\n"
        "async def many(rows):\n"
        "    tasks = [asyncio.create_task(load(row)) for row in rows]\n"
        "    await asyncio.gather(*tasks)\n",
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(source).findings]
    assert rules == ["bg.asyncio_joined", "bg.asyncio_joined"]


def test_a_different_or_computed_join_does_not_claim_task_ownership(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fanout.py"
    source.write_text(
        "import asyncio\n"
        "async def one(other):\n"
        "    task = asyncio.create_task(load())\n"
        "    await asyncio.gather(other)\n"
        "async def many(rows):\n"
        "    tasks = [asyncio.create_task(load(row)) for row in rows]\n"
        "    await asyncio.gather(*(tasks + []))\n",
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(source).findings]
    assert rules == ["bg.asyncio_loop"]


def test_adapter_override_is_not_mistaken_for_identity(tmp_path: Path) -> None:
    source = tmp_path / "test_station.py"
    source.write_text(
        "app.dependency_overrides[weather_service] = lambda: forecast\n",
        encoding="utf-8",
    )
    assert [finding.rule_id for finding in port.analyze(source).findings] == [
        "test.dependency_override_adapter"
    ]


def test_attribute_named_identity_override_uses_test_client_identity(tmp_path: Path) -> None:
    source = tmp_path / "test_station.py"
    source.write_text(
        "app.dependency_overrides[dependencies.current_ranger] = lambda: ranger\n",
        encoding="utf-8",
    )
    assert [finding.rule_id for finding in port.analyze(source).findings] == [
        "test.dependency_override_auth"
    ]


@pytest.mark.parametrize(
    "allowed_hosts",
    ["['camera.example']", "['*', 'camera.example']", "hosts", "[]"],
)
def test_only_the_single_literal_wildcard_host_policy_is_a_noop(
    tmp_path: Path, allowed_hosts: str
) -> None:
    source = tmp_path / "hosts.py"
    source.write_text(
        "from fastapi import FastAPI\n"
        "from starlette.middleware.trustedhost import TrustedHostMiddleware\n"
        "app = FastAPI()\n"
        f"app.add_middleware(TrustedHostMiddleware, allowed_hosts={allowed_hosts})\n",
        encoding="utf-8",
    )
    assert "mw.trustedhost" in [
        finding.rule_id for finding in port.analyze(source).findings
    ]
    emitted = port.emit_module(source)
    assert "TrustedHostPolicy" in emitted
    assert f"allowed_hosts={allowed_hosts}" in emitted


@pytest.mark.parametrize(
    ("class_name", "assignment", "expected"),
    [
        ("StationExceptionMiddleware", "", "mw.exception"),
        ("StationErrorMiddleware", "", "mw.exception"),
        ("TrailContextMiddleware", "request.state.trail = trail", "mw.state"),
        ("TrailMiddleware", "", "mw.custom"),
    ],
)
def test_custom_middleware_is_split_by_its_actual_job(
    tmp_path: Path, class_name: str, assignment: str, expected: str
) -> None:
    line = f"        {assignment}\n" if assignment else ""
    source = tmp_path / "layer.py"
    source.write_text(
        "from starlette.middleware.base import BaseHTTPMiddleware\n"
        f"class {class_name}(BaseHTTPMiddleware):\n"
        "    async def dispatch(self, request, call_next):\n"
        + line
        + "        return await call_next(request)\n",
        encoding="utf-8",
    )
    assert expected in [finding.rule_id for finding in port.analyze(source).findings]


@pytest.mark.parametrize(
    "validator",
    [
        (
            "    @field_validator('distance', 'height')\n"
            "    @classmethod\n    def check(cls, value):\n        return value\n"
        ),
        (
            "    @field_validator('distance', mode='before')\n"
            "    @classmethod\n    def check(cls, value):\n        return value\n"
        ),
        (
            "    @field_validator(field_name)\n"
            "    @classmethod\n    def check(cls, value):\n        return value\n"
        ),
        (
            "    @field_validator('missing')\n"
            "    @classmethod\n    def check(cls, value):\n        return value\n"
        ),
        (
            "    @field_validator('distance')\n    @classmethod\n"
            "    def check(cls, value):\n"
            "        if value in {'m', 'km'}:\n            return value\n"
            "        raise ValueError\n"
        ),
        (
            "    @field_validator('distance')\n    @classmethod\n"
            "    def check(cls, value):\n"
            "        if value not in {'m'}:\n            raise ValueError\n"
            "        return value\n"
        ),
        (
            "    @field_validator('distance')\n    @classmethod\n"
            "    def check(cls, value):\n"
            "        if value not in {'m', 'km'}:\n            raise ValueError\n"
            "        return other\n"
        ),
    ],
)
def test_only_a_literal_membership_restatement_is_deleted(
    tmp_path: Path, validator: str
) -> None:
    source = tmp_path / "units.py"
    source.write_text(
        "from typing import Literal\n"
        "from pydantic import BaseModel, field_validator\n"
        "class Units(BaseModel):\n"
        "    distance: Literal['m', 'km'] = 'm'\n"
        + validator,
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(source).findings]
    assert "pydantic.validator" in rules
    assert "pydantic.validator_literal" not in rules


def test_single_member_literal_restatement_is_deleted(tmp_path: Path) -> None:
    source = tmp_path / "units.py"
    source.write_text(
        "from typing import Literal\n"
        "from pydantic import BaseModel, field_validator\n"
        "class Units(BaseModel):\n"
        "    distance: Literal['m'] = 'm'\n"
        "    @field_validator('distance')\n"
        "    @classmethod\n"
        "    def check(cls, value):\n"
        "        if value not in {'m'}:\n"
        "            raise ValueError\n"
        "        return value\n",
        encoding="utf-8",
    )
    assert "pydantic.validator_literal" in [
        finding.rule_id for finding in port.analyze(source).findings
    ]


def test_a_docstring_does_not_hide_a_literal_restatement(tmp_path: Path) -> None:
    source = tmp_path / "units.py"
    source.write_text(
        "from typing import Literal\n"
        "from pydantic import BaseModel, field_validator\n"
        "class Units(BaseModel):\n"
        "    distance: Literal['m', 'km'] = 'm'\n"
        "    @field_validator('distance')\n"
        "    @classmethod\n"
        "    def check(cls, value):\n"
        "        '''Keep the wire units narrow.'''\n"
        "        if value not in {'m', 'km'}:\n"
        "            raise ValueError\n"
        "        return value\n",
        encoding="utf-8",
    )
    emitted = port.emit_module(source)
    assert "def check" not in emitted
    assert "field_validator" not in emitted


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("Field(default=1, multiple_of=2)", "pydantic.field_constraint"),
        (
            "Field(default=1, max_digits=4, decimal_places=2)",
            "pydantic.field_marker",
        ),
    ],
)
def test_only_field_metadata_with_an_equivalent_is_translated(
    tmp_path: Path, marker: str, expected: str
) -> None:
    source = tmp_path / "reading.py"
    source.write_text(
        "from pydantic import BaseModel, Field\n"
        "class Reading(BaseModel):\n"
        f"    value: int = {marker}\n",
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(source).findings]
    assert expected in rules
    assert "pydantic.field_metadata_exact" not in rules


def test_pydantic_v1_regex_uses_wreath_pattern_metadata(tmp_path: Path) -> None:
    source = tmp_path / "camera.py"
    source.write_text(
        "from pydantic import BaseModel, Field\n"
        "class Camera(BaseModel):\n"
        "    label: str = Field(regex='^[A-Z]+$', alias='cameraLabel')\n",
        encoding="utf-8",
    )
    emitted = port.emit_module(source)
    assert "Field(pattern='^[A-Z]+$', alias='cameraLabel')" in emitted
    assert "from pydantic" not in emitted


@pytest.mark.parametrize(
    "declaration",
    [
        "value: int = Field(0, 1, ge=0)",
        "value: int = Field(default=0, ge=0, **options)",
        "value: int = Field(default=0, ge=0, strict=True)",
    ],
)
def test_ambiguous_field_calls_keep_their_review(
    tmp_path: Path, declaration: str
) -> None:
    source = tmp_path / "reading.py"
    source.write_text(
        "from pydantic import BaseModel, Field\n"
        "class Reading(BaseModel):\n"
        f"    {declaration}\n",
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(source).findings]
    assert "pydantic.field_metadata_exact" not in rules


def test_a_non_pydantic_field_factory_is_not_claimed(tmp_path: Path) -> None:
    source = tmp_path / "reading.py"
    source.write_text(
        "from pydantic import BaseModel\n"
        "from camera_fields import Field\n"
        "class Reading(BaseModel):\n"
        "    value: int = Field(default=0, ge=0)\n",
        encoding="utf-8",
    )
    assert "pydantic.field_metadata_exact" not in [
        finding.rule_id for finding in port.analyze(source).findings
    ]


def test_order_remains_visible_through_an_intermediate_limit(tmp_path: Path) -> None:
    source = tmp_path / "reading.py"
    source.write_text(
        "from wreath.orm import Session\n"
        "async def latest(session: Session):\n"
        "    return await Reading.objects.filter(active=True).order_by('-id').limit(2).first()\n",
        encoding="utf-8",
    )
    assert "orm.query.filter_exact" in [
        finding.rule_id for finding in port.analyze(source).findings
    ]
    emitted = port.emit_module(source)
    assert ".order_by(Reading.id.desc()).limit(1)" in emitted


def test_protocol_and_implementation_signatures_move_with_their_callers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "station"
    root.mkdir()
    (root / "repository.py").write_text(
        "from abc import ABC, abstractmethod\n"
        "class ReadingPort(ABC):\n"
        "    @abstractmethod\n"
        "    async def latest_reading(self):\n"
        "        pass\n"
        "class ReadingRepository(ReadingPort):\n"
        "    async def latest_reading(self):\n"
        "        return await Reading.objects.order_by('-id').first()\n",
        encoding="utf-8",
    )
    (root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from repository import ReadingRepository\n"
        "router = APIRouter()\n"
        "@router.get('/latest')\n"
        "async def latest():\n"
        "    return await ReadingRepository().latest_reading()\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    repository = (output / "repository.py").read_text(encoding="utf-8")
    routes = (output / "routes.py").read_text(encoding="utf-8")
    assert repository.count("latest_reading(self, *, session: Session)") == 2
    assert "await session.fetch_one(Reading.select().order_by(" in repository
    assert "Reading.id.desc()).limit(1)" in repository
    assert "latest_reading(session=session)" in routes
    assert "orm.query.session_added" not in repository


def test_same_named_unrelated_method_does_not_gain_a_session(tmp_path: Path) -> None:
    root = tmp_path / "station"
    root.mkdir()
    (root / "repository.py").write_text(
        "class CameraRepository:\n"
        "    async def locate(self):\n"
        "        return await Camera.objects.get_or_none(active=True)\n"
        "class RidgeMap:\n"
        "    async def locate(self):\n"
        "        return 'north'\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    emitted = (output / "repository.py").read_text(encoding="utf-8")
    assert emitted.count("async def locate(self):") == 1
    assert emitted.count("async def locate(self, *, session: Session)") == 1
    assert "await session.fetch_one(Camera.select()" in emitted
    assert "return 'north'" in emitted


def test_query_create_is_rewritten_before_local_session_calls_are_threaded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "station"
    root.mkdir()
    (root / "archive.py").write_text(
        "class ReadingArchive:\n"
        "    async def create(self, camera_id):\n"
        "        return await CameraReading.objects.create(camera_id=camera_id)\n"
        "async def record(camera_id):\n"
        "    return await CameraReading.objects.create(camera_id=camera_id)\n",
        encoding="utf-8",
    )
    (root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from archive import ReadingArchive, record\n"
        "router = APIRouter()\n"
        "@router.post('/readings')\n"
        "async def add_reading(camera_id: int):\n"
        "    await ReadingArchive().create(camera_id)\n"
        "    return await record(camera_id)\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    archive = (output / "archive.py").read_text(encoding="utf-8")
    routes = (output / "routes.py").read_text(encoding="utf-8")
    assert archive.count("await session.create(CameraReading, camera_id=camera_id)") == 2
    assert ".objects.create" not in archive
    assert "create(camera_id, session=session)" in routes
    assert "record(camera_id, session=session)" in routes


def test_legacy_model_mixins_jsonb_and_meta_indexes_move_together(
    tmp_path: Path,
) -> None:
    root = tmp_path / "station"
    root.mkdir()
    (root / "shared.py").write_text(
        "import ormar\n"
        "class CameraIdentity:\n"
        "    id: int = ormar.Integer(primary_key=True)\n",
        encoding="utf-8",
    )
    (root / "models.py").write_text(
        "import ormar\n"
        "from shared import CameraIdentity\n"
        "class Reading(ormar.Model, CameraIdentity):\n"
        "    class Meta:\n"
        "        tablename = 'camera_reading'\n"
        "        indexes = ['camera_id', ('camera_id', 'observed_at')]\n"
        "        constraints = [ormar.UniqueColumns('camera_id', 'observed_at')]\n"
        "    camera_id: int = ormar.Integer(nullable=False)\n"
        "    payload: dict = ormar.JSONB(nullable=False)\n"
        "    observed_at: str = ormar.String(max_length=40)\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    shared = (output / "shared.py").read_text(encoding="utf-8")
    models = (output / "models.py").read_text(encoding="utf-8")
    assert "class CameraIdentity(Model):" in shared
    assert 'class Reading(CameraIdentity, table="camera_reading"):' in models
    assert "index('camera_id')" in models
    assert "index('camera_id', 'observed_at')" in models
    assert "unique('camera_id', 'observed_at')" in models
    assert "payload: Mapped[dict] = column(Jsonb, nullable=False)" in models
    assert "ormar.JSONB" not in models


def test_settings_become_environment_bound_nested_dataclasses(tmp_path: Path) -> None:
    source = tmp_path / "settings.py"
    source.write_text(
        "from pydantic import Field\n"
        "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
        "class RidgeSettings(BaseSettings):\n"
        "    api_key: str = Field(alias='RIDGE_CAMERA_KEY')\n"
        "class StationSettings(BaseSettings):\n"
        "    model_config = SettingsConfigDict(extra='ignore')\n"
        "    ridge: RidgeSettings = RidgeSettings()\n"
        "    labels: list[str] = []\n"
        "settings = StationSettings()\n",
        encoding="utf-8",
    )
    emitted = port.emit_module(
        source,
        port.TreeContext(
            index={
                "pydantic": set(),
                "settings": {"RidgeSettings", "StationSettings"},
                "orm": set(),
                "orm_mixin": set(),
            }
        ),
        opinionated=True,
    )
    assert emitted.count("@dataclass(kw_only=True)") == 2
    assert "BaseSettings" not in emitted
    assert "SettingsConfigDict" not in emitted
    assert "Annotated[str, Env('RIDGE_CAMERA_KEY')]" in emitted
    assert "field(default_factory=RidgeSettings)" in emitted
    assert "field(default_factory=list)" in emitted
    assert "Environment(read_osenv()).bind(StationSettings)" in emitted


def test_dotenv_prefix_and_custom_settings_initializers_are_preserved(
    tmp_path: Path,
) -> None:
    source = tmp_path / "settings.py"
    source.write_text(
        "import os\n"
        "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
        "class TrailSettings(BaseSettings):\n"
        "    model_config = SettingsConfigDict(env_file='.trail.env', env_prefix='CAM_')\n"
        "    host: str\n"
        "class CheckedSettings(BaseSettings):\n"
        "    enabled: bool = True\n"
        "    def __init__(self):\n"
        "        if not os.getenv('CAMERA_READY'):\n"
        "            raise ValueError('camera is not ready')\n"
        "        super().__init__()\n"
        "class ConnectionSettings(BaseSettings):\n"
        "    host: str\n"
        "    secure: bool = False\n"
        "    labels: dict[str, str] = {}\n"
        "    def __init__(self, host: str, secure: bool = False):\n"
        "        super().__init__(host=host, secure=secure)\n"
        "trail = TrailSettings()\n"
        "checked = CheckedSettings()\n",
        encoding="utf-8",
    )
    context = port.TreeContext(
        index={
            "pydantic": set(),
            "settings": {"TrailSettings", "CheckedSettings", "ConnectionSettings"},
            "orm": set(),
            "orm_mixin": set(),
        }
    )
    emitted = port.emit_module(source, context, opinionated=True)
    assert "Environment.load('.trail.env').bind(TrailSettings, prefix='CAM_')" in emitted
    assert "def __post_init__(self):" in emitted
    assert "super().__init__" not in emitted
    assert "self.host = host" in emitted
    assert "self.secure = secure" in emitted
    assert "self.labels = {}" in emitted
    compile(emitted, str(tmp_path / "settings_ported.py"), "exec")


def test_custom_field_validators_move_to_dataclass_post_init(tmp_path: Path) -> None:
    source = tmp_path / "reading.py"
    source.write_text(
        "from pydantic import BaseModel, field_validator\n"
        "class Reading(BaseModel):\n"
        "    camera_label: str\n"
        "    @field_validator(\n"
        "        'camera_label',\n"
        "        mode='before',\n"
        "    )\n"
        "    @classmethod\n"
        "    def normalize_label(cls, value):\n"
        "        return value.strip().lower()\n",
        encoding="utf-8",
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "field_validator" not in emitted
    assert "@classmethod" not in emitted
    assert "def normalize_label(cls, value):" in emitted
    assert "def __post_init__(self) -> None:" in emitted
    assert "self.camera_label = self.normalize_label(self.camera_label)" in emitted
    assert "[pydantic.validator]" not in emitted


def test_directly_appended_tasks_are_joined(tmp_path: Path) -> None:
    source = tmp_path / "camera_tasks.py"
    source.write_text(
        "import asyncio\n"
        "async def load(cameras):\n"
        "    tasks = []\n"
        "    for camera in cameras:\n"
        "        tasks.append(asyncio.create_task(read(camera)))\n"
        "    await asyncio.gather(*tasks)\n",
        encoding="utf-8",
    )
    assert "bg.asyncio_joined" in [
        finding.rule_id for finding in port.analyze(source).findings
    ]


@pytest.mark.parametrize(
    "append",
    [
        "tasks.extend([asyncio.create_task(read(camera))])",
        "tasks.append(asyncio.create_task(read(camera)), camera)",
        "holder.tasks.append(asyncio.create_task(read(camera)))",
    ],
)
def test_only_a_plain_single_argument_append_is_followed(
    tmp_path: Path, append: str
) -> None:
    source = tmp_path / "camera_tasks.py"
    source.write_text(
        "import asyncio\n"
        "async def load(camera, holder):\n"
        "    tasks = []\n"
        f"    {append}\n"
        "    await asyncio.gather(*tasks)\n",
        encoding="utf-8",
    )
    assert "bg.asyncio_loop" in [
        finding.rule_id for finding in port.analyze(source).findings
    ]


def test_a_task_gathered_before_it_is_created_is_not_claimed(tmp_path: Path) -> None:
    source = tmp_path / "camera_tasks.py"
    source.write_text(
        "import asyncio\n"
        "async def load(cameras):\n"
        "    tasks = []\n"
        "    await asyncio.gather(*tasks)\n"
        "    tasks.append(asyncio.create_task(read(cameras)))\n",
        encoding="utf-8",
    )
    assert "bg.asyncio_loop" in [
        finding.rule_id for finding in port.analyze(source).findings
    ]


@pytest.mark.parametrize(
    ("annotation", "body"),
    [
        ("str", "if value not in {'m', 'km'}:\n            raise ValueError\n        return value"),
        (
            "Literal[UNIT]",
            "if value not in {'m', 'km'}:\n            raise ValueError\n        return value",
        ),
        (
            "Literal['m', 'km']",
            "value = value.strip()\n        if value not in {'m', 'km'}:\n"
            "            raise ValueError\n        return value",
        ),
        ("Literal['m', 'km']", "return value"),
        (
            "Literal['m', 'km']",
            "if value not in {'m', 'km'}:\n            raise ValueError\n        value",
        ),
        (
            "Literal['m', 'km']",
            "if self.value not in {'m', 'km'}:\n            raise ValueError\n        return value",
        ),
        (
            "Literal['m', 'km']",
            "if value not in {'m', 'km'} == allowed:\n"
            "            raise ValueError\n        return value",
        ),
        (
            "Literal['m', 'km']",
            "if value not in {'m': 1, 'km': 2}:\n"
            "            raise ValueError\n        return value",
        ),
        (
            "Literal['m', 'km']",
            "if value not in {'m', 'km'}:\n"
            "            raise ValueError\n            raise RuntimeError\n"
            "        return value",
        ),
        (
            "Literal['m', 'km']",
            "if value not in {'m', 'km'}:\n            log(value)\n        return value",
        ),
        (
            "Literal['m', 'km']",
            "if value not in {'m', 'km'}:\n            raise ValueError\n"
            "        else:\n            log(value)\n        return value",
        ),
        (
            "Literal['m', 'km']",
            "if value not in {METER, 'km'}:\n            raise ValueError\n        return value",
        ),
    ],
)
def test_other_validator_control_flow_is_preserved(
    tmp_path: Path, annotation: str, body: str
) -> None:
    source = tmp_path / "units.py"
    source.write_text(
        "from typing import Literal\n"
        "from pydantic import BaseModel, field_validator\n"
        "class Units(BaseModel):\n"
        f"    distance: {annotation} = 'm'\n"
        "    @field_validator('distance')\n"
        "    @classmethod\n"
        "    def check(cls, value):\n"
        f"        {body}\n",
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(source).findings]
    assert "pydantic.validator" in rules
    assert "pydantic.validator_literal" not in rules


def test_static_creation_fields_expand_without_a_generated_helper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "models.py").write_text(
        "import ormar\n"
        "class CameraAssignment(ormar.Model):\n"
        "    ormar_config = db.copy(\n"
        "        tablename='camera_assignment',\n"
        "        constraints=[ormar.UniqueColumns('camera', 'ranger')],\n"
        "    )\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    camera: int = ormar.Integer()\n"
        "    ranger: int = ormar.Integer()\n",
        encoding="utf-8",
    )
    (root / "queries.py").write_text(
        "async def assign(camera, ranger):\n"
        "    return await CameraAssignment.objects.get_or_create(\n"
        "        camera=camera, ranger=ranger\n"
        "    )\n",
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(root).findings]
    assert "orm.query.get_or_create_exact" in rules
    output = tmp_path / "out"
    port.port_tree(root, output, opinionated=True)
    emitted = (output / "queries.py").read_text(encoding="utf-8")
    assert "await session.fetch_one(CameraAssignment.select().where(" in emitted
    assert "await session.create(CameraAssignment, camera=camera, ranger=ranger)" in emitted
    assert "def _wreath_" not in emitted


@pytest.mark.parametrize(
    ("model", "query"),
    [
        (
            "class SurveyRole(ormar.Model):\n"
            "    ormar_config = db.copy(tablename='survey_role')\n"
            "    id: int = ormar.Integer(primary_key=True)\n"
            "    name: str = ormar.String(max_length=40, unique=True)\n",
            "SurveyRole.objects.get_or_create(name)",
        ),
        (
            "class SurveyRole(ormar.Model):\n"
            "    ormar_config = db.copy(tablename='survey_role')\n"
            "    id: int = ormar.Integer(primary_key=True)\n"
            "    name: str = ormar.String(max_length=40, unique=True)\n",
            "SurveyRole.objects.get_or_create()",
        ),
        (
            "class SurveyRole(ormar.Model):\n"
            "    ormar_config = db.copy(tablename='survey_role')\n"
            "    id: int = ormar.Integer(primary_key=True)\n"
            "    name: str = ormar.String(max_length=40, unique=True)\n",
            "SurveyRole.objects.get_or_create(name=name, _defaults=defaults)",
        ),
        (
            "class SurveyRole(ormar.Model):\n"
            "    ormar_config = db.copy(tablename='survey_role')\n"
            "    id: int = ormar.Integer(primary_key=True)\n"
            "    name: str = ormar.String(max_length=40, unique=True)\n",
            "SurveyRole.objects.get_or_create(name=name, defaults=defaults)",
        ),
        (
            "class SurveyRole(ormar.Model):\n"
            "    ormar_config = db.copy(tablename='survey_role')\n"
            "    id: int = ormar.Integer(primary_key=True)\n"
            "    name: str = ormar.String(max_length=40, unique=True)\n",
            "SurveyRole.objects.get_or_create(**defaults)",
        ),
    ],
)
def test_creation_expansion_requires_static_field_values(
    tmp_path: Path, model: str, query: str
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "models.py").write_text("import ormar\n" + model, encoding="utf-8")
    (root / "queries.py").write_text(
        f"async def role(name, defaults=None):\n    return await {query}\n",
        encoding="utf-8",
    )
    rules = [finding.rule_id for finding in port.analyze(root).findings]
    assert "orm.query.get_or_create" in rules
    assert "orm.query.get_or_create_exact" not in rules


def test_module_test_client_becomes_a_lifespan_fixture(tmp_path: Path) -> None:
    source = (
        "from fastapi.testclient import TestClient\n"
        "client = TestClient(app)\n"
        "def test_camera_health():\n"
        "    response = client.get('/health')\n"
        "    assert response.status_code == 200\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "@fixture\nasync def client():" in emitted
    assert "async with TestClient(app) as client:" in emitted
    assert "async def test_camera_health(client):" in emitted
    assert "response = await client.get('/health')" in emitted
    assert "response.status == 200" in emitted
    compile(emitted, str(tmp_path / "test_health.py"), "exec")


def test_yielded_test_client_fixture_owns_its_lifespan(tmp_path: Path) -> None:
    source = (
        "import pytest\n"
        "from starlette.testclient import TestClient\n"
        "@pytest.fixture\n"
        "def ridge_client():\n"
        "    yield TestClient(app)\n"
        "def test_ridge(ridge_client):\n"
        "    assert ridge_client.get('/ridge').status_code == 204\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "async def ridge_client():" in emitted
    assert "async with TestClient(app) as ridge_client:" in emitted
    assert "yield ridge_client" in emitted
    assert "async def test_ridge(ridge_client):" in emitted
    assert "(await ridge_client.get('/ridge')).status == 204" in emitted
    compile(emitted, str(tmp_path / "test_ridge.py"), "exec")


def test_scoped_test_client_context_becomes_async(tmp_path: Path) -> None:
    source = (
        "from fastapi.testclient import TestClient\n"
        "def test_station_startup():\n"
        "    with TestClient(app) as client:\n"
        "        assert client.get('/ready').status_code == 200\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "async def test_station_startup():" in emitted
    assert "async with TestClient(app) as client:" in emitted
    assert "(await client.get('/ready')).status == 200" in emitted
    compile(emitted, str(tmp_path / "test_startup.py"), "exec")


def test_partial_model_families_stay_visible_without_a_generated_helper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "station"
    root.mkdir()
    (root / "models.py").write_text(
        "from typing import Generic, TypeVar\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "from pydantic_partial import PartialModelMixin\n"
        "T = TypeVar('T')\n"
        "class CameraBase(PartialModelMixin, BaseModel):\n"
        "    label: str\n"
        "class CameraCreate(CameraBase):\n"
        "    ridge: str\n"
        "class CameraPatch(CameraBase.model_as_partial()):\n"
        "    model_config = ConfigDict(use_enum_values=True)\n"
        "    enabled: bool | None = None\n"
        "class Envelope(BaseModel, Generic[T]):\n"
        "    item: T\n",
        encoding="utf-8",
    )
    output = tmp_path / "ported"
    port.port_tree(root, output, opinionated=True)
    emitted = (output / "models.py").read_text(encoding="utf-8")
    assert "class CameraBase(PartialModelMixin, BaseModel):" in emitted
    assert "class CameraCreate(CameraBase):" in emitted
    assert "class CameraPatch(CameraBase.model_as_partial()):" in emitted
    assert "class Envelope(Generic[T]):" in emitted
    assert "[pydantic.partial]" in emitted
    assert "def _wreath_" not in emitted
    compile(emitted, str(output / "models.py"), "exec")


def test_asgi_transport_fixture_becomes_the_first_party_test_client(
    tmp_path: Path,
) -> None:
    source = (
        "import pytest\n"
        "import httpx\n"
        "@pytest.fixture\n"
        "async def camera_client():\n"
        "    yield httpx.AsyncClient(\n"
        "        transport=httpx.ASGITransport(app=app),\n"
        "        base_url='http://station',\n"
        "    )\n"
        "async def test_camera(camera_client):\n"
        "    assert (await camera_client.get('/camera')).status_code == 200\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "async with TestClient(app) as camera_client:" in emitted
    assert "yield camera_client" in emitted
    assert "httpx" not in emitted
    assert "AsyncClient" not in emitted
    assert "ASGITransport" not in emitted
    assert "(await camera_client.get('/camera')).status == 200" in emitted
    compile(emitted, str(tmp_path / "test_camera.py"), "exec")


def test_route_background_tasks_attach_to_every_response(tmp_path: Path) -> None:
    source = (
        "from fastapi import APIRouter, BackgroundTasks, Response\n"
        "router = APIRouter()\n"
        "@router.post('/camera/{camera_id}')\n"
        "async def wake_camera(camera_id: str, tasks: BackgroundTasks):\n"
        "    if not camera_id:\n"
        "        return Response(status_code=404)\n"
        "    tasks.add_task(wake, camera_id)\n"
        "    return Response(status_code=202)\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "tasks: BackgroundTasks" not in emitted
    assert "tasks = BackgroundTasks()" in emitted
    assert emitted.count("Response(status=") == 2
    assert emitted.count("background=tasks") == 2
    assert "def _wreath_" not in emitted
    assert "from fastapi" not in emitted
    compile(emitted, str(tmp_path / "background.py"), "exec")


def test_fixed_window_middleware_registration_becomes_http_policy(
    tmp_path: Path,
) -> None:
    source = (
        "from middleware.ratelimiter import InMemoryLimitProvider, RateLimitingMiddleware\n"
        "def configure(app):\n"
        "    app.add_middleware(\n"
        "        RateLimitingMiddleware,\n"
        "        included_routes=['/camera'],\n"
        "        provider=InMemoryLimitProvider(\n"
        "            block_duration=None, timespan=60, limit=20\n"
        "        ),\n"
        "    )\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "RateLimitingMiddleware" not in emitted
    assert "InMemoryLimitProvider" not in emitted
    assert "rate_limit=RateLimitPolicy(limit=20, window=60" in emitted
    assert "request.path.startswith(prefix)" in emitted
    assert "[mw.custom]" not in emitted
    compile(emitted, str(tmp_path / "policy.py"), "exec")


def test_route_fire_and_forget_work_moves_onto_the_response(tmp_path: Path) -> None:
    source = (
        "import asyncio\n"
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.post('/camera')\n"
        "async def record_camera():\n"
        "    asyncio.create_task(store_frame('ridge'), name='frame')\n"
        "    return {'accepted': True}\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "_wreath_background = BackgroundTasks()" in emitted
    assert "_wreath_background.add_task(store_frame, 'ridge')" in emitted
    assert "JSONResponse({'accepted': True}, background=_wreath_background)" in emitted
    assert "[bg.asyncio_loop]" not in emitted
    compile(emitted, str(tmp_path / "route.py"), "exec")


def test_http_request_timeout_verbs_and_errors_use_first_party_signatures(
    tmp_path: Path,
) -> None:
    source = (
        "import httpx\n"
        "async def notify(url, payload):\n"
        "    try:\n"
        "        async with httpx.AsyncClient() as client:\n"
        "            response = await client.put(\n"
        "                url, json=payload, timeout=httpx.Timeout(timeout=7)\n"
        "            )\n"
        "            response.raise_for_status()\n"
        "            return response.status_code\n"
        "    except (httpx.TimeoutException, httpx.HTTPError):\n"
        "        return 503\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "ClientTimeout(total=7)" in emitted
    assert "_client_url = urlsplit(str(url))" in emitted
    assert "await client.request('PUT', urlunsplit(('', '', _client_url.path" in emitted
    assert "response.status" in emitted
    assert "except (ClientError, ClientError):" in emitted
    assert "httpx" not in emitted
    compile(emitted, str(tmp_path / "outbound.py"), "exec")


def test_http_transport_retries_move_to_the_managed_client(tmp_path: Path) -> None:
    source = (
        "from httpx import AsyncClient, AsyncHTTPTransport\n"
        "async def upload(origin, payload):\n"
        "    transport = AsyncHTTPTransport(retries=3)\n"
        "    async with AsyncClient(transport=transport, timeout=8) as client:\n"
        "        return await client.post(origin + '/frame', json=payload)\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "AsyncHTTPTransport" not in emitted
    assert "AsyncClient" not in emitted
    assert "RetryPolicy(attempts=1 + (3), idempotent_only=False" in emitted
    assert "ClientTimeout(total=8)" in emitted
    compile(emitted, str(tmp_path / "retry.py"), "exec")


def test_repeated_http_local_names_keep_their_own_origin_and_transport(
    tmp_path: Path,
) -> None:
    source = (
        "from typing import Final\n"
        "from httpx import AsyncClient, AsyncHTTPTransport, Timeout\n"
        "RIDGE_TIMEOUT: Final[Timeout] = Timeout(10.0)\n"
        "async def upload_frames(origin):\n"
        "    transport = AsyncHTTPTransport(retries=2)\n"
        "    async def send():\n"
        "        async with AsyncClient(transport=transport, timeout=RIDGE_TIMEOUT) as client:\n"
        "            return await client.get(origin + '/frames')\n"
        "    return await send()\n"
        "async def upload_weather(origin):\n"
        "    transport = AsyncHTTPTransport(retries=4)\n"
        "    async with AsyncClient(transport=transport, timeout=RIDGE_TIMEOUT) as client:\n"
        "        return await client.get(origin + '/weather')\n"
    )
    emitted = port.emit_module(source, opinionated=True)
    assert "RIDGE_TIMEOUT: Final[ClientTimeout] = ClientTimeout(total=10.0)" in emitted
    assert "AsyncHTTPTransport" not in emitted
    assert "_client_url = urlsplit(str(origin + '/frames'))" in emitted
    assert "_client_url_2 = urlsplit(str(origin + '/weather'))" in emitted
    assert "attempts=1 + (2)" in emitted
    assert "attempts=1 + (4)" in emitted
    frames = emitted.index("_client_url.path or '/'")
    weather = emitted.index("_client_url_2.path or '/'")
    assert frames < weather
    compile(emitted, str(tmp_path / "ridge_http.py"), "exec")
