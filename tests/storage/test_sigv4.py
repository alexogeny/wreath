import hashlib
import hmac
import importlib
import importlib.util
import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "wreath"


def _load(name, filename):
    try:
        return importlib.import_module(f"wreath.{name}")
    except ImportError:
        spec = importlib.util.spec_from_file_location(name, _SRC / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


sigv4 = _load("_sigv4", "_sigv4.py")

_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"

# AWS SigV4 "GET / ListUsers" worked example (docs).
_IAM_CR_HASH = "f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59"
_IAM_SIGNATURE = "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"

# AWS S3 "presigned GET object" worked example (docs).
_S3_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_S3_SIGNATURE = "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
_S3_URL = (
    "https://examplebucket.s3.amazonaws.com/test.txt?"
    "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
    "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request&"
    "X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host&"
    "X-Amz-Signature=" + _S3_SIGNATURE
)


def test_iam_listusers_canonical_and_signature():
    headers = {
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "iam.amazonaws.com",
        "x-amz-date": "20150830T123600Z",
    }
    params = [("Action", "ListUsers"), ("Version", "2010-05-08")]
    cr, signed = sigv4.canonical_request("GET", "/", params, headers, sigv4.EMPTY_SHA256)
    assert signed == "content-type;host;x-amz-date"
    assert sigv4.sha256_hex(cr.encode()) == _IAM_CR_HASH
    scope = "20150830/us-east-1/iam/aws4_request"
    sts = sigv4.string_to_sign("20150830T123600Z", scope, cr)
    key = sigv4.signing_key(_SECRET, "20150830", "us-east-1", "iam")
    sig = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
    assert sig == _IAM_SIGNATURE


def test_s3_presign_vector():
    url = sigv4.presign(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        region="us-east-1",
        service="s3",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key=_S3_SECRET,
        amz_date="20130524T000000Z",
        expires=86400,
    )
    assert _S3_SIGNATURE in url
    assert url == _S3_URL


def test_sign_header_auth_includes_content_sha256():
    out = sigv4.sign(
        method="PUT",
        host="b.s3.amazonaws.com",
        path="/k",
        region="us-east-1",
        service="s3",
        access_key="AK",
        secret_key=_S3_SECRET,
        amz_date="20240101T000000Z",
        payload_hash=sigv4.EMPTY_SHA256,
    )
    assert out["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/20240101/us-east-1/s3/")
    assert out["x-amz-content-sha256"] == sigv4.EMPTY_SHA256


# The vectors above pin `canonical_request`, `string_to_sign` and `signing_key`
# against AWS's published examples, but they call those directly. What they cannot
# see is which headers and parameters `sign` assembles before handing them over --
# and AWS publishes no header-auth example that matches, because `sign` always
# signs `x-amz-content-sha256` and the IAM example does not include it. So these
# spell the expected header set out by hand and re-derive the signature from the
# vector-pinned primitives: a header `sign` forgets to sign, or signs and does not
# send, moves the signature and is caught here. Every one of them is a 403 from S3
# whose message names only the mismatch.


def _authorization(
    *,
    method,
    path,
    params,
    headers,
    payload_hash,
    amz_date,
    region,
    service,
    access_key,
    secret_key,
):
    cr, signed = sigv4.canonical_request(method, path, params, headers, payload_hash)
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    sts = sigv4.string_to_sign(amz_date, scope, cr)
    key = sigv4.signing_key(secret_key, amz_date[:8], region, service)
    sig = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
    return (
        f"{sigv4.ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={sig}"
    )


def test_sign_covers_exactly_the_headers_and_params_it_is_given():
    payload_hash = sigv4.sha256_hex(b"body")
    params = [("partNumber", "2"), ("uploadId", "UP")]
    out = sigv4.sign(
        method="PUT",
        host="b.s3.us-east-1.amazonaws.com",
        path="/dir/k.txt",
        region="us-east-1",
        service="s3",
        access_key="AK",
        secret_key=_S3_SECRET,
        amz_date="20240101T000000Z",
        params=params,
        headers={"Content-Type": "text/csv"},
        payload_hash=payload_hash,
        session_token="TOK/EN==",
    )
    expected = _authorization(
        method="PUT",
        path="/dir/k.txt",
        params=params,
        headers={
            "host": "b.s3.us-east-1.amazonaws.com",
            "content-type": "text/csv",
            "x-amz-date": "20240101T000000Z",
            "x-amz-content-sha256": payload_hash,
            "x-amz-security-token": "TOK/EN==",
        },
        payload_hash=payload_hash,
        amz_date="20240101T000000Z",
        region="us-east-1",
        service="s3",
        access_key="AK",
        secret_key=_S3_SECRET,
    )
    assert out["Authorization"] == expected
    assert "x-amz-security-token" in out["Authorization"], out["Authorization"]
    assert out["x-amz-security-token"] == "TOK/EN=="
    assert out["x-amz-content-sha256"] == payload_hash
    assert out["x-amz-date"] == "20240101T000000Z"


def test_sign_without_a_session_token_neither_signs_nor_sends_one():
    out = sigv4.sign(
        method="GET",
        host="b.s3.us-east-1.amazonaws.com",
        path="/k",
        region="us-east-1",
        service="s3",
        access_key="AK",
        secret_key=_S3_SECRET,
        amz_date="20240101T000000Z",
    )
    assert "x-amz-security-token" not in out
    assert "x-amz-security-token" not in out["Authorization"]
    # No payload hash given: an empty body's hash, not the *absence* of the header.
    assert out["x-amz-content-sha256"] == sigv4.EMPTY_SHA256
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date," in out["Authorization"]


def test_an_empty_path_signs_as_the_root():
    kw = dict(
        method="GET",
        host="b.s3.us-east-1.amazonaws.com",
        region="us-east-1",
        service="s3",
        access_key="AK",
        secret_key=_S3_SECRET,
        amz_date="20240101T000000Z",
    )
    assert sigv4.sign(path="", **kw) == sigv4.sign(path="/", **kw)
    cr, _ = sigv4.canonical_request("GET", "", [], {"host": "h"}, sigv4.EMPTY_SHA256)
    assert cr.split("\n")[1] == "/"


def test_presign_carries_the_token_the_extra_params_and_the_signed_headers():
    url = sigv4.presign(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        region="us-east-1",
        service="s3",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key=_S3_SECRET,
        amz_date="20130524T000000Z",
        expires=86400,
        signed_headers={"X-Amz-Acl": "private"},
        extra_params=[("response-content-disposition", "attachment")],
        session_token="TOK/EN==",
    )
    assert "X-Amz-Security-Token=TOK%2FEN%3D%3D" in url
    assert "response-content-disposition=attachment" in url
    # host;x-amz-acl, percent-encoded: the extra header is signed, not dropped.
    assert "X-Amz-SignedHeaders=host%3Bx-amz-acl" in url
    # ... and it changed the signature, so a verifier sees a different request.
    assert _S3_SIGNATURE not in url


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all sigv4 vectors PASS")
