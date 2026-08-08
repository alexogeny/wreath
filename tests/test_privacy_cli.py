"""`wreath privacy`: what it prints, what it exits with, and what it refuses.

The command is the reading half of the module, so its exit code is the product:
a blocked plan must fail a CI step rather than print a warning nobody reads.
The other half of this file is `_load`, which resolves `module:attribute` — a
refusal there is somebody's first five minutes with the command, and an
`AttributeError` three frames down is a worse answer than a sentence saying
where to point it.

There is deliberately no `erase` action, and that absence is asserted: a
production delete button reachable from a shell with an application target and
no other context is exactly what this module argues against.
"""

from __future__ import annotations

import json

import pytest

from wreath._privacy.cli import add_privacy_parser, execute
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.privacy import Erase, Privacy


class FakeDatabase:
    name = "main"


class Person(Model, table="cli_people"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    email: Mapped[str] = column(Text)


class Photo(Model, table="cli_photos"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    owner_id: Mapped[int] = column(Int64, references=Person.id)
    caption: Mapped[str] = column(Text)


class Orphaned(Model, table="cli_orphaned"):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    contact_string: Mapped[str] = column(Text)


def _build(*, blocked: bool) -> Privacy:
    registry = Registry(
        FakeDatabase(), [Person, Photo, Orphaned], validate_schema="off"
    )
    privacy = Privacy(registry)
    privacy.subject(Person, key="id")
    privacy.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    privacy.retain(Photo, after=90 * 86400, on="id", reason="gallery policy")
    if blocked:
        privacy.classify(Orphaned, personal={"contact_string": Erase.REDACT})
    return privacy


#: The module attribute `_load` resolves. Set per test rather than declared, so
#: one module can stand in for both a clean and a blocked application.
target: Privacy | None = None
not_a_privacy = object()


def _namespace(action: str, **extra: object):
    import argparse

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_privacy_parser(commands)
    args = ["privacy", action, f"{__name__}:target"]
    for key, value in extra.items():
        args += [f"--{key}", str(value)]
    return parser.parse_args(args)


def test_a_clean_plan_prints_the_traversal_and_exits_zero(capsys) -> None:
    global target
    target = _build(blocked=False)
    assert execute(_namespace("plan", subject="4711")) == 0
    printed = capsys.readouterr().out
    assert "erasure plan for Person.id = 4711" in printed
    assert "cli_photos" in printed
    assert "Ready. Quote this digest" in printed


def test_a_blocked_plan_exits_one_so_a_ci_step_fails_on_it(capsys) -> None:
    """The point of the command: a finding is a failure, not a note."""
    global target
    target = _build(blocked=True)
    assert execute(_namespace("plan", subject="4711")) == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_the_json_form_is_the_same_plan_with_no_opinions(capsys) -> None:
    global target
    target = _build(blocked=False)
    assert execute(_namespace("plan", subject="4711", format="json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject_id"] == "4711"
    assert payload["blocked"] is False
    assert payload["digest"] == target.plan("4711").digest


def test_the_access_action_prints_the_read_mode_traversal(capsys) -> None:
    global target
    target = _build(blocked=False)
    assert execute(_namespace("access", subject="4711")) == 0
    printed = capsys.readouterr().out
    assert "access request for Person.id = 4711" in printed
    assert "Tables to export" in printed


def test_the_access_action_answers_json_too(capsys) -> None:
    global target
    target = _build(blocked=False)
    assert execute(_namespace("access", subject="4711", format="json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject_model"] == "Person"
    assert "blocked" not in payload, "an export has no verdict to report"


def test_the_retention_action_prints_every_window_and_every_absence(capsys) -> None:
    global target
    target = _build(blocked=False)
    assert execute(_namespace("retention")) == 0
    printed = capsys.readouterr().out
    assert "Photo: rows deleted 90d after id -- gallery policy" in printed
    assert "erasure records: UNBOUNDED" in printed


def test_there_is_no_erase_action(capsys) -> None:
    """An irreversible delete belongs to the place that knows why it is issued."""
    with pytest.raises(SystemExit):
        _namespace("erase", subject="4711")


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("no_colon_at_all", "must be spelled module:attribute"),
        (":target", "must be spelled module:attribute"),
        ("a.module:", "must be spelled module:attribute"),
    ],
)
def test_a_target_that_is_not_module_colon_attribute_is_refused(
    spec: str, message: str
) -> None:
    """Each half of the spelling check, because each half is a different typo."""
    import argparse

    from wreath._privacy.cli import execute as run

    namespace = argparse.Namespace(
        target=spec, privacy_action="retention", subject="4711"
    )
    with pytest.raises(ValueError, match=message):
        run(namespace)


def test_a_module_that_cannot_be_imported_says_so() -> None:
    import argparse

    namespace = argparse.Namespace(
        target="wreath.no_such_module:thing", privacy_action="retention"
    )
    with pytest.raises(ValueError, match="could not import"):
        execute(namespace)


def test_a_module_without_that_attribute_says_so() -> None:
    import argparse

    namespace = argparse.Namespace(
        target=f"{__name__}:nothing_here", privacy_action="retention"
    )
    with pytest.raises(ValueError, match="has no attribute"):
        execute(namespace)


def test_a_target_that_is_not_a_privacy_object_says_where_to_point_instead() -> None:
    """Refused by type: duck-typing this produces an error three frames down."""
    import argparse

    namespace = argparse.Namespace(
        target=f"{__name__}:not_a_privacy", privacy_action="retention"
    )
    with pytest.raises(ValueError, match="not a wreath.privacy.Privacy"):
        execute(namespace)
