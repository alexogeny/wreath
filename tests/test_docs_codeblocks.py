"""The docs floor: does it catch the fictions that motivated it, and does it
still have something to check?

Two halves. The first drives `codeblocks` over synthetic pages and asserts the
specific defect is named -- including all five that actually shipped, which is
the acceptance test: a check built to catch known bugs must catch them.

The second is the ratchet. A static checker degrades silently: one wrong
`VOCABULARY` entry, one over-eager `bind_name`, and it resolves nothing while
still exiting 0. So the corpus counts are pinned with a floor, and the parked
defect list is pinned with a ceiling. Both may move in the improving direction
and neither may drift the other way unnoticed.
"""

from __future__ import annotations

import pathlib

import pytest

from wreath._docs import codeblocks

DOCS = pathlib.Path(__file__).resolve().parents[1] / "docs"


def page(*blocks: str) -> str:
    return "\n\n".join(f"```python\n{b}\n```" for b in blocks)


def messages(text: str) -> list[str]:
    findings, _ = codeblocks.check_page(text, "scratch.md")
    return [f.message for f in findings]


# --- the five that shipped ---------------------------------------------------
#
# Each is the real spelling from the page it shipped on, not a paraphrase.

FICTIONS = [
    pytest.param(
        'from wreath.postgres import Database\n'
        'db = Database()\n'
        'value = await db.pool("read").fetchval("SELECT 1")',
        "Pool has no attribute `fetchval`",
        id="pool-fetchval",
    ),
    pytest.param(
        'from wreath.postgres import Database\n'
        'db = Database()\n'
        'async with db.pool("write").acquire() as conn:\n'
        '    pass',
        "is a coroutine function, not an async context manager",
        id="acquire-as-context-manager",
    ),
    pytest.param(
        'from wreath.postgres import Database\ndb = Database()\nawait db.ping()',
        "Database has no attribute `ping`",
        id="database-ping",
    ),
    pytest.param(
        'from typing import Annotated\n'
        'from wreath.binding import Depends\n'
        'from wreath.orm import Session\n'
        'async def handler(request, s: Annotated[Session, Depends(open_session)]):\n'
        '    pass',
        "Depends inside Annotated is ignored",
        id="depends-in-annotated",
    ),
    pytest.param(
        # `legacy_page_params` is a frozen replica of the signature that
        # shipped; `wreath.pagination.page_params` has since been fixed, and
        # importing the fixed one here would leave this case asserting nothing.
        # See `tests/_paging_fiction.py`.
        'from _paging_fiction import legacy_page_params\n'
        'from wreath.binding import Depends\n'
        'async def handler(request, params = Depends(legacy_page_params)):\n'
        '    pass',
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
    """The absence each specimen relies on is asserted, not assumed.

    If wreath ever grows one of these, this fails first and names it -- rather
    than the corresponding case in `FICTIONS` quietly becoming a test that the
    floor accepts correct code.
    """
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
    """The negative half. A checker that flags everything catches everything."""
    assert not messages(
        page(
            "from wreath.postgres import Database\n"
            "db = Database()\n"
            "conn = await db.pool('read').acquire()"
        )
    )


# --- resolution, and its limits ----------------------------------------------


def test_names_accumulate_across_blocks_on_one_page() -> None:
    """A page that binds `orm` in block one means it in block two."""
    text = page(
        "from wreath.postgres import Database\ndatabase = Database()",
        "await database.nonexistent()",
    )
    assert any("has no attribute `nonexistent`" in m for m in messages(text))


def test_an_annotation_beats_the_conventional_vocabulary() -> None:
    """`connection: WebSocket` must not be checked against a DB connection.

    This was a live false positive: the websocket guides annotate their handler
    parameter, and the vocabulary said `connection` meant `postgres.Connection`.
    """
    text = page(
        "from wreath.websocket import WebSocket\n"
        "async def feed(connection: WebSocket) -> None:\n"
        "    await connection.accept()\n"
        "    await connection.send_text('hi')"
    )
    assert not messages(text)


def test_rebinding_a_vocabulary_name_drops_it_rather_than_guessing() -> None:
    """An untypeable assignment must clear the conventional binding.

    `response = await client.get(...)` is not a `wreath.Response`, and resolving
    its attributes against one invented errors on two real pages.
    """
    text = page("response = await billing.get('/x')\nprint(response.json())")
    assert not messages(text)


def test_a_loop_target_is_not_resolved_against_the_vocabulary() -> None:
    text = page("for connection in clients:\n    await connection.send_text('x')")
    assert not messages(text)


def test_an_unparseable_python_block_is_reported_not_skipped() -> None:
    """The hole a floor most easily grows: silently skipping what it can't read."""
    found = messages("```python\n.measure(x=1).seal()\n```")
    assert any("does not parse" in m for m in found)


def test_a_marked_fragment_is_accepted_with_its_reason() -> None:
    text = '```python no-check="continues the block above"\n.measure(x=1).seal()\n```'
    assert not messages(text)


def test_no_check_suppresses_a_finding_that_would_otherwise_fire() -> None:
    source = (
        "from wreath.postgres import Database\ndb = Database()\nawait db.ping()"
    )
    assert messages(f"```python\n{source}\n```")
    assert not messages(f'```python no-check="illustrative"\n{source}\n```')


def test_a_nested_fence_is_not_scanned_as_its_own_block() -> None:
    """A guide documenting fence syntax must not have its example checked."""
    text = "````markdown\n```python\nawait db.ping()\n```\n````"
    assert not messages(text)


# --- the ratchet -------------------------------------------------------------


def _corpus() -> dict[str, str]:
    excluded = ("plans/", "decisions/", "agents/", "release_notes/")
    pages = {}
    for path in sorted(DOCS.rglob("*.md")):
        rel = path.relative_to(DOCS).as_posix()
        if rel.startswith(excluded):
            continue
        pages[rel] = path.read_text(encoding="utf-8")
    return pages


#: Measured when the floor landed. A *floor* on what it looks at, not a target:
#: it may resolve more, never less. A drop means resolution regressed and the
#: check quietly stopped checking -- which is the whole failure mode here.
MINIMUM_RESOLVED_CHAINS = 600
MINIMUM_BLOCKS = 280


def test_the_floor_still_resolves_most_of_the_corpus() -> None:
    stats = codeblocks.coverage(_corpus())
    assert stats.blocks >= MINIMUM_BLOCKS, (
        f"only {stats.blocks} python blocks found; the scanner may have broken"
    )
    assert stats.resolved >= MINIMUM_RESOLVED_CHAINS, (
        f"resolved {stats.resolved} chains, was at least {MINIMUM_RESOLVED_CHAINS}: "
        "the floor is looking at less than it used to"
    )


def test_there_is_no_parked_defect_list() -> None:
    """The floor has no waiver, so a finding is a failure with nowhere to go.

    `KNOWN_DEFECTS` held the eight real defects the floor found on the day it
    landed, parked because each needed a decision about what the page *meant*.
    All eight are fixed, so the list and its filter are gone rather than kept at
    zero: an empty waiver still reads as permission, and the next author with an
    awkward page finds a mechanism instead of a decision. A block that genuinely
    should not be checked says so at the fence, with a reason, where a reviewer
    reads it.
    """
    assert not hasattr(codeblocks, "KNOWN_DEFECTS")
    assert not hasattr(codeblocks, "_known")


def test_the_published_corpus_has_no_unparked_findings() -> None:
    """What `wreath docs check` asserts, as a test that names the page."""
    leftover = []
    for path, text in _corpus().items():
        findings, _ = codeblocks.check_page(text, path)
        leftover.extend(str(f) for f in findings)
    assert not leftover, "\n".join(leftover)
