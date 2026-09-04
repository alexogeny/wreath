from __future__ import annotations

import asyncio
import base64
from typing import Any, cast

import pytest

import wreath._userkit as userkit_module
from wreath._userkit import (
    CapturingEmailSender,
    InMemoryUserStore,
    authenticate,
    hash_password,
    register,
    reset_password,
    sign_token,
    start_password_reset,
    verify_email,
    verify_password,
    verify_token,
)


def run(coro):
    return asyncio.run(coro)


def _stored_hash(
    *, n: int = 16384, r: int = 8, p: int = 1, salt: bytes = b"s" * 16, digest: bytes = b"d" * 32
) -> str:
    def encoded(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"scrypt${n}${r}${p}${encoded(salt)}${encoded(digest)}"


def test_password_hash_roundtrip_and_reject():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("correct horse", "garbage") is False
    # distinct salts -> distinct encodings
    assert hash_password("x") != hash_password("x")


def test_password_verify_refuses_excessive_stored_work_before_scrypt(monkeypatch):
    def unexpected_scrypt(*args, **kwargs):
        pytest.fail("an excessive stored work factor reached scrypt")

    monkeypatch.setattr(userkit_module.hashlib, "scrypt", unexpected_scrypt)
    encoded = _stored_hash(p=5)

    assert verify_password("password", encoded) is False


@pytest.mark.parametrize(
    "encoded",
    (
        _stored_hash(n=1),
        _stored_hash(n=3),
        _stored_hash(r=0),
        _stored_hash(p=0),
        _stored_hash(salt=b"s" * 15),
        _stored_hash(digest=b"d" * 31),
    ),
)
def test_password_verify_refuses_invalid_stored_parameters_before_scrypt(
    monkeypatch, encoded: str
):
    def unexpected_scrypt(*args, **kwargs):
        pytest.fail("invalid stored parameters reached scrypt")

    monkeypatch.setattr(userkit_module.hashlib, "scrypt", unexpected_scrypt)

    assert verify_password("password", encoded) is False


def test_password_verify_refuses_oversized_inputs_before_expensive_work(monkeypatch):
    def unexpected_scrypt(*args, **kwargs):
        pytest.fail("an oversized password reached scrypt")

    monkeypatch.setattr(userkit_module.hashlib, "scrypt", unexpected_scrypt)
    assert verify_password("p" * 1025, _stored_hash()) is False

    def unexpected_decode(*args, **kwargs):
        pytest.fail("an oversized stored hash reached base64 decoding")

    monkeypatch.setattr(userkit_module, "_unb64", unexpected_decode)
    oversized = _stored_hash(salt=b"s" * 400)
    assert len(oversized) > 512
    assert verify_password("password", oversized) is False


@pytest.mark.parametrize("password", [None, b"password"])
def test_password_verify_refuses_non_text_passwords(password: Any):
    assert verify_password(cast(str, password), _stored_hash()) is False


@pytest.mark.parametrize("encoded", [None, b"hash"])
def test_password_verify_refuses_non_text_stored_hashes(encoded: Any):
    assert verify_password("password", cast(str, encoded)) is False


def test_token_sign_verify_expire_and_purpose():
    t = sign_token("secret", "verify", "42", ttl=100, now=1000)
    assert verify_token("secret", "verify", t, now=1050) == "42"
    assert verify_token("secret", "verify", t, now=2000) is None  # expired
    assert verify_token("secret", "reset", t, now=1050) is None  # wrong purpose
    assert verify_token("other", "verify", t, now=1050) is None  # wrong secret
    assert verify_token("secret", "verify", "no.dot.here", now=1050) is None
    assert verify_token("secret", "verify", t[:-2] + "zz", now=1050) is None  # tampered mac


def test_token_is_expired_at_its_signed_expiry_boundary():
    token = sign_token("secret", "verify", "42", ttl=100, now=1000)

    assert verify_token("secret", "verify", token, now=1100) is None


def test_oversized_token_is_refused_before_hmac(monkeypatch):
    def unexpected_hmac(*args, **kwargs):
        pytest.fail("an oversized action token reached HMAC")

    monkeypatch.setattr(userkit_module.hmac, "new", unexpected_hmac)

    assert verify_token("secret", "verify", "A" * 4097 + ".mac", now=1000) is None


def test_unencodable_token_text_is_an_invalid_token():
    assert verify_token("secret", "verify", "\ud800.mac", now=1000) is None


@pytest.mark.parametrize("token", [None, b"token.mac"])
def test_non_text_token_is_an_invalid_token(token: Any):
    assert verify_token("secret", "verify", cast(str, token), now=1000) is None


def test_token_bound_single_use():
    t = sign_token("s", "reset", "7", ttl=100, bound="fp1", now=0)
    assert verify_token("s", "reset", t, bound="fp1", now=1) == "7"
    assert verify_token("s", "reset", t, bound="fp2", now=1) is None  # fingerprint changed


def test_memory_store_batch_lookup_preserves_order_duplicates_and_misses():
    async def scenario():
        store = InMemoryUserStore()
        first = await store.create("first@example.com", "hash-1")
        second = await store.create("second@example.com", "hash-2")

        found = await store.get_many_by_id((second.id, "missing", first.id, second.id))

        assert found == [second, None, first, second]

    run(scenario())


def _links(purpose, token):
    return f"https://app/{purpose}/{token}"


def test_full_lifecycle():
    async def scenario():
        store = InMemoryUserStore()
        mail = CapturingEmailSender()

        # register -> unverified user + verification email (use the REAL emailed token)
        await register(
            store, mail, secret="s", email="Ann@Example.com ", password="pw1", link_builder=_links
        )
        user = await store.get_by_email("ann@example.com")
        assert user is not None and user.is_verified is False
        assert len(mail.verifications) == 1
        vtoken = mail.verifications[0][1].rsplit("/", 1)[1]

        # login works pre-verification here; verification gates features, not login.
        assert await authenticate(store, "ann@example.com", "pw1") is not None
        assert await authenticate(store, "ann@example.com", "nope") is None
        assert await authenticate(store, "unknown@example.com", "pw1") is None

        # verify
        assert await verify_email(store, secret="s", token=vtoken) is True
        assert (await store.get_by_email("ann@example.com")).is_verified is True

        # forgot -> reset email; reset changes the password (single-use)
        await start_password_reset(
            store, mail, secret="s", email="ann@example.com", link_builder=_links
        )
        assert len(mail.resets) == 1
        rtoken = mail.resets[0][1].rsplit("/", 1)[1]
        assert await reset_password(store, secret="s", token=rtoken, new_password="pw2") is True
        assert await authenticate(store, "ann@example.com", "pw2") is not None
        assert await authenticate(store, "ann@example.com", "pw1") is None
        # token no longer works (password hash changed -> fingerprint mismatch)
        assert await reset_password(store, secret="s", token=rtoken, new_password="pw3") is False

    run(scenario())


def test_enumeration_safe():
    async def scenario():
        store = InMemoryUserStore()
        mail = CapturingEmailSender()
        await register(store, mail, secret="s", email="a@b.com", password="pw", link_builder=_links)
        # re-register same email: uniform, creates nothing new, sends nothing new
        await register(
            store, mail, secret="s", email="a@b.com", password="other", link_builder=_links
        )
        assert len(mail.verifications) == 1
        # forgot on unknown email: no email sent, no error
        await start_password_reset(
            store, mail, secret="s", email="nobody@b.com", link_builder=_links
        )
        assert len(mail.resets) == 0

    run(scenario())
