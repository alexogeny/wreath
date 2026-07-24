"""SigV4 correctness against AWS's published example vectors.

Standalone-runnable: loads ``_sigv4.py`` by path (it imports only stdlib), so it runs
under ``/usr/bin/python3`` without the built wreath extension — and as a normal pytest.
"""
import hashlib
import hmac
import importlib.util
import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "wreath"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SRC / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sigv4 = _load("wreath_sigv4_standalone", "_sigv4.py")

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
    assert "x-amz-content-sha256" in out["Authorization"].lower() or out["x-amz-content-sha256"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all sigv4 vectors PASS")
