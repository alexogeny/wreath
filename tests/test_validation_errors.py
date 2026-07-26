"""Validation-error shaping: formatters, catalogues, and Accept-Language."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wreath import Wreath
from wreath.response import ProblemDetail
from wreath.testing import TestClient
from wreath.validation_errors import (
    MessageCatalogue,
    catalogue_formatter,
    field_names,
    select_language,
)


@dataclass
class NewItem:
    name: str
    count: int


def _app() -> Wreath:
    app = Wreath()

    @app.post("/items")
    async def create(request: Any, item: NewItem) -> dict:
        return {"ok": True}

    return app


# --- select_language ---------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "offered", "expected"),
    [
        (None, ["en", "fr"], "en"),                     # absent -> fallback
        ("", ["en", "fr"], "en"),                       # empty -> fallback
        ("fr", ["en", "fr"], "fr"),
        ("fr-CA", ["en", "fr"], "fr"),                  # prefix match
        ("fr", ["en", "fr-CA"], "fr-CA"),               # reverse prefix match
        ("de", ["en", "fr"], "en"),                     # no match -> fallback
        ("de,fr;q=0.8", ["en", "fr"], "fr"),
        ("fr;q=0.2,en;q=0.9", ["en", "fr"], "en"),      # highest q wins
        ("*", ["en", "fr"], "en"),                      # wildcard -> first
        ("en;q=0,*", ["en", "fr"], "fr"),               # refused, wildcard next
        ("en;q=0", ["en", "fr"], "en"),                 # refused, no wildcard
        ("FR", ["en", "fr"], "fr"),                     # case-insensitive
        (b"fr", ["en", "fr"], "fr"),                    # bytes header
        ("!!!;;;", ["en", "fr"], "en"),                 # garbage -> fallback
        ("fr;q=bad", ["en", "fr"], "en"),               # unparseable q -> refused
    ],
)
def test_select_language(header, offered, expected) -> None:
    assert select_language(header, offered) == expected


def test_select_language_requires_an_offer() -> None:
    with pytest.raises(ValueError, match="at least one"):
        select_language("en", [])


# --- catalogue ---------------------------------------------------------------


def test_a_catalogue_falls_back_across_languages_then_to_the_validator() -> None:
    catalogue = MessageCatalogue({
        "en": {"missing": "Required.", "int": "Whole number."},
        "fr": {"missing": "Obligatoire."},
    })
    assert catalogue.message("fr", "missing", "x") == "Obligatoire."
    # Missing in fr -> the default language's message.
    assert catalogue.message("fr", "int", "x") == "Whole number."
    # Missing everywhere -> the validator's own message survives.
    assert catalogue.message("fr", "unheard_of", "original") == "original"


def test_an_empty_catalogue_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one language"):
        MessageCatalogue({})


def test_field_names_reads_the_last_location_segment() -> None:
    errors = [{"loc": ["body", "name"]}, {"loc": ["query", "page"]}, {"loc": []}]
    assert field_names(errors) == ["name", "page", ""]


# --- end to end --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_default_output_is_unchanged_without_a_formatter() -> None:
    client = TestClient(_app())
    response = await client.post("/items", json={"name": "x"})

    assert response.status == 422
    body = response.json()
    assert body["title"] == "Unprocessable Content"
    assert body["detail"] == "Request validation failed"
    assert body["errors"][0]["type"] == "missing"
    assert "language" not in body          # nothing added when unconfigured


@pytest.mark.asyncio
async def test_a_catalogue_formatter_translates_on_type() -> None:
    catalogue = MessageCatalogue({
        "en": {"missing": "This field is required."},
        "fr": {"missing": "Ce champ est obligatoire."},
    })
    app = _app()
    app.set_validation_formatter(catalogue_formatter(catalogue))
    client = TestClient(app)

    english = await client.post("/items", json={"name": "x"})
    assert english.status == 422
    assert english.json()["errors"][0]["msg"] == "This field is required."
    assert english.json()["language"] == "en"

    french = await client.post(
        "/items", json={"name": "x"}, headers={"accept-language": "fr"}
    )
    assert french.json()["errors"][0]["msg"] == "Ce champ est obligatoire."
    assert french.json()["language"] == "fr"
    # The machine-readable type is never translated.
    assert french.json()["errors"][0]["type"] == "missing"


@pytest.mark.asyncio
async def test_aliases_rename_the_field_in_the_error_location() -> None:
    catalogue = MessageCatalogue({"en": {"missing": "Required."}})
    app = _app()
    app.set_validation_formatter(
        catalogue_formatter(catalogue, aliases={"count": "itemCount"})
    )
    response = await TestClient(app).post("/items", json={"name": "x"})

    assert response.json()["errors"][0]["loc"][-1] == "itemCount"


@pytest.mark.asyncio
async def test_a_custom_formatter_owns_the_whole_problem_document() -> None:
    def flat(errors: list[dict[str, Any]], request: Any) -> ProblemDetail:
        return ProblemDetail(
            status=422,
            title="Bad input",
            detail=f"{len(errors)} problem(s)",
            extensions={"fields": field_names(errors)},
        )

    app = _app()
    app.set_validation_formatter(flat)
    response = await TestClient(app).post("/items", json={})

    body = response.json()
    assert response.status == 422
    assert body["title"] == "Bad input"
    assert sorted(body["fields"]) == ["count", "name"]
    assert "errors" not in body


@pytest.mark.asyncio
async def test_a_formatter_can_be_removed_again() -> None:
    app = _app()
    app.set_validation_formatter(catalogue_formatter(MessageCatalogue({"en": {}})))
    app.set_validation_formatter(None)

    body = (await TestClient(app).post("/items", json={"name": "x"})).json()
    assert body["detail"] == "Request validation failed"
    assert "language" not in body


@pytest.mark.asyncio
async def test_an_unparseable_accept_language_still_answers() -> None:
    catalogue = MessageCatalogue({"en": {"missing": "Required."}, "fr": {}})
    app = _app()
    app.set_validation_formatter(catalogue_formatter(catalogue))

    response = await TestClient(app).post(
        "/items", json={"name": "x"}, headers={"accept-language": ";;;q=@@@"}
    )
    assert response.status == 422
    assert response.json()["language"] == "en"
