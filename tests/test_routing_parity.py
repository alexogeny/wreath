"""Behavioural parity across all four routing implementations.

Every registered path, HEAD fallback, and a large random fuzz corpus must
produce identical matches from c-dt, py-dt, c-trie, and py-trie.
"""

from __future__ import annotations

import random

import pytest
from _routing_impls import IMPLS, RouteTableLike, build, normalize

ROUTES = [
    ("GET", "/"),
    ("GET", "/users"),
    ("GET", "/users/me"),
    ("GET", "/users/{uid}"),
    ("GET", "/users/{uid}/posts"),
    ("GET", "/users/{uid}/posts/{pid}"),
    ("GET", "/users/{uid}/posts/latest"),
    ("POST", "/users/{uid}/posts"),
    ("GET", "/files/{a}/{b}/{c}"),
    ("GET", "/static/css/main"),
    ("GET", "/static/{kind}/main"),
    ("GET", "/a/b/c/d/e"),
    ("DELETE", "/users/{uid}"),
    ("GET", "/orders/{id}/items/{item}/detail"),
    ("GET", "/{root}"),
    ("PUT", "/x/{y}/z"),
    ("PUT", "/x/y/z"),
]

_POOL = [
    "users", "me", "posts", "latest", "files", "static", "css", "main", "x", "y",
    "z", "1", "2", "a", "b", "c", "d", "e", "orders", "items", "detail", "42", "",
]


def _tables() -> dict[str, RouteTableLike]:
    return {name: build(factory, ROUTES) for name, factory in IMPLS.items()}


def _concrete(path: str) -> str:
    return "/".join("42" if s.startswith("{") else s for s in path.split("/"))


def test_registered_paths_agree() -> None:
    tables = _tables()
    for method, path in ROUTES:
        concrete = _concrete(path)
        for probe_method in (method, "HEAD"):
            results = {
                name: normalize(t.match(probe_method, concrete))
                for name, t in tables.items()
            }
            assert len({repr(r) for r in results.values()}) == 1, (
                probe_method,
                concrete,
                results,
            )


def test_random_fuzz_agrees() -> None:
    tables = _tables()
    rng = random.Random(20260714)
    methods = ["GET", "POST", "DELETE", "HEAD", "PUT", "PATCH"]
    for _ in range(20000):
        n = rng.randint(0, 6)
        path = "/" + "/".join(rng.choice(_POOL) for _ in range(n))
        method = rng.choice(methods)
        results = {name: normalize(t.match(method, path)) for name, t in tables.items()}
        assert len({repr(r) for r in results.values()}) == 1, (method, path, results)


@pytest.mark.parametrize("name", list(IMPLS))
def test_duplicate_static_route_rejected(name: str) -> None:
    table = IMPLS[name]()
    table.add("/x", "GET", 1)
    with pytest.raises(ValueError, match="duplicate route"):
        table.add("/x", "GET", 2)


@pytest.mark.parametrize("name", list(IMPLS))
def test_conflicting_dynamic_route_rejected(name: str) -> None:
    table = IMPLS[name]()
    table.add("/y/{a}", "GET", 1)
    with pytest.raises(ValueError, match="conflicting route"):
        table.add("/y/{b}", "GET", 2)


@pytest.mark.parametrize("name", list(IMPLS))
def test_parameter_capture(name: str) -> None:
    table = IMPLS[name]()
    table.add("/users/{uid}/posts/{pid}", "GET", "h")
    handler, params = table.match("GET", "/users/7/posts/9")
    assert handler == "h"
    assert params == {"uid": "7", "pid": "9"}


@pytest.mark.parametrize("name", list(IMPLS))
def test_literal_beats_parameter(name: str) -> None:
    table = IMPLS[name]()
    table.add("/users/{uid}", "GET", "param")
    table.add("/users/me", "GET", "literal")
    assert table.match("GET", "/users/me")[0] == "literal"
    assert table.match("GET", "/users/42")[0] == "param"


@pytest.mark.parametrize("name", list(IMPLS))
def test_static_match_is_repeatable(name: str) -> None:
    # Regression: the C decision table returns a precomputed (handler, None)
    # tuple for static hits; repeated matches must stay correct and identical.
    table = IMPLS[name]()
    table.add("/health", "GET", "h")
    for _ in range(3):
        assert table.match("GET", "/health") == ("h", None)
        assert table.match("HEAD", "/health") == ("h", None)


@pytest.mark.skipif("c-bitset" not in IMPLS, reason="native extension unavailable")
def test_compiled_native_bitset_is_sealed_and_keeps_both_route_shapes() -> None:
    table = IMPLS["c-bitset"]()
    table.add("/health", "GET", "static")
    table.add("/users/{uid}", "GET", "dynamic")
    table.compile()

    assert table.match("GET", "/health") == ("static", None)
    assert table.match("GET", "/users/42") == ("dynamic", {"uid": "42"})
    with pytest.raises(RuntimeError, match="immutable"):
        table.add("/late", "GET", "late")


