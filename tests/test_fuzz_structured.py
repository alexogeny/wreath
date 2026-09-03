from __future__ import annotations

import random
from typing import Any

import pytest

from wreath import xml
from wreath._fuzz.structured import HTTP1_STRATEGY, XML_STRATEGY, StructuredStrategy
from wreath._native import _core


def test_strategy_identity_is_explicitly_versioned() -> None:
    assert HTTP1_STRATEGY.identity == "http1-grammar@1"
    assert XML_STRATEGY.identity == "xml-grammar@1"
    assert HTTP1_STRATEGY.dictionary
    assert XML_STRATEGY.dictionary
    assert all(isinstance(token, bytes) and token for token in HTTP1_STRATEGY.dictionary)


def test_optional_hooks_have_empty_results() -> None:
    strategy = StructuredStrategy("empty", 1, seeds=(b"seed",))
    rng = random.Random(1)

    assert strategy.generate_case(rng, 32) is None
    assert strategy.mutate_case(b"seed", rng, 32) is None
    assert strategy.crossover_case(b"a", b"b", rng, 32) is None
    assert strategy.shrink_cases(b"seed", max_candidates=8, max_size=32) == ()
    assert strategy.dictionary_tokens(b"seed", max_tokens=8, max_token_size=32) == ()


def test_hook_output_and_iteration_are_bounded() -> None:
    def generate(_rng: random.Random, _max_size: int) -> bytes:
        return b"x" * 9

    def shrink(_data: bytes):
        yield from (b"a", b"a", b"bb", b"ccc", b"dddd")

    strategy = StructuredStrategy("bounded", 1, generate=generate, shrink=shrink)

    with pytest.raises(ValueError, match="generate.*max_size"):
        strategy.generate_case(random.Random(1), 8)
    assert strategy.shrink_cases(b"source", max_candidates=2, max_size=2) == (b"a", b"bb")


def test_static_and_input_aware_dictionary_tokens_are_bounded() -> None:
    def dictionary(data: bytes):
        yield data[:2]
        yield b"fixed"
        yield b"toolong"
        while True:
            yield b"duplicate"

    strategy = StructuredStrategy(
        "dictionary",
        1,
        dictionary=(b"fixed",),
        dictionary_hook=dictionary,
    )

    assert strategy.dictionary_tokens(b"input", max_tokens=3, max_token_size=5) == (b"fixed", b"in")


@pytest.mark.parametrize("strategy", [HTTP1_STRATEGY, XML_STRATEGY])
def test_structured_operations_are_deterministic(strategy: StructuredStrategy) -> None:
    def sequence(seed: int) -> tuple[bytes | None, bytes | None, bytes | None]:
        rng = random.Random(seed)
        generated = strategy.generate_case(rng, 512)
        assert generated is not None
        mutated = strategy.mutate_case(generated, rng, 512)
        crossed = strategy.crossover_case(generated, mutated or generated, rng, 512)
        return generated, mutated, crossed

    assert sequence(91) == sequence(91)


def test_http_generation_mutation_and_crossover_preserve_the_grammar() -> None:
    rng = random.Random(7)
    for _ in range(32):
        left = HTTP1_STRATEGY.generate_case(rng, 256)
        right = HTTP1_STRATEGY.generate_case(rng, 256)
        assert left is not None and right is not None
        candidates = (
            left,
            right,
            HTTP1_STRATEGY.mutate_case(left, rng, 256),
            HTTP1_STRATEGY.crossover_case(left, right, rng, 256),
        )

        for candidate in candidates:
            assert candidate is not None
            assert len(candidate) <= 256
            assert _core.http_parse_request(candidate) is not None


def test_http_shrinking_is_valid_unique_smaller_and_bounded() -> None:
    source = b"POST /alpha/beta HTTP/1.1\r\nHost: example.test\r\nContent-Length: 4\r\n\r\ndata"
    candidates = HTTP1_STRATEGY.shrink_cases(source, max_candidates=3, max_size=256)

    assert 0 < len(candidates) <= 3
    assert len(candidates) == len(set(candidates))
    assert all(len(candidate) < len(source) for candidate in candidates)
    assert all(_core.http_parse_request(candidate) is not None for candidate in candidates)


def test_xml_generation_mutation_and_crossover_preserve_the_grammar() -> None:
    rng = random.Random(11)
    for _ in range(32):
        left = XML_STRATEGY.generate_case(rng, 256)
        right = XML_STRATEGY.generate_case(rng, 256)
        assert left is not None and right is not None
        candidates = (
            left,
            right,
            XML_STRATEGY.mutate_case(left, rng, 256),
            XML_STRATEGY.crossover_case(left, right, rng, 256),
        )

        for candidate in candidates:
            assert candidate is not None
            assert len(candidate) <= 256
            xml.parse(candidate)


def test_xml_shrinking_is_valid_unique_smaller_and_bounded() -> None:
    source = b'<root id="value">text<child>more</child></root>'
    candidates = XML_STRATEGY.shrink_cases(source, max_candidates=3, max_size=256)

    assert 0 < len(candidates) <= 3
    assert len(candidates) == len(set(candidates))
    assert all(len(candidate) < len(source) for candidate in candidates)
    for candidate in candidates:
        xml.parse(candidate)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "Bad Name", "name"),
        ("version", 0, "version"),
        ("dictionary", ("text",), "dictionary"),
    ],
)
def test_invalid_strategy_declarations_are_refused(field: str, value: object, message: str) -> None:
    values: dict[str, Any] = {"name": "valid", "version": 1, field: value}
    with pytest.raises((TypeError, ValueError), match=message):
        StructuredStrategy(**values)
