"""Declaration errors are refused where they are declared, not where they land.

`docs/decisions/0019-refuse-rather-than-half-wire.md` names the family: a
declaration whose shape is knowable at registration, accepted there and then
failing per request as a status that blames the caller. Each case below was
observed as a runtime failure before it was a refusal, and the comment on each
records what the caller used to see.

The over-refusal guards matter as much as the refusals. A check that rejects a
correct declaration is worse than the defect it replaces, because it fails at
import and takes the whole application with it.
"""

from __future__ import annotations

import os
import tempfile
import typing

import pytest

from wreath._pure.typegen import ts_type
from wreath.authorization import EntityUid
from wreath.crud import Access
from wreath.objects import LocalObjectStore, MemoryObjectStore
from wreath.openapi import _openapi_schema
from wreath.typegen.model import TypeKind, TypeRef

# -- Access.cedar: a bare type name was a 500 on a route declaring 403 ---------


@pytest.mark.parametrize("resource", ["Registry", "Registry:", "", "Type::", "::id"])
def test_a_resource_that_is_not_an_entity_reference_is_refused(resource: str) -> None:
    """Previously accepted, then `CedarParseError` per request -- a 500.

    `EntityUid.parse` is what the engine eventually calls, so refusing here asks
    the same question at declaration that the request would have asked later.
    """
    with pytest.raises(ValueError, match="not a Cedar entity reference"):
        Access.cedar(action="read", resource=resource)


def test_the_check_is_the_engines_own_and_no_stricter() -> None:
    """`EntityUid.parse` also accepts the bare `Type::id` form, so this does too.

    Asking a *different* question at declaration would refuse declarations that
    work, which is the failure mode a refusal is supposed to prevent.
    """
    assert EntityUid.parse('Type::"unterminated').type == "Type"
    Access.cedar(action="r", resource='Type::"unterminated')
    Access.cedar(action="r", resource="Reserve::bare-id")


def test_the_refusal_names_the_form_that_would_have_worked() -> None:
    """A refusal a reader cannot act on is only a faster failure."""
    with pytest.raises(ValueError) as caught:
        Access.cedar(action="read", resource="Registry")
    message = str(caught.value)
    assert 'Type::"id"' in message
    assert "Registry" in message
    assert "callable" in message  # the escape hatch for a custom mapper


@pytest.mark.parametrize(
    "resource",
    ['Registry::"species"', 'Station::"{id}"', 'A::B::"c"', 'T::"{a}-{b}"'],
)
def test_a_correct_resource_is_still_accepted(resource: str) -> None:
    """The over-refusal guard: templates are checked for shape, not for values."""
    assert Access.cedar(action="read", resource=resource).resource == resource


def test_a_callable_or_entity_uid_bypasses_the_string_check() -> None:
    """Not every deployment maps a resource from a string; both escapes stay open."""
    resolver = lambda request: request  # noqa: E731 - the shape is the point
    assert Access.cedar(action="r", resource=resolver).resource is resolver
    uid = EntityUid("Reserve", "abc")
    assert Access.cedar(action="r", resource=uid).resource is uid


def test_a_template_is_validated_by_substitution_not_by_guessing() -> None:
    """`Type::"{id}"` cannot be parsed as written; the check fills it first."""
    with pytest.raises(ValueError):
        Access.cedar(action="r", resource="{id}")          # no type at all
    Access.cedar(action="r", resource='Reserve::"{slug}"')  # shape is sound


# -- url_secret: a str survived construction and raised from inside hmac ------


@pytest.mark.parametrize("value", ["not-bytes", 42, ["k"], {"k": 1}])
def test_a_non_bytes_url_secret_is_refused_by_both_stores(value: object) -> None:
    """Previously: `TypeError` from `hmac.new` on the first `url()` call, naming
    neither the option nor the registration that supplied it."""
    with tempfile.TemporaryDirectory() as root:
        with pytest.raises(TypeError, match="url_secret must be bytes"):
            LocalObjectStore(root, url_secret=value)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="url_secret must be bytes"):
        MemoryObjectStore(url_secret=value)  # type: ignore[arg-type]


def test_the_str_refusal_says_how_to_fix_it() -> None:
    with pytest.raises(TypeError, match=r"encode it, e\.g\. url_secret=secret\.encode\(\)"):
        MemoryObjectStore(url_secret="secret")  # type: ignore[arg-type]


def test_refusing_a_url_secret_does_not_leak_the_root_descriptor() -> None:
    """The refusal runs before `open_root`.

    Ordered the other way the check would trade one defect for a worse one: the
    half-built store is discarded, so its `close` never runs and the descriptor
    is held for the process lifetime.
    """
    with tempfile.TemporaryDirectory() as root:
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(25):
            with pytest.raises(TypeError):
                LocalObjectStore(root, url_secret="nope")  # type: ignore[arg-type]
        assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("value", [None, b"k" * 32, bytearray(b"k" * 32)])
def test_a_usable_url_secret_is_still_accepted(value: object) -> None:
    """The over-refusal guard: `bytes`, buffers, and omission all keep working."""
    store = MemoryObjectStore(url_secret=value)  # type: ignore[arg-type]
    assert "signature=" in store.url("a/b.txt", expires=60)


def test_app_objects_refuses_at_registration() -> None:
    """`Wreath.objects` takes `**options`, so the annotation alone stops nothing.

    Registration constructs the store, so the constructor's check *is* the
    registration-time check -- there is no second place to keep in step.
    """
    import wreath

    with tempfile.TemporaryDirectory() as root:
        app = wreath.Wreath()
        with pytest.raises(TypeError, match="url_secret must be bytes"):
            app.objects("media", backend="local", root=root, url_secret="str")


# -- TypeKind: a new kind emitted a wrong schema and a wrong client -----------


def test_an_unhandled_type_kind_is_refused_by_both_emitters() -> None:
    """Previously `{}` and `"unknown"`, with the generator reporting success.

    The output is a client somebody ships, so a silent default is the most
    expensive shape this failure takes.
    """
    with pytest.raises(ValueError, match="no OpenAPI schema for TypeKind"):
        _openapi_schema(TypeRef(kind="datetime"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no TypeScript type for TypeKind"):
        ts_type(("datetime", None, (), ()))


def test_each_refusal_names_its_counterpart() -> None:
    """The two must be extended together; each message says so."""
    with pytest.raises(ValueError, match=r"wreath\._pure\.typegen\.ts_type"):
        _openapi_schema(TypeRef(kind="nope"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"wreath\.openapi\._openapi_schema"):
        ts_type(("nope", None, (), ()))


@pytest.mark.parametrize("kind", typing.get_args(TypeKind))
def test_every_declared_kind_still_renders(kind: str) -> None:
    """The over-refusal guard, derived from `TypeKind` itself.

    Enumerating the Literal rather than restating it means adding a kind makes
    this test fail until both emitters handle it -- which is the property the
    refusal exists to enforce, checked rather than asserted.
    """
    arguments = (TypeRef("string"),) if kind in ("array", "tuple", "record", "union") else ()
    literals: tuple[str, ...] = ("a",) if kind == "literal" else ()
    name = "Widget" if kind == "reference" else None
    ref = TypeRef(kind=kind, name=name, arguments=arguments, literals=literals)  # type: ignore[arg-type]
    assert _openapi_schema(ref) is not None
    node = (kind, name, tuple((a.kind, a.name, (), ()) for a in arguments), literals)
    assert ts_type(node)