@pytest.mark.parametrize("name", list(IMPLS))
def test_head_falls_back_after_dynamic_verification_miss(name: str) -> None:
    # Regression: an explicit dynamic HEAD route that reaches a leaf but fails
    # literal verification must still fall back to the GET tree. The C
    # decision table used to report a plain miss here.
    table = IMPLS[name]()
    table.add("/h/{x}/only-head", "HEAD", "head-route")
    table.add("/h/{x}/other", "GET", "get-route")
    assert normalize(table.match("HEAD", "/h/1/other")) == ("get-route", {"x": "1"})
    assert normalize(table.match("HEAD", "/h/1/only-head")) == ("head-route", {"x": "1"})
    assert table.match("HEAD", "/h/1/neither") is None


# --- sorted child lookup ----------------------------------------------------
#
# The native trie keeps each node's children sorted so a wide static fanout is
# found by binary search rather than a scan. Registration order therefore must
# not influence matching, and every child must still be reachable.

def _build_stable(factory, routes: list[tuple[str, str]]):
    """Build with an order-independent handler, so only ordering is under test.

    `build()` keys the handler off the list index, which would itself change
    when the list is reordered.
    """
    table = factory()
    for method, path in routes:
        table.add(path, method, f"{method} {path}")
    return table


def test_registration_order_does_not_affect_matching() -> None:
    """Sorted insertion must be a pure implementation detail."""
    forward = list(ROUTES)
    reverse = list(reversed(ROUTES))
    shuffled = list(ROUTES)
    random.Random(1234).shuffle(shuffled)

    for name, factory in IMPLS.items():
        tables = [_build_stable(factory, order) for order in (forward, reverse, shuffled)]
        for _method, path in ROUTES:
            concrete = _concrete(path)
            for probe in ("GET", "POST", "PUT", "DELETE", "HEAD"):
                results = [normalize(t.match(probe, concrete)) for t in tables]
                assert len({repr(r) for r in results}) == 1, (name, probe, concrete)


@pytest.mark.parametrize("name", list(IMPLS))
def test_wide_static_fanout_finds_every_child(name: str) -> None:
    factory = IMPLS[name]
    table = factory()
    count = 500
    for i in range(count):
        table.add(f"/seg-{i:05d}/leaf", "GET", i)
    for i in range(count):
        result = normalize(table.match("GET", f"/seg-{i:05d}/leaf"))
        assert result == (i, None), i
    assert table.match("GET", "/seg-99999/leaf") is None


@pytest.mark.parametrize("name", list(IMPLS))
def test_wide_fanout_with_shared_prefixes_and_lengths(name: str) -> None:
    """Segments that share prefixes or differ only in length must not collide.

    The child order tie-breaks equal prefixes by length, so these are exactly
    the cases a wrong comparison would confuse.
    """
    factory = IMPLS[name]
    table = factory()
    segments = ["a", "aa", "aaa", "ab", "b", "ba", "", "a-b", "a_b", "A", "AA"]
    for i, seg in enumerate(segments):
        table.add(f"/{seg}/leaf", "GET", i)
    for i, seg in enumerate(segments):
        assert normalize(table.match("GET", f"/{seg}/leaf")) == (i, None), seg


@pytest.mark.parametrize("name", list(IMPLS))
def test_literal_precedence_survives_wide_fanout(name: str) -> None:
    factory = IMPLS[name]
    table = factory()
    for i in range(200):
        table.add(f"/item-{i:04d}", "GET", i)
    table.add("/{anything}", "GET", "param")
    # A registered literal wins over the parameter route...
    assert normalize(table.match("GET", "/item-0100")) == (100, None)
    # ...and an unregistered one still falls back to it.
    assert normalize(table.match("GET", "/nope")) == ("param", {"anything": "nope"})


# --- adversarial miss -------------------------------------------------------

def _adversarial(factory, depth: int):
    """Every level offers both a literal and a parameter branch."""
    table = factory()
    for combo in range(1 << depth):
        segments = ["a" if (combo >> i) & 1 else f"{{p{i}}}" for i in range(depth)]
        table.add("/" + "/".join(segments), "POST", combo)
    return table, "/" + "/".join(["a"] * depth)


@pytest.mark.parametrize("name", list(IMPLS))
def test_adversarial_miss_terminates_and_agrees(name: str) -> None:
    """A method miss where every level branches must still be a clean miss.

    The trie is a tree, so each (node, index) state is reachable by exactly one
    path and the traversal visits each node at most once: the work is bounded by
    the table's own size, not by re-exploring failed states.
    """
    factory = IMPLS[name]
    table, path = _adversarial(factory, 10)
    assert table.match("GET", path) is None
    # The same table still answers the method it does have.
    assert normalize(table.match("POST", path)) is not None


@pytest.mark.parametrize("name", list(IMPLS))
def test_adversarial_table_still_captures_parameters(name: str) -> None:
    factory = IMPLS[name]
    table, _path = _adversarial(factory, 6)
    result = normalize(table.match("POST", "/a/zz/a/zz/a/zz"))
    assert result is not None
    _handler, params = result
    assert params == {"p1": "zz", "p3": "zz", "p5": "zz"}
