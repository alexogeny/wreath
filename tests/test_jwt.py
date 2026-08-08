"""JWT verification: correctness and the adversarial/negative suite.

HS* tokens are built with the stdlib (no external dependency). RS*/PS* tokens are
signed with ``cryptography`` (a dev dependency) as an external oracle — exactly
the arrangement the design mandates for a security component. This is the
first leg of the eventual three-legged harness; the review fork should add
Project Wycheproof vectors before production trust.

NOTE FOR THE REVIEW FORK: these tests exercise the Python facade + stdlib
fallbacks. To also cover the native jose.c path they must run once with the
compiled _core present (default) and once with WREATH_PURE=1.
"""

from __future__ import annotations

import base64
import hmac
import json
import time

import pytest

import wreath._auth.jwt as jwt_module
import wreath._b64 as b64_module
from wreath.auth import (
    JwtVerifier,
    RsaPublicKey,
    SymmetricKey,
    UnsupportedAlgorithm,
    key_from_pem,
)

SECRET = b"a-shared-secret-of-reasonable-length"
_HS_DIGEST = {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segments(header: dict, claims: dict) -> tuple[str, str, bytes]:
    hb = _b64u(json.dumps(header).encode())
    pb = _b64u(json.dumps(claims).encode())
    return hb, pb, f"{hb}.{pb}".encode("ascii")


def _hs(claims: dict, *, secret: bytes = SECRET, alg: str = "HS256",
        header_extra: dict | None = None) -> str:
    header = {"alg": alg, "typ": "JWT", **(header_extra or {})}
    hb, pb, signing_input = _segments(header, claims)
    sig = hmac.new(secret, signing_input, _HS_DIGEST[alg]).digest()
    return f"{hb}.{pb}.{_b64u(sig)}"


def _claims(**overrides) -> dict:
    base = {"sub": "user-123", "exp": int(time.time()) + 3600,
            "iss": "https://issuer.example", "aud": "my-api"}
    base.update(overrides)
    return base


def _verifier(**overrides) -> JwtVerifier:
    kwargs = dict(
        algorithms=("HS256",),
        key=SymmetricKey(SECRET),
        issuer="https://issuer.example",
        audience="my-api",
        leeway=0,
    )
    kwargs.update(overrides)
    return JwtVerifier(**kwargs)


def test_the_pure_base64url_fallback_rejects_discarded_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stdlib silently discards ``!`` unless Wreath checks first.

    Patched on `wreath._b64` rather than on this module: the decoder lives
    there now, and `wreath._auth.jwt` imports it. It used to be a second copy
    here with its own native binding, and the two were identical -- same cap,
    same alphabet, same message.
    """
    monkeypatch.setattr(b64_module, "_native_b64url", None)

    with pytest.raises(ValueError, match="invalid base64url"):
        jwt_module._b64url_decode("YWJj!")


def test_the_pure_compact_fallback_enforces_the_total_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jwt_module, "_native_parse", None)

    with pytest.raises(ValueError, match="compact JWT exceeds maximum size"):
        jwt_module._parse_compact("a" * (jwt_module._MAX_TOKEN_BYTES + 1))


@pytest.mark.parametrize("value", ["soon", 1.5, True])
def test_the_pure_claim_fallback_refuses_non_integer_times(
    monkeypatch: pytest.MonkeyPatch, value: object,
) -> None:
    monkeypatch.setattr(jwt_module, "_native_claims", None)

    reason = jwt_module._reason_valid(
        {"exp": value},
        now=int(time.time()),
        leeway=0,
        issuer=None,
        audiences=(),
        required=(),
    )

    assert reason == 7


# ---- happy path -----------------------------------------------------------


def test_valid_hs256_returns_identity():
    identity = _verifier()(_hs(_claims(roles=["admin", "ops"])))
    assert identity is not None
    assert identity.id == "user-123"
    assert identity.roles == frozenset({"admin", "ops"})


def test_cognito_groups_map_to_roles():
    token = _hs(_claims(**{"cognito:groups": ["fleet-admin"]}))
    identity = _verifier()(token)
    assert identity is not None
    assert identity.roles == frozenset({"fleet-admin"})


# ---- adversarial / negative ----------------------------------------------


def test_alg_none_is_rejected():
    # A classic downgrade: alg "none" with an empty signature.
    hb, pb, _ = _segments({"alg": "none", "typ": "JWT"}, _claims())
    assert _verifier()(f"{hb}.{pb}.") is None
    # ...and even with junk in the signature slot.
    assert _verifier()(f"{hb}.{pb}.{_b64u(b'junk')}") is None


def test_unlisted_algorithm_is_rejected():
    # Token signed HS384 but the verifier only allows HS256.
    assert _verifier(algorithms=("HS256",))(_hs(_claims(), alg="HS384")) is None


def test_rs_hs_key_confusion_is_rejected():
    # Attack: sign an HS256 token using the RSA *public key* bytes as the HMAC
    # secret and present it to an RS256 verifier. The token's alg (HS256) is not
    # in the RS256 allow-list, so it never reaches a verify.
    pub_pem = b"-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----"
    verifier = JwtVerifier(
        algorithms=("RS256",),
        key=RsaPublicKey(n=0xC0FFEE, e=65537),
        issuer="https://issuer.example",
        audience="my-api",
    )
    forged = _hs(_claims(), secret=pub_pem, alg="HS256")
    assert verifier(forged) is None


def test_tampered_signature_is_rejected():
    token = _hs(_claims())
    head, _, _sig = token.rpartition(".")
    assert _verifier()(f"{head}.{_b64u(b'0' * 32)}") is None


def test_wrong_secret_is_rejected():
    assert _verifier()(_hs(_claims(), secret=b"the-wrong-secret")) is None


def test_expired_token_is_rejected():
    assert _verifier()(_hs(_claims(exp=int(time.time()) - 10))) is None


def test_not_yet_valid_token_is_rejected():
    assert _verifier()(_hs(_claims(nbf=int(time.time()) + 3600))) is None


def test_wrong_audience_is_rejected():
    assert _verifier()(_hs(_claims(aud="some-other-api"))) is None


def test_wrong_issuer_is_rejected():
    assert _verifier()(_hs(_claims(iss="https://evil.example"))) is None


def test_missing_sub_is_rejected():
    claims = _claims()
    del claims["sub"]
    assert _verifier()(_hs(claims)) is None


@pytest.mark.parametrize("token", ["", "a", "a.b", "a.b.c.d", "....", "not-a-jwt"])
def test_malformed_tokens_are_rejected(token):
    assert _verifier()(token) is None


def test_oversized_token_is_rejected():
    huge = _hs(_claims(blob="x" * (200 * 1024)))
    assert _verifier()(huge) is None


# ---- construction-time guards --------------------------------------------


def test_symmetric_key_rejects_rsa_algorithm():
    with pytest.raises(UnsupportedAlgorithm):
        JwtVerifier(algorithms=("RS256",), key=SymmetricKey(SECRET))


def test_es256_is_a_loud_unsupported_error():
    with pytest.raises(UnsupportedAlgorithm):
        JwtVerifier(algorithms=("ES256",), key=SymmetricKey(SECRET))


def test_eddsa_is_a_loud_unsupported_error():
    with pytest.raises(UnsupportedAlgorithm):
        JwtVerifier(algorithms=("EdDSA",), key=SymmetricKey(SECRET))


# ---- RSA via the cryptography oracle -------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, pem


def _rs_token(private, claims: dict, *, alg: str = "RS256") -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    hashes_by_alg = {
        "RS256": hashes.SHA256, "RS384": hashes.SHA384, "RS512": hashes.SHA512,
        "PS256": hashes.SHA256, "PS384": hashes.SHA384, "PS512": hashes.SHA512,
    }
    algo = hashes_by_alg[alg]()
    header = {"alg": alg, "typ": "JWT"}
    hb, pb, signing_input = _segments(header, claims)
    if alg.startswith("PS"):
        pad = padding.PSS(mgf=padding.MGF1(algo), salt_length=algo.digest_size)
    else:
        pad = padding.PKCS1v15()
    sig = private.sign(signing_input, pad, algo)
    return f"{hb}.{pb}.{_b64u(sig)}"


@pytest.mark.parametrize("alg", ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512"])
def test_rsa_valid_token_verifies(rsa_keypair, alg):
    private, pem = rsa_keypair
    verifier = JwtVerifier(
        algorithms=(alg,), key=key_from_pem(pem),
        issuer="https://issuer.example", audience="my-api", leeway=0,
    )
    identity = verifier(_rs_token(private, _claims(), alg=alg))
    assert identity is not None
    assert identity.id == "user-123"


def test_rsa_tampered_signature_is_rejected(rsa_keypair):
    private, pem = rsa_keypair
    verifier = JwtVerifier(
        algorithms=("RS256",), key=key_from_pem(pem),
        issuer="https://issuer.example", audience="my-api",
    )
    token = _rs_token(private, _claims(), alg="RS256")
    head, _, _sig = token.rpartition(".")
    bad_sig = _b64u(b"\x00" * 256)
    assert verifier(f"{head}.{bad_sig}") is None


def test_pem_and_jwk_agree(rsa_keypair):
    private, pem = rsa_keypair
    from wreath.auth import key_from_jwk

    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "n": _b64u(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64u(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }
    from_pem = key_from_pem(pem)
    from_jwk = key_from_jwk(jwk)
    assert (from_pem.n, from_pem.e) == (from_jwk.n, from_jwk.e)


# --- JWKS document robustness ------------------------------------------------


def _jwks_cache(document: dict):
    """A `JwksCache` wired to a client that serves `document` once."""
    import json as _json

    from wreath._auth.jwks import JwksCache

    class _Response:
        def __init__(self, body):
            self.status = 200
            self.body = body

        def header(self, name, default=None):
            return default

    class _Client:
        def __init__(self, body):
            self._body = body

        async def get(self, path):
            return _Response(self._body)

    return JwksCache(
        http_client=_Client(_json.dumps(document).encode()), jwks_path="/jwks"
    )


async def test_a_non_object_in_keys_does_not_discard_the_rest():
    """One junk entry must not cost every key after it.

    `jwk.get("use")` ran before the try, so a string in `keys` raised
    `AttributeError` out of the whole refresh -- the blanket catch below it
    started one line too late. Every valid key later in the document was lost,
    and the failure looked like a provider that had stopped serving keys.
    """
    cache = _jwks_cache(
        {"keys": ["junk", 42, {"kty": "oct", "k": "AAAA", "kid": "good"}]}
    )
    await cache.prefetch()

    assert await cache.resolve("good") is not None, "a valid key was discarded"
    assert cache.malformed_keys == 2


async def test_a_malformed_jwk_is_counted_not_silent():
    """A provider serving junk shows up as a number, not as keys that vanish."""
    cache = _jwks_cache(
        {
            "keys": [
                {"kty": "RSA", "n": "###", "e": "AQAB"},  # bad base64url
                {"kty": "EC", "crv": "P-521"},  # unsupported curve
                {"kty": "oct"},  # missing "k"
                {"kty": "oct", "k": "AAAA", "kid": "good"},
            ]
        }
    )
    await cache.prefetch()

    assert await cache.resolve("good") is not None
    assert cache.malformed_keys == 3


# --- RSA: the refusing side, which neither family had ---------------------------
#
# A mutation sweep reported `_verify_ps` as replaceable, whole, by `return True`:
# every internal check -- signature length, `s >= n`, the 0xbc trailer, the top-bits
# mask, the DB padding, the 0x01 separator, the salt slice -- could be deleted and the
# suite stayed green. `test_rsa_valid_token_verifies` parametrises PS256/384/512 but
# only over tokens that *should* verify, and the one tampered-signature test covers
# RS256 alone. So RSA-PSS had no negative test at all: a verifier that accepted any
# signature would have passed CI, and forging a token needs no key at that point.
#
# The code turned out to be correct -- every forgery below was already refused. What
# was missing was the proof, which is the part a regression needs.


def _rsa_verifier(pem: bytes, alg: str) -> JwtVerifier:
    return JwtVerifier(
        algorithms=(alg,), key=key_from_pem(pem),
        issuer="https://issuer.example", audience="my-api", leeway=0,
    )


def _resign(token: str, signature: bytes) -> str:
    """Same header and payload, a different signature."""
    head, _, _ = token.rpartition(".")
    return f"{head}.{_b64u(signature)}"


_RSA_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512"]


@pytest.mark.parametrize("alg", _RSA_ALGORITHMS)
def test_every_rsa_algorithm_rejects_a_flipped_bit(rsa_keypair, alg):
    """One bit of a genuine signature, for every algorithm rather than just RS256.

    This is the mutant that matters most: it is the only assertion that fails if the
    verifier is replaced by `return True`, and until now five of the six algorithms
    had nothing making that claim.
    """
    private, pem = rsa_keypair
    token = _rs_token(private, _claims(), alg=alg)
    _, _, signature = token.rpartition(".")
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    flipped = bytes([raw[0] ^ 0x01]) + raw[1:]

    assert _rsa_verifier(pem, alg)(token) is not None       # the genuine one still passes
    assert _rsa_verifier(pem, alg)(_resign(token, flipped)) is None


@pytest.mark.parametrize("alg", _RSA_ALGORITHMS)
def test_a_signature_over_different_content_is_rejected(rsa_keypair, alg):
    """A real signature, made with the real key, over something else.

    Distinct from a flipped bit: this one is *well formed* all the way through the
    padding checks and fails only on the final comparison, so it is what proves the
    verifier reads the signing input rather than merely parsing the envelope.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    private, pem = rsa_keypair
    algo = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}[alg[2:]]()
    pad = (
        padding.PSS(mgf=padding.MGF1(algo), salt_length=algo.digest_size)
        if alg.startswith("PS")
        else padding.PKCS1v15()
    )
    elsewhere = private.sign(b"a different signing input entirely", pad, algo)
    token = _rs_token(private, _claims(), alg=alg)
    assert _rsa_verifier(pem, alg)(_resign(token, elsewhere)) is None


