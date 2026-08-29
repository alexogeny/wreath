from __future__ import annotations

import pytest

from wreath._client_codec import response_framing, response_keeps_alive


def test_a_plain_content_length_frames_the_body() -> None:
    assert response_framing("GET", 200, [(b"content-length", b"12")]) == ("length", 12)


def test_chunked_frames_the_body() -> None:
    assert response_framing("GET", 200, [(b"transfer-encoding", b"chunked")]) == (
        "chunked",
        -1,
    )


def test_no_framing_header_means_the_body_ends_at_close() -> None:
    assert response_framing("GET", 200, [(b"server", b"nginx")]) == ("close", -1)


def test_only_connection_headers_can_control_reuse() -> None:
    assert response_keeps_alive(
        1,
        [(b"x-connection-note", b"close"), (b"connection", b"keep-alive")],
        True,
    )


def test_reuse_requires_complete_framing_and_http_10_opt_in() -> None:
    assert not response_keeps_alive(1, [], False)
    assert not response_keeps_alive(0, [], True)
    assert response_keeps_alive(0, [(b"connection", b"keep-alive")], True)


@pytest.mark.parametrize(
    ("method", "status"),
    [("HEAD", 200), ("GET", 204), ("GET", 304)],
    ids=["head", "no-content", "not-modified"],
)
def test_a_bodiless_response_ignores_its_framing_headers(method: str, status: int) -> None:
    assert response_framing(
        method, status, [(b"content-length", b"12"), (b"transfer-encoding", b"chunked")]
    ) == ("none", 0)


def test_transfer_encoding_beside_content_length_is_refused() -> None:
    with pytest.raises(ValueError, match="conflicting transfer-encoding and content-length"):
        response_framing(
            "GET", 200, [(b"content-length", b"5"), (b"transfer-encoding", b"chunked")]
        )


def test_two_disagreeing_content_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="conflicting content-length values"):
        response_framing("GET", 200, [(b"content-length", b"5"), (b"content-length", b"7")])


def test_two_agreeing_content_lengths_are_accepted() -> None:
    assert response_framing("GET", 200, [(b"content-length", b"5"), (b"content-length", b"5")]) == (
        "length",
        5,
    )


@pytest.mark.parametrize(
    "value",
    [b"", b"abc", b"-1", b"5x", b" 5"],
    ids=["empty", "letters", "negative", "trailing", "leading-space"],
)
def test_a_content_length_that_is_not_digits_is_refused(value: bytes) -> None:
    with pytest.raises(ValueError, match="invalid response content-length"):
        response_framing("GET", 200, [(b"content-length", value)])


def test_chunked_must_be_the_last_transfer_coding() -> None:
    with pytest.raises(ValueError, match="unsupported response transfer-encoding"):
        response_framing("GET", 200, [(b"transfer-encoding", b"chunked, gzip")])


def test_chunked_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported response transfer-encoding"):
        response_framing("GET", 200, [(b"transfer-encoding", b"chunked, chunked")])


def test_chunked_split_across_two_headers_is_still_read_as_one_list() -> None:
    assert response_framing(
        "GET",
        200,
        [(b"transfer-encoding", b"gzip"), (b"transfer-encoding", b"chunked")],
    ) == ("chunked", -1)


def test_a_transfer_encoding_not_ending_in_chunked_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported response transfer-encoding"):
        response_framing("GET", 200, [(b"transfer-encoding", b"gzip")])


def test_transfer_coding_names_are_case_insensitive_and_may_carry_spaces() -> None:
    assert response_framing("GET", 200, [(b"transfer-encoding", b" Chunked ")]) == (
        "chunked",
        -1,
    )
