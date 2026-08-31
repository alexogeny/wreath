from __future__ import annotations

from typing import cast

import pytest

from wreath.link_template import LinkTemplate, serialize_link_templates
from wreath.response import FileResponse, Response, StreamingResponse


def test_link_template_serializes_rfc_9652_examples() -> None:
    assert LinkTemplate("/{username}", rel="item").to_header() == (b'"/{username}";rel="item"')
    assert serialize_link_templates(
        [
            LinkTemplate("/books/{book_id}/author", rel="author", anchor="#{book_id}"),
            LinkTemplate(
                "/widgets/{widget_id}",
                rel="https://example.org/rel/widget",
                var_base="https://example.org/vars/",
            ),
        ]
    ) == (
        b'"/books/{book_id}/author";rel="author";anchor="#{book_id}", '
        b'"/widgets/{widget_id}";rel="https://example.org/rel/widget";'
        b'var-base="https://example.org/vars/"'
    )


def test_link_template_uses_display_strings_for_unicode_attributes() -> None:
    template = LinkTemplate(
        "/author",
        rel="author",
        attributes={
            "title": (
                "Bj\N{LATIN SMALL LETTER O WITH DIAERESIS}rn "
                "J\N{LATIN SMALL LETTER A WITH DIAERESIS}rnsida"
            )
        },
    )

    assert template.to_header() == (b'"/author";rel="author";title=%"Bj%c3%b6rn J%c3%a4rnsida"')


def test_link_template_response_setter_replaces_existing_fields() -> None:
    response = Response(
        b"ok",
        headers=[
            (b"link-template", b'"/old/{id}";rel="item"'),
            (b"Link-Template", b'"/older/{id}";rel="item"'),
        ],
    )

    response.set_link_templates(
        LinkTemplate("/items/{id}", rel="item"),
        LinkTemplate("/items{?cursor}", rel="collection"),
    )

    assert [value for name, value in response.headers if name.lower() == b"link-template"] == [
        b'"/items/{id}";rel="item", "/items{?cursor}";rel="collection"'
    ]


def test_streaming_response_supports_link_templates() -> None:
    async def chunks():
        yield b"event"

    response = StreamingResponse(chunks())
    response.set_link_templates(LinkTemplate("/events{?after}", rel="next"))

    assert (b"link-template", b'"/events{?after}";rel="next"') in response.headers


def test_file_response_supports_link_templates() -> None:
    response = FileResponse("asset.bin")
    response.set_link_templates(LinkTemplate("/assets/{name}", rel="item"))

    assert (b"link-template", b'"/assets/{name}";rel="item"') in response.headers


@pytest.mark.parametrize(
    "template",
    [
        "{var}",
        "{+path}/here",
        "{#x,hello,y}",
        "X{.var}",
        "{/var,x}",
        "{;x,y,empty}",
        "{?x,y}",
        "{&x,y,empty}",
        "{var:3}",
        "{list*}",
        "{keys*}",
        "{?keys*}",
    ],
)
def test_link_template_accepts_every_rfc_6570_expression_level(template: str) -> None:
    assert LinkTemplate(template).to_header().startswith(b'"')


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("/items/{", "closing"),
        ("/items/{}", "variable"),
        ("/items/{id:00001}", "prefix"),
        ("/items/{id**}", "modifier"),
        ("/items/{id%zz}", "percent"),
        ("/caf\N{LATIN SMALL LETTER E WITH ACUTE}/{id}", "percent-encode"),
    ],
)
def test_link_template_refuses_invalid_uri_templates(template: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LinkTemplate(template)


def test_link_template_refuses_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="parameter name"):
        LinkTemplate("/{id}", attributes={"Bad_Name": "value"})
    with pytest.raises(TypeError, match="attribute 'title'.*str"):
        LinkTemplate("/{id}", attributes={"title": cast(str, 1)})
    with pytest.raises(ValueError, match="rel must not be empty"):
        LinkTemplate("/{id}", rel="")


def test_link_template_refuses_an_empty_response_field() -> None:
    response = Response(b"ok")

    with pytest.raises(ValueError, match="at least one"):
        response.set_link_templates()