@pytest.mark.parametrize("alg", _RSA_ALGORITHMS)
@pytest.mark.parametrize("delta", [-1, 1], ids=["one-short", "one-long"])
def test_a_signature_of_the_wrong_length_is_refused(rsa_keypair, alg, delta):
    """`len(signature) != k`, from both sides.

    A modular exponentiation on a wrong-length buffer is not a verification, and
    without the check `int.from_bytes` would happily consume it.
    """
    private, pem = rsa_keypair
    token = _rs_token(private, _claims(), alg=alg)
    assert _rsa_verifier(pem, alg)(_resign(token, b"\x00" * (256 + delta))) is None


@pytest.mark.parametrize("alg", _RSA_ALGORITHMS)
def test_a_signature_numerically_at_the_modulus_is_refused(rsa_keypair, alg):
    """`s >= key.n` — right length, out of range.

    RSA is arithmetic modulo `n`, so `s` and `s + n` exponentiate identically. Without
    this check a signature would have unboundedly many accepted spellings, which is
    the same class of bug as accepting base64 padding on a segment: one token, many
    strings, and any deployment that blocklists a leaked token by its value loses.
    """
    private, pem = rsa_keypair
    modulus = private.public_key().public_numbers().n
    token = _rs_token(private, _claims(), alg=alg)
    at_modulus = modulus.to_bytes(256, "big")
    assert _rsa_verifier(pem, alg)(_resign(token, at_modulus)) is None


