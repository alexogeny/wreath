from __future__ import annotations

from wreath._fuzz import FuzzTarget
from wreath._graphql.parser import GraphQLSyntaxError, Limits, parse

from ._corpus import load_versioned

_LIMITS = Limits(
    max_depth=16,
    max_complexity=2_048,
    max_aliases=64,
    max_steps=50_000,
    max_document_bytes=16_384,
)


def run(data: bytes) -> tuple[str, ...]:
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        return ("graphql:refused:utf8",)
    try:
        document = parse(source, _LIMITS)
    except GraphQLSyntaxError as refusal:
        return (f"graphql:refused:{refusal.code}",)
    if parse(source, _LIMITS) != document:
        raise AssertionError("GraphQL parsing is not deterministic")
    return (
        "graphql:parsed",
        f"graphql:operations:{len(document.operations)}",
        f"graphql:fragments:{len(document.fragments)}",
        f"graphql:depth:{min(document.depth, 8)}",
    )


TARGET = FuzzTarget(
    "graphql-parser",
    run,
    seeds=load_versioned("graphql"),
    dictionary=(
        b"query",
        b"mutation",
        b"fragment",
        b" on ",
        b"...",
        b"$id: ID!",
        b"{",
        b"}",
        b"(",
        b")",
    ),
    source_files=(
        "src/wreath/_graphql/parser.py",
        "src/wreath/_graphql/ast.py",
        "src/wreath/_native/graphql_parser.c",
    ),
    operator_names=(
        "guard.always-fires",
        "guard.never-fires",
        "guard.remove-raise",
        "predicate.always-true",
        "predicate.drop-operand",
        "value.widen-bound",
    ),
)
