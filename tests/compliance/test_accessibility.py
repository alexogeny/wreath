from __future__ import annotations

import pytest

from wreath._audit.dom import parse_html
from wreath._audit.rules import A11Y_RULES


def _findings(html: str):
    root = parse_html(html)
    out = []
    for rule in A11Y_RULES:
        out.extend(rule(root, "compliance"))
    return out


def _fired(html: str) -> set[str]:
    return {f.rule_id for f in _findings(html)}


def test_1_1_1_non_text_content() -> None:
    assert "img-alt" in _fired("<img src=a.png>")
    assert "img-alt" in _fired('<input type="image" src=go.png>')
    assert "img-alt" in _fired('<map><area href="/a"></map>')
    assert _fired('<img src=a.png alt=""><input type=image src=go alt=Go>') == set()


def test_3_1_1_language_of_page() -> None:
    assert "html-lang" in _fired("<html><head><title>t</title></head><body></body></html>")


def test_2_4_2_page_titled() -> None:
    assert "document-title" in _fired("<html><body>x</body></html>")


def test_3_3_2_labels_or_instructions() -> None:
    assert "control-label" in _fired("<input type=text name=q>")
    assert "control-label" not in _fired("<label>Q <input type=text name=q></label>")


def test_4_1_2_name_role_value() -> None:
    assert "aria-valid" in _fired('<div role="notarole">x</div>')
    assert "aria-valid" not in _fired('<div role="button">x</div>')
    assert "duplicate-id" in _fired("<p id=x>a</p><p id=x>b</p>")
    assert "frame-title" in _fired("<iframe src=x></iframe>")
    assert "frame-title" not in _fired('<iframe src=x title="Report"></iframe>')


def test_1_3_1_info_and_relationships() -> None:
    assert "table-headers" in _fired("<table><tr><td>a</td></tr></table>")


def test_2_2_1_timing_adjustable() -> None:
    assert "meta-refresh" in _fired('<meta http-equiv="refresh" content="30">')


def test_2_4_4_link_purpose() -> None:
    assert "link-text" in _fired('<a href="/x">click here</a>')


def test_1_4_2_audio_control() -> None:
    assert "autoplay" in _fired("<video autoplay src=v.mp4>")
    assert "autoplay" not in _fired("<video autoplay muted src=v.mp4>")


def test_1_4_4_resize_text() -> None:
    assert "viewport-scale" in _fired(
        '<meta name=viewport content="width=device-width, user-scalable=no">'
    )


def test_2_4_3_focus_order() -> None:
    assert "tabindex" in _fired('<div tabindex="3">x</div>')


def test_2_4_7_focus_visible() -> None:
    assert "focus-visible" in _fired("<style>:focus{outline:none}</style>")
    assert "focus-visible" in _fired('<button style="outline:0">x</button>')


def test_1_4_11_non_text_contrast() -> None:
    # A form-control border below 3:1 fails; a decorative border is not flagged.
    low = "<style>:root{background:#ffffff} input{border-color:#dddddd}</style>"
    assert "non-text-contrast" in _fired(low)
    ok = "<style>:root{background:#ffffff} input{border-color:#595959}</style>"
    assert "non-text-contrast" not in _fired(ok)
    deco = "<style>:root{background:#ffffff} div.card{border-color:#dddddd}</style>"
    assert "non-text-contrast" not in _fired(deco)


@pytest.mark.parametrize(
    "markup",
    [
        '<th aria-sort="ascending">N</th>',
        '<span role="code">x</span>',
        '<div role="meter" aria-valuenow="1" aria-valuemin="0" aria-valuemax="2"></div>',
    ],
)
def test_valid_aria_1_2_is_not_false_flagged(markup: str) -> None:
    # A narrow allow-list would report conformant ARIA as broken (WCAG 4.1.2).
    assert "aria-valid" not in _fired(markup)
