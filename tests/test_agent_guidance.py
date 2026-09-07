import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
MAX_GUIDANCE_BYTES = 8 * 1024
ROUTED_CARDS = {
    "framework.md",
    "native.md",
    "parallel-work.md",
    "performance.md",
    "postgres.md",
    "server-edge.md",
    "test-runner.md",
    "test-validity.md",
    "testing.md",
    "toolchain.md",
}
ROOT_RULES = (
    "Target CPython 3.14",
    "Keep `src/wreath` free of mandatory third-party runtime dependencies.",
    "Do not integrate Pydantic",
    "Do not add SQLAlchemy",
    "Keep framework and server layers separable.",
    "Add focused tests for every behavior change and regression.",
    "uv run wreath test",
    "uv run wreath-check",
)


def compact(text: str) -> str:
    return " ".join(text.split())


def test_root_agent_guidance_fits_context_budget() -> None:
    guidance = ROOT / "AGENTS.md"

    assert guidance.stat().st_size <= MAX_GUIDANCE_BYTES


def test_root_agent_guidance_routes_every_bounded_card() -> None:
    root_text = (ROOT / "AGENTS.md").read_text()
    routed = set(re.findall(r"docs/agent/([a-z-]+\.md)", root_text))

    assert routed == ROUTED_CARDS
    for name in sorted(routed):
        card = ROOT / "docs" / "agent" / name
        assert card.stat().st_size <= MAX_GUIDANCE_BYTES, name


def test_root_agent_guidance_retains_universal_rules() -> None:
    root_text = (ROOT / "AGENTS.md").read_text()

    for rule in ROOT_RULES:
        assert rule in root_text, rule


def test_routed_native_rules_are_self_contained() -> None:
    native_text = compact((ROOT / "docs" / "agent" / "native.md").read_text())

    assert (
        "For object construction use `PyBytes_FromStringAndSize(NULL, n)` and fill "
        "the buffer -- `NULL` always allocates"
    ) in native_text


def test_whole_tree_oracle_rule_stays_in_root_guidance() -> None:
    root_text = compact((ROOT / "AGENTS.md").read_text())

    assert "Correctness is checked against an independent implementation" in root_text
    assert "anchor on the RFC, the published vectors or the stdlib" in root_text


def test_local_guidance_links_resolve() -> None:
    guidance_files = [ROOT / "AGENTS.md"] + [
        ROOT / "docs" / "agent" / name for name in sorted(ROUTED_CARDS)
    ]

    for guidance in guidance_files:
        for target in re.findall(
            r"(?<![A-Za-z0-9_])\[[^]\n]+\]\(([^)\n]+)\)", guidance.read_text()
        ):
            if "://" in target or target.startswith("#"):
                continue
            path = guidance.parent / target.partition("#")[0]
            assert path.exists(), f"{guidance.relative_to(ROOT)}: {target}"
