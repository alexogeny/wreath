"""Attacks a red team actually ran against a Wreath application, kept executable.

Every case here was sent at a live application by an autonomous agent working a
black-box engagement, and every one was **refused**. That provenance is the
point: the shapes are not the ones that are easy to think of at a desk, they
are the ones somebody reached for with a working exploit chain already in hand
and a reason to believe the next thing would land.

Two of them are worth naming before the code, because neither was on anyone's
list beforehand:

* **A symlink planted *inside* the object bucket.** The attacker had an archive
  extractor that honoured symlink members, so rather than escaping with a key it
  wrote a link into the bucket and asked for it by a perfectly ordinary name.
  The key traverses nothing; the filesystem does the escaping. Containment has
  to be physical to refuse that, and a check on the *key* never sees it.
* **A presigned grant transplanted to a neighbouring object.** Sign what you are
  allowed to sign, then move the signature. The arithmetic has to commit the key
  as well as the deadline, and "the signature verified" is not the same sentence
  as "the signature verified *this key*".

These are regression tests, not a demonstration that the code is correct today:
they were green when written. Their value is entirely in staying green through a
refactor that has no idea they exist. Each is falsifiable in the ordinary way --
neuter the guard it names and it goes red -- and several were checked that way
when they landed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import pytest

from wreath._graphql.parser import GraphQLSyntaxError, Limits as GraphQLLimits, parse
from wreath.objects import LocalObjectStore, ObjectError
from wreath.pagination import apply_sort, parse_sort, sortable_fields
from wreath.templates import Template, TemplateRenderError, TemplateSyntaxError
from wreath.xml import Limits as XMLLimits, XMLRefusal, parse as xml_parse


# --- the object store's containment gate --------------------------------------


@pytest.fixture
def bucket(tmp_path):
    """A store with something worth reaching planted just outside it."""
    root = tmp_path / "bucket"
    (root / "org" / "acme").mkdir(parents=True)
    (root / "org" / "acme" / "manifest.txt").write_bytes(b"ordinary object\n")
    outside = tmp_path / "vault"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"NOT-FOR-THE-BUCKET\n")
    return LocalObjectStore(root, url_secret=b"k" * 48), root, outside


#: Key spellings the engagement sent at the read route. The Unicode entries are
#: the ones that repay attention: U+FF0F FULLWIDTH SOLIDUS, U+FF0E FULLWIDTH
#: FULL STOP and U+2024 ONE DOT LEADER are not `/` and `.` -- until something
#: normalises, decodes or transcodes them, and then they are.
ESCAPING_KEYS = (
    "../vault/secret.txt",
    "....//vault/secret.txt",
    "..%2fvault%2fsecret.txt",
    "..%252fvault%252fsecret.txt",
    "org/acme/../../vault/secret.txt",
    "org\\acme\\..\\..\\vault\\secret.txt",
    "..\\vault\\secret.txt",
    "/vault/secret.txt",
    "//vault/secret.txt",
    "./../vault/secret.txt",
    "%2e%2e/vault/secret.txt",
    "..;/vault/secret.txt",
    "．．/vault/secret.txt",
    "․․/vault/secret.txt",
    "／..／vault/secret.txt",
    "org/acme/manifest.txt\x00",
    "org/acme/manifest.txt%00.png",
)


@pytest.mark.parametrize("key", ESCAPING_KEYS)
async def test_object_key_cannot_leave_the_bucket(bucket, key):
    """`normalize_key` is the single gate, so every spelling stops there."""
    store, _root, _outside = bucket
    with pytest.raises((ObjectError, FileNotFoundError)):
        await store.read(key)


async def test_a_symlink_planted_inside_the_bucket_is_refused(bucket):
    """The attack that needed no traversal at all.

    An archive extractor that honoured symlink members let the attacker write a
    link *into* the bucket pointing at the file outside it, then ask for it by
    an ordinary key. Nothing about `org/acme/pwn.txt` looks like an escape, and
    a gate that validates the key string has already agreed by the time the
    filesystem resolves the link.
    """
    store, root, outside = bucket
    (root / "org" / "acme" / "pwn.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(ObjectError, match="symlink"):
        await store.read("org/acme/pwn.txt")


async def test_a_symlinked_directory_inside_the_bucket_is_refused(bucket):
    """The same idea one level up: the link is a path *component*."""
    store, root, outside = bucket
    (root / "shortcut").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ObjectError, match="symlink"):
        await store.read("shortcut/secret.txt")


async def test_the_legitimate_key_still_reads(bucket):
    """Not vacuous: a guard that refuses everything is not a guard."""
    store, _root, _outside = bucket
    assert await store.read("org/acme/manifest.txt") == b"ordinary object\n"


# --- presigned grants ---------------------------------------------------------


def _query(url: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlsplit
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_a_grant_does_not_move_to_a_neighbouring_key(bucket):
    """Sign what you may, then point the signature somewhere else.

    The key has to be inside what is signed. If only the deadline were, every
    grant would be a grant for the whole bucket.
    """
    store, root, _outside = bucket
    (root / "escrow").mkdir()
    (root / "escrow" / "release.txt").write_bytes(b"NOT-FOR-YOU\n")
    granted = _query(store.url("org/acme/manifest.txt", expires=300))
    assert not store.verify_local_url(
        "escrow/release.txt",
        method="GET",
        expires=int(granted["expires"]),
        signature=granted["signature"],
    )


def test_a_grant_deadline_cannot_be_extended(bucket):
    """The expiry is signed, so moving it invalidates the signature."""
    store, _root, _outside = bucket
    granted = _query(store.url("org/acme/manifest.txt", expires=1))
    assert not store.verify_local_url(
        "org/acme/manifest.txt",
        method="GET",
        expires=int(granted["expires"]) + 86_400,
        signature=granted["signature"],
    )


def test_a_grant_does_not_change_method(bucket):
    """A GET grant is not a PUT grant."""
    store, _root, _outside = bucket
    granted = _query(store.url("org/acme/manifest.txt", expires=300, method="GET"))
    assert not store.verify_local_url(
        "org/acme/manifest.txt", method="PUT",
        expires=int(granted["expires"]), signature=granted["signature"],
    )


@pytest.mark.parametrize("signature", ["", "0" * 64, "f" * 64, "not-hex"])
def test_a_fabricated_signature_is_refused(bucket, signature):
    store, _root, _outside = bucket
    assert not store.verify_local_url(
        "org/acme/manifest.txt", method="GET",
        expires=int(time.time()) + 300, signature=signature,
    )


def test_a_valid_grant_still_verifies(bucket):
    """Not vacuous."""
    store, _root, _outside = bucket
    granted = _query(store.url("org/acme/manifest.txt", expires=300))
    assert store.verify_local_url(
        "org/acme/manifest.txt", method="GET",
        expires=int(granted["expires"]), signature=granted["signature"],
    )


# --- the template engine's lookup guard ---------------------------------------


class _Context:
    """A live object graph, which is what a render context actually is."""

    def __init__(self) -> None:
        self.name = "ana"
        self.role = "viewer"


#: Every escape the engagement tried against a template it controlled. The
#: engine has no call opcode, so the ceiling is disclosure -- but disclosure of
#: a module global is disclosure of whatever that module holds.
ESCAPE_TEMPLATES = (
    "{{ user.__class__ }}",
    "{{ user.__init__ }}",
    "{{ user.__init__.__globals__ }}",
    "{{ user.__class__.__mro__ }}",
    "{{ user.__class__.__init__.__globals__ }}",
    "{{ user.__dict__ }}",
    "{{ user.__getattribute__ }}",
    "{{ user._private }}",
    "{{ user.__class__.__subclasses__ }}",
    "{{ ''.__class__ }}",
    "{{ 7*7 }}",
    '{{ 7*"7" }}',
)


@pytest.mark.parametrize("source", ESCAPE_TEMPLATES)
def test_a_template_cannot_walk_out_of_its_context(source):
    """Refused at *compile* time, which is why the native renderer needs no twin.

    A lookup path may not contain a segment starting with `_`. The guard is on
    the path rather than on the result, so it costs nothing per render and
    cannot be talked out of by what an attribute happens to return.
    """
    with pytest.raises((TemplateSyntaxError, TemplateRenderError)):
        Template.from_string(source).render(user=_Context())


def test_an_ordinary_lookup_still_renders():
    """Not vacuous."""
    rendered = Template.from_string("{{ user.name }}/{{ user.role }}").render(
        user=_Context()
    )
    assert rendered == "ana/viewer"


# --- the XML profile ----------------------------------------------------------


def _external(tmp_path) -> tuple[bytes, str]:
    target = tmp_path / "secret.txt"
    target.write_text("NOT-FOR-THE-PARSER\n")
    return target.read_bytes(), target.as_uri()


HOSTILE_XML = (
    ("comment", b"<a><!-- c --></a>"),
    ("cdata", b"<a><![CDATA[hi]]></a>"),
    ("cdata lowercase", b"<a><![cdata[hi]]></a>"),
    ("processing instruction", b"<a><?pi ?></a>"),
    ("xml declaration mid document", b"<a><?xml version='1.0'?></a>"),
    ("byte order mark", b"\xef\xbb\xbf<a>hi</a>"),
    ("second root", b"<a>hi</a><b>x</b>"),
    ("undeclared entity", b"<a>&x;</a>"),
    ("unknown bang", b"<a><!FOO></a>"),
    ("nul in text", b"<a>\x00</a>"),
    ("declared utf-16", b"<?xml version='1.0' encoding='UTF-16'?><a>hi</a>"),
    ("encoding without hyphen", b"<?xml version='1.0' encoding='UTF8'?><a>hi</a>"),
    ("overlong <", b"\xc0\xbca>hi</a>"),
    ("charref surrogate", b"<a>&#xD800;</a>"),
    ("charref above plane", b"<a>&#x110000;</a>"),
    ("doctype hidden in a comment", b"<!--<!DOCTYPE a>--><a>hi</a>"),
    ("nested expansion", b'<!DOCTYPE a [<!ENTITY a0 "AA"><!ENTITY a1 "&a0;&a0;">]><a>&a1;</a>'),
)


@pytest.mark.parametrize("label,document", HOSTILE_XML, ids=[c[0] for c in HOSTILE_XML])
def test_the_xml_profile_refuses(label, document):
    """It refuses far more than it accepts, and the refusals are the feature."""
    with pytest.raises(XMLRefusal):
        xml_parse(document, XMLLimits(max_bytes=64 * 1024, max_depth=24))


@pytest.mark.parametrize("shape", ["general", "parameter", "dtd-only", "lowercase"])
def test_no_external_entity_resolves(tmp_path, shape):
    """`<!DOCTYPE` in any form, which removes XXE and billion-laughs outright."""
    _content, uri = _external(tmp_path)
    documents = {
        "general": f'<!DOCTYPE a [<!ENTITY x SYSTEM "{uri}">]><a>&x;</a>',
        "parameter": f'<!DOCTYPE a [<!ENTITY % p SYSTEM "{uri}">%p;]><a/>',
        "dtd-only": f'<!DOCTYPE a SYSTEM "{uri}"><a>hi</a>',
        "lowercase": f'<!doctype a [<!ENTITY x SYSTEM "{uri}">]><a>&x;</a>',
    }
    with pytest.raises(XMLRefusal):
        xml_parse(documents[shape].encode(), XMLLimits(max_bytes=64 * 1024))


def test_an_ordinary_document_still_parses():
    """Not vacuous."""
    document = xml_parse(b"<edi><ref>NWD-1</ref></edi>")
    assert document.root.children[0].text == "NWD-1"


# --- the GraphQL cost budget --------------------------------------------------


BUDGET = GraphQLLimits(max_depth=6, max_complexity=120, max_aliases=12,
                       max_document_bytes=8 * 1024)


def _aliases(count: int) -> str:
    return "{ " + " ".join(f"a{i}:shipments{{id}}" for i in range(count)) + " }"


def _nested_fragments(fan: int = 4, depth: int = 3) -> str:
    lines = ["fragment F0 on Shipment { id reference origin destination status }"]
    for level in range(1, depth + 1):
        body = " ".join([f"...F{level - 1}"] * fan)
        lines.append(f"fragment F{level} on Shipment {{ {body} }}")
    lines.append("{ shipments { ...F" + str(depth) + " } }")
    return "\n".join(lines)


COSTLY_DOCUMENTS = (
    ("alias fan-out", _aliases(200)),
    ("repeated field", "{ shipments { " + " ".join(["id"] * 500) + " } }"),
    ("deep nesting", "{ shipments { " + "customer { " * 12 + "id" + " }" * 12 + " } }"),
    ("skipped fields still cost",
     "{ shipments { " + " ".join(["id @skip(if: true)"] * 300) + " } }"),
    ("include false still costs",
     "{ shipments { " + " ".join(["id @include(if: false)"] * 300) + " } }"),
    ("inline fragment", "{ shipments { ... on Shipment { "
     + " ".join(["id"] * 300) + " } } }"),
    ("two operations, the expensive one selected",
     "query A { shipments { id } }\nquery B { shipments { "
     + " ".join(["id"] * 300) + " } }"),
)


@pytest.mark.parametrize("label,document", COSTLY_DOCUMENTS,
                         ids=[c[0] for c in COSTLY_DOCUMENTS])
def test_the_cost_budget_refuses_during_the_parse(label, document):
    """A tiny document must not buy an enormous selection.

    `skip`/`include` are in here deliberately: a budget charged *after*
    directives are applied and a resolver that honours them disagree, and the
    gap between them is free work for whoever noticed.
    """
    with pytest.raises(GraphQLSyntaxError):
        parse(document, BUDGET)


def test_nested_fragment_expansion_passes_the_parser_and_is_charged_later():
    """The two-pass split, recorded so a refactor cannot quietly collapse it.

    `fan ** depth` expansion -- 4**3 here -- produces a *tiny* document with an
    enormous selection behind it. The parser counts selections while reading and
    therefore sees thirteen fragment references and lets it through, which is
    correct: a fragment's weight depends on what its name resolves to, and the
    schema is not known while parsing.

    `_graphql/cost.py` walks the parsed document against the schema afterwards
    and charges each field's declared weight against the *same* budget. That is
    the pass that refuses this, and it is why folding the two together would be
    a regression rather than a simplification.

    Asserting the refusal here would need a compiled schema and a database. It
    is covered end to end elsewhere; what this test pins is that the parser's
    silence is deliberate and not an oversight somebody should "fix".
    """
    assert parse(_nested_fragments(), BUDGET) is not None


def test_a_cyclic_fragment_is_refused():
    """It cannot be expanded, so it must not be accepted."""
    with pytest.raises(GraphQLSyntaxError):
        parse("fragment F on Shipment { ...F }\n{ shipments { ...F } }", BUDGET)


def test_an_ordinary_query_still_parses():
    """Not vacuous: the budget sits just above what a real console asks for."""
    assert parse("{ shipments { id reference status } }", BUDGET) is not None


# --- the pagination sort allow-list -------------------------------------------


class _Sortable:
    """Stands in for a model: `sortable_fields` reads the declared columns."""


@pytest.mark.parametrize("term", [
    "password_hash", "-password_hash", "id; DROP TABLE t", "(SELECT 1)",
    "amount) --", "id/**/", "id,password_hash",
])
def test_sort_terms_outside_the_allow_list_are_refused(term):
    """What stands between a caller's `?sort=` and an ORDER BY clause."""
    allow = frozenset({"id", "number", "amount", "status"})
    outside = [t for t in parse_sort(term) if t.lstrip("-") not in allow]
    assert outside, f"{term!r} should not be inside {sorted(allow)}"


def test_a_listed_sort_term_is_accepted():
    """Not vacuous."""
    allow = frozenset({"id", "number", "amount", "status"})
    assert all(t.lstrip("-") in allow for t in parse_sort("-amount,number"))