@pytest.mark.parametrize(
    ("signed_as", "verified_as"),
    [("RS256", "PS256"), ("PS256", "RS256")],
    ids=["pkcs1v15-signature-as-pss", "pss-signature-as-pkcs1v15"],
)
def test_the_two_rsa_paddings_are_not_interchangeable(rsa_keypair, signed_as, verified_as):
    """A genuine signature from the genuine key, under the other padding scheme.

    Both families share a key type and a modulus, so nothing structural stops a
    PKCS#1 v1.5 signature being offered where PSS is expected. If the verifier
    dispatched on the key rather than on `alg`, this would pass — and an attacker who
    could get one signature could present it under whichever scheme was weaker.
    """
    private, pem = rsa_keypair
    token = _rs_token(private, _claims(), alg=signed_as)
    head, _, signature = token.rpartition(".")
    header, payload = head.split(".")
    # Re-label the header as the other algorithm, keeping the original signature.
    relabelled = _b64u(json.dumps({"alg": verified_as, "typ": "JWT"}).encode())
    forged = f"{relabelled}.{payload}.{signature}"
    assert _rsa_verifier(pem, verified_as)(forged) is None


@pytest.mark.parametrize("alg", ["PS256", "PS384", "PS512"])
@pytest.mark.parametrize(
    "filler",
    [b"\x00", b"\xff", b"\x01", b"\xbc"],
    ids=["all-zero", "all-ones", "all-one-bytes", "all-trailer-bytes"],
)
def test_pss_refuses_structurally_invalid_signatures(rsa_keypair, alg, filler):
    """Right length, in range, and nothing behind it.

    The PSS checks are ordered, so one crafted signature only ever reaches the first
    guard it fails. These four spread across them: an all-zero `em` fails the 0xbc
    trailer, an all-ones `em` fails the top-bits mask, and the others land in the DB
    padding and the 0x01 separator. Deterministic byte patterns rather than random
    ones, so a failure names a shape instead of a seed.
    """
    private, pem = rsa_keypair
    token = _rs_token(private, _claims(), alg=alg)
    assert _rsa_verifier(pem, alg)(_resign(token, filler * 256)) is None


