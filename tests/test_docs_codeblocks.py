from __future__ import annotations

import pytest

from wreath._docs import codeblocks


def page(*blocks: str) -> str:
    return "\n\n".join(f"```python\n{b}\n```" for b in blocks)


def messages(text: str) -> list[str]:
    findings, _ = codeblocks.check_page(text, "scratch.md")
    return [f.message for f in findings]


# Each is the real spelling from the page it shipped on, not a paraphrase.

FICTIONS = [
    pytest.param(
        "from wreath.postgres import Database\n"
        "db = Database()\n"
        'value = await db.pool("read").fetchval("SELECT 1")',
        "Pool has no attribute `fetchval`",
        id="pool-fetchval",
    ),
    pytest.param(
        "from wreath.postgres import Database\n"
        "db = Database()\n"
        'async with db.pool("write").acquire() as conn:\n'
        "    pass",
        "is a coroutine function, not an async context manager",
        id="acquire-as-context-manager",
    ),
    pytest.param(
        "from wreath.postgres import Database\ndb = Database()\nawait db.ping()",
        "Database has no attribute `ping`",
        id="database-ping",
    ),
    pytest.param(
        "from typing import Annotated\n"
        "from wreath.binding import Depends\n"
        "from wreath.orm import Session\n"
        "async def handler(request, s: Annotated[Session, Depends(open_session)]):\n"
        "    pass",
        "Depends inside Annotated is ignored",
        id="depends-in-annotated",
    ),
    pytest.param(
        # `legacy_page_params` is a frozen replica of the signature that
        # shipped; `wreath.pagination.page_params` has since been fixed, and
        # importing the fixed one here would leave this case asserting nothing.
        # See `tests/_paging_fiction.py`.
        "from _paging_fiction import legacy_page_params\n"
        "from wreath.binding import Depends\n"
        "async def handler(request, params = Depends(legacy_page_params)):\n"
        "    pass",
        "carry binding markers",
        id="markers-in-a-dependency",
    ),
]


#: What each fiction depends on *staying absent* from the live API. The source
#: above is frozen, but three of these cases are fictions only because the
#: attribute does not exist -- add `Pool.fetchval` and `pool-fetchval` keeps
#: passing while proving nothing, exactly the way the `page_params` case rotted
#: when the defect it pointed at was fixed. A frozen specimen is not enough on
#: its own when the specimen's *falseness* is a property of live code.
ABSENT_BY_ASSUMPTION = [
    ("wreath.postgres", "Pool", "fetchval"),
    ("wreath.postgres", "Database", "ping"),
]


@pytest.mark.parametrize(("module", "cls", "attribute"), ABSENT_BY_ASSUMPTION)
def test_the_fictions_are_still_fictions(module: str, cls: str, attribute: str) -> None:
    import importlib

    owner = getattr(importlib.import_module(module), cls)
    assert not hasattr(owner, attribute), (
        f"{cls}.{attribute} now exists, so the {cls.lower()}-{attribute} case in "
        "FICTIONS is no longer a fiction and proves nothing. Replace it with a "
        "spelling that is still wrong, or drop it."
    )


@pytest.mark.parametrize(("source", "expected"), FICTIONS)
def test_the_floor_catches_every_fiction_that_shipped(source: str, expected: str) -> None:
    found = messages(page(source))
    assert any(expected in m for m in found), f"not caught: {found}"


def test_a_correct_spelling_is_not_flagged() -> None:
    assert not messages(
        page(
            "from wreath.postgres import Database\n"
            "db = Database()\n"
            "conn = await db.pool('read').acquire()"
        )
    )


def test_names_accumulate_across_blocks_on_one_page() -> None:
    text = page(
        "from wreath.postgres import Database\ndatabase = Database()",
        "await database.nonexistent()",
    )
    assert any("has no attribute `nonexistent`" in m for m in messages(text))


def test_an_annotation_beats_the_conventional_vocabulary() -> None:
    text = page(
        "from wreath.websocket import WebSocket\n"
        "async def feed(connection: WebSocket) -> None:\n"
        "    await connection.accept()\n"
        "    await connection.send_text('hi')"
    )
    assert not messages(text)


def test_rebinding_a_vocabulary_name_drops_it_rather_than_guessing() -> None:
    text = page("response = await billing.get('/x')\nprint(response.json())")
    assert not messages(text)


def test_a_loop_target_is_not_resolved_against_the_vocabulary() -> None:
    text = page("for connection in clients:\n    await connection.send_text('x')")
    assert not messages(text)


def test_an_unparseable_python_block_is_reported_not_skipped() -> None:
    found = messages("```python\n.measure(x=1).seal()\n```")
    assert any("does not parse" in m for m in found)


def test_a_marked_fragment_is_accepted_with_its_reason() -> None:
    text = '```python no-check="continues the block above"\n.measure(x=1).seal()\n```'
    assert not messages(text)


def test_no_check_suppresses_a_finding_that_would_otherwise_fire() -> None:
    source = "from wreath.postgres import Database\ndb = Database()\nawait db.ping()"
    assert messages(f"```python\n{source}\n```")
    assert not messages(f'```python no-check="illustrative"\n{source}\n```')


def test_a_nested_fence_is_not_scanned_as_its_own_block() -> None:
    text = "````markdown\n```python\nawait db.ping()\n```\n````"
    assert not messages(text)


def test_there_is_no_parked_defect_list() -> None:
    assert not hasattr(codeblocks, "KNOWN_DEFECTS")
    assert not hasattr(codeblocks, "_known")
