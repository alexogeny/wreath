import pytest

from wreath import _pgdriver


@pytest.mark.parametrize("binary", [False, True])
def test_bind_does_not_concatenate_encoded_value_with_temporary_prefix(monkeypatch, binary):
    copied = []

    class Encoded(bytes):
        def __radd__(self, prefix):
            copied.append(len(self))
            return bytes(prefix) + bytes(self)

    encoded = Encoded(b"x" * 10_000)
    monkeypatch.setattr(
        _pgdriver, "_encode_binary" if binary else "_encode_text", lambda value, oid: encoded
    )
    payload = _pgdriver._bind_payload(
        b"s", (None,), (17,), binary_parameters=binary, binary_results=True
    )
    assert encoded in payload
    assert copied == []


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("binary_results", [False, True])
def test_bind_packet_bytes_match_independent_field_encoding(binary, binary_results):
    values = (None, "", "café", b"\0\xff", 42)
    oids = (25, 25, 25, 17, 23)
    encoded = [None, b"", "café".encode(), b"\0\xff" if binary else b"\\x00ff"]
    encoded.append((42).to_bytes(4, "big", signed=True) if binary else b"42")
    expected = bytearray(b"\0s\0")
    expected.extend(b"\0\5" + b"\0\1" * 5 if binary else b"\0\0")
    expected.extend(b"\0\5")
    for value in encoded:
        if value is None:
            expected.extend(b"\xff" * 4)
        else:
            expected.extend(len(value).to_bytes(4, "big"))
            expected.extend(value)
    expected.extend(b"\0\1\0\1" if binary_results else b"\0\0")
    actual = _pgdriver._bind_payload(
        b"s", values, oids, binary_parameters=binary, binary_results=binary_results
    )
    assert actual == bytes(expected)


@pytest.mark.parametrize("binary", [False, True])
def test_bind_preserves_codec_and_argument_count_errors(binary):
    message = "int4 codec requires int" if binary else "integer codec requires int"
    with pytest.raises(TypeError, match=message):
        _pgdriver._bind_payload(
            b"s", ("bad",), (23,), binary_parameters=binary, binary_results=False
        )
    with pytest.raises(_pgdriver.InterfaceError, match="argument count"):
        _pgdriver._bind_payload(b"s", (1,), (), binary_parameters=binary, binary_results=False)