# --- PSS internals: one forgery per guard ---------------------------------------
#
# The tests above prove `_verify_ps` is not `return True`, but its checks are ordered
# and each crafted signature stops at the first one it fails, so most of the interior
# stayed unproven -- the trailer byte, the top-bits mask, the DB padding, the 0x01
# separator and the salt could each be deleted with the suite green.
#
# Reaching them needs a signature that is valid right up to the guard under test. The
# private key makes that possible: recover the genuine encoded message with the public
# exponent (`em = s**e mod n`), change exactly one field, and re-sign the result with
# the private exponent so the verifier's own `pow(s, e, n)` reproduces it. The control
# case below re-signs an *unperturbed* `em`, which must still verify -- without it
# every case here could pass because the harness was broken rather than because the
# guard fired.


def _pss_forgery(private, perturb) -> tuple[str, bytes]:
    """A PS256 token whose encoded message is `perturb(em, parts)`.

    `_mgf1` is imported from the module under test rather than reimplemented. That is
    deliberate and narrow: the mask is not what is being tested here -- the guards
    reading the unmasked DB are -- and it is already proven by every passing
    signature above. Reimplementing it would be testing a copy against itself.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    from wreath._auth.jwt import _mgf1

    numbers = private.public_key().public_numbers()
    n, e, d = numbers.n, numbers.e, private.private_numbers().d
    k = (n.bit_length() + 7) // 8
    em_bits = n.bit_length() - 1
    em_len = (em_bits + 7) // 8
    h_len = s_len = 32
    pad_len = em_len - h_len - s_len - 2

    header = _b64u(json.dumps({"alg": "PS256", "typ": "JWT"}).encode())
    payload = _b64u(json.dumps(_claims()).encode())
    signing_input = f"{header}.{payload}".encode()

    algo = hashes.SHA256()
    genuine = private.sign(
        signing_input, padding.PSS(mgf=padding.MGF1(algo), salt_length=h_len), algo
    )
    em = pow(int.from_bytes(genuine, "big"), e, n).to_bytes(em_len, "big")

    h = em[em_len - h_len - 1 : em_len - 1]
    db_mask = _mgf1(h, em_len - h_len - 1, "sha256")
    db = bytes(a ^ b for a, b in zip(em[: em_len - h_len - 1], db_mask, strict=True))
    db = bytes([db[0] & 0x7F]) + db[1:]

    def remask(new_db: bytes) -> bytes:
        masked = bytes(a ^ b for a, b in zip(new_db, db_mask, strict=True))
        return bytes([masked[0] & 0x7F]) + masked[1:] + h + b"\xbc"

    forged_em = perturb(em, db, pad_len, em_len, remask, n)
    value = int.from_bytes(forged_em, "big")
    assert value < n, "the crafted encoded message must be in range to reach the guard"
    signature = pow(value, d, n).to_bytes(k, "big")
    return f"{header}.{payload}.{_b64u(signature)}", genuine


def _unperturbed(em, db, pad_len, em_len, remask, n):
    return em


def _wrong_trailer(em, db, pad_len, em_len, remask, n):
    return em[:-1] + b"\x00"


def _top_bit_set(em, db, pad_len, em_len, remask, n):
    # 2**em_bits with a correct trailer: always below `n` (a modulus is a product of
    # two odd primes and so is strictly greater than its own leading power of two),
    # which is what lets this reach the mask check rather than the range check.
    return ((1 << (n.bit_length() - 1)) | 0xBC).to_bytes(em_len, "big")


def _padding_not_zero(em, db, pad_len, em_len, remask, n):
    return remask(bytes(pad_len - 1) + b"\x01" + db[pad_len:])


def _separator_not_one(em, db, pad_len, em_len, remask, n):
    return remask(db[:pad_len] + b"\x02" + db[pad_len + 1 :])


def _salt_altered(em, db, pad_len, em_len, remask, n):
    return remask(
        db[: pad_len + 1] + bytes([db[pad_len + 1] ^ 0xFF]) + db[pad_len + 2 :]
    )


def test_the_pss_forgery_harness_reproduces_a_valid_signature(rsa_keypair):
    """The control. Re-signing an unmodified `em` must still verify.

    If this fails, every rejection below is meaningless — they would be refusing the
    harness rather than the forgery.
    """
    private, pem = rsa_keypair
    token, _ = _pss_forgery(private, _unperturbed)
    assert _rsa_verifier(pem, "PS256")(token) is not None


@pytest.mark.parametrize(
    "perturb",
    [_wrong_trailer, _top_bit_set, _padding_not_zero, _separator_not_one, _salt_altered],
    ids=["trailer-not-0xbc", "top-bits-not-cleared", "db-padding-not-zero",
         "separator-not-0x01", "salt-altered"],
)
def test_each_pss_structural_check_refuses_its_own_forgery(rsa_keypair, perturb):
    """One crafted encoded message per guard, valid up to the guard under test.

    `top-bits-not-cleared` is the subtle one. EMSA-PSS encodes into `em_bits` bits but
    `em_len` bytes, so the leading `8 * em_len - em_bits` bits must be zero; without
    that check the same signature has more than one valid encoding, which is the
    forgery-malleability class RFC 8017 §9.1.2 step 4 exists to close.
    """
    private, pem = rsa_keypair
    token, _ = _pss_forgery(private, perturb)
    assert _rsa_verifier(pem, "PS256")(token) is None


async def test_an_issuer_that_publishes_no_usable_keys_revokes_the_cached_ones():
    """**Zero keys is an answer, not a failure.**

    `if keys:` retained the previous set on an empty result, which is right for
    a transient error and wrong for a 200 carrying `{"keys": []}` -- that is an
    issuer saying every key it had is withdrawn. Retaining them meant anyone
    holding a token signed by a revoked key kept authenticating for the process
    lifetime, and the TTL was re-armed on the way past, so nothing ever
    reconsidered.
    """
    import json as _json

    from wreath._auth.jwks import JwksCache

    documents = [
        {"keys": [{"kty": "oct", "k": "AAAA", "kid": "k1"}]},
        {"keys": []},
    ]

    class _Response:
        def __init__(self, body):
            self.status = 200
            self.body = body

        def header(self, name, default=None):
            return default

    class _Client:
        async def get(self, path):
            # The last document repeats: an empty cache makes `resolve` try to
            # refresh again, which is the correct behaviour and would otherwise
            # run this stub out of responses.
            served = documents.pop(0) if len(documents) > 1 else documents[0]
            return _Response(_json.dumps(served).encode())

    cache = JwksCache(http_client=_Client(), jwks_path="/jwks", min_refresh_interval=0.0)
    await cache.prefetch()
    assert await cache.resolve("k1") is not None

    await cache.prefetch()
    # Counted before the lookup: an empty cache makes `resolve` refresh again,
    # which finds the same empty document and counts a second revocation.
    assert cache.empty_documents == 1
    assert await cache.resolve("k1") is None


async def test_a_transient_error_still_keeps_the_cached_keys():
    """The control the clearing must not cost.

    A non-200 is the case the retain-on-failure comment was written for, and it
    is a different thing from an issuer publishing an empty set.
    """
    from wreath._auth.jwks import JwksCache

    class _Response:
        def __init__(self, status, body):
            self.status = status
            self.body = body

        def header(self, name, default=None):
            return default

    responses = [
        _Response(200, b'{"keys": [{"kty": "oct", "k": "AAAA", "kid": "k1"}]}'),
        _Response(503, b""),
    ]

    class _Client:
        async def get(self, path):
            return responses.pop(0)

    cache = JwksCache(http_client=_Client(), jwks_path="/jwks", min_refresh_interval=0.0)
    await cache.prefetch()
    await cache.prefetch()
    assert await cache.resolve("k1") is not None
    assert cache.empty_documents == 0


async def test_a_document_of_only_encryption_keys_also_revokes():
    """The same state reached the other way: 200, keys present, none usable."""
    import json as _json

    from wreath._auth.jwks import JwksCache

    documents = [
        {"keys": [{"kty": "oct", "k": "AAAA", "kid": "k1"}]},
        {"keys": [{"kty": "oct", "k": "AAAA", "kid": "k1", "use": "enc"}]},
    ]

    class _Response:
        def __init__(self, body):
            self.status = 200
            self.body = body

        def header(self, name, default=None):
            return default

    class _Client:
        async def get(self, path):
            # The last document repeats: an empty cache makes `resolve` try to
            # refresh again, which is the correct behaviour and would otherwise
            # run this stub out of responses.
            served = documents.pop(0) if len(documents) > 1 else documents[0]
            return _Response(_json.dumps(served).encode())

    cache = JwksCache(http_client=_Client(), jwks_path="/jwks", min_refresh_interval=0.0)
    await cache.prefetch()
    await cache.prefetch()
    # Counted before the lookup: an empty cache makes `resolve` refresh again,
    # which finds the same empty document and counts a second revocation.
    assert cache.empty_documents == 1
    assert await cache.resolve("k1") is None
