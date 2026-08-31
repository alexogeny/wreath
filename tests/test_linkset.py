from __future__ import annotations

import pytest

from wreath.linkset import LinkContext, Linkset, LinksetResponse, LinkTarget


def test_rfc_9264_simple_linkset_round_trips() -> None:
    document = {
        "linkset": [
            {
                "anchor": "https://example.net/bar",
                "next": [{"href": "https://example.com/foo"}],
            }
        ]
    }
    assert Linkset.from_document(document).document() == document


def test_link_target_serializes_repeatable_hreflang_as_an_array() -> None:
    links = Linkset(
        LinkContext(
            anchor="https://example.net/",
            links={
                "alternate": (
                    LinkTarget(
                        "https://example.net/fr",
                        hreflang=("fr", "fr-CA"),
                        type="text/html",
                    ),
                )
            },
        )
    )
    assert links.document()["linkset"][0]["alternate"] == [
        {
            "href": "https://example.net/fr",
            "hreflang": ["fr", "fr-CA"],
            "type": "text/html",
        }
    ]


def test_linkset_response_uses_the_registered_media_type_and_optional_profile() -> None:
    links = Linkset(LinkContext(anchor="/", links={"next": (LinkTarget("/page/2"),)}))
    plain = LinksetResponse(links)
    profiled = LinksetResponse(links, profile="https://example.net/profile")
    assert dict(plain.headers)[b"content-type"] == b"application/linkset+json"
    assert dict(profiled.headers)[b"content-type"] == (
        b'application/linkset+json; profile="https://example.net/profile"'
    )


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"linkset": [], "extra": True},
        {"linkset": {}},
        {"linkset": [{"next": {"href": "/next"}}]},
        {"linkset": [{"next": [{}]}]},
        {"linkset": [{"next": [{"href": "/next", "hreflang": "en"}]}]},
    ],
)
def test_linkset_parser_refuses_non_rfc_shapes(document) -> None:
    with pytest.raises(ValueError, match="linkset"):
        Linkset.from_document(document)
