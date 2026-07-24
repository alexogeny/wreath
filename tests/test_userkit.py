"""User-management core: password hashing, action tokens, and lifecycle flows.

Stdlib-only surface (``wreath._userkit``) — no server / native build needed.
"""
from __future__ import annotations

import asyncio

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


# --- hashing ---------------------------------------------------------------

def test_password_hash_roundtrip_and_reject():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("correct horse", "garbage") is False
    # distinct salts -> distinct encodings
    assert hash_password("x") != hash_password("x")


# --- tokens ----------------------------------------------------------------

def test_token_sign_verify_expire_and_purpose():
    t = sign_token("secret", "verify", "42", ttl=100, now=1000)
    assert verify_token("secret", "verify", t, now=1050) == "42"
    assert verify_token("secret", "verify", t, now=2000) is None       # expired
    assert verify_token("secret", "reset", t, now=1050) is None        # wrong purpose
    assert verify_token("other", "verify", t, now=1050) is None        # wrong secret
    assert verify_token("secret", "verify", "no.dot.here", now=1050) is None
    assert verify_token("secret", "verify", t[:-2] + "zz", now=1050) is None  # tampered mac


def test_token_bound_single_use():
    t = sign_token("s", "reset", "7", ttl=100, bound="fp1", now=0)
    assert verify_token("s", "reset", t, bound="fp1", now=1) == "7"
    assert verify_token("s", "reset", t, bound="fp2", now=1) is None    # fingerprint changed


# --- flows -----------------------------------------------------------------

def _links(purpose, token):
    return f"https://app/{purpose}/{token}"


def test_full_lifecycle():
    async def scenario():
        store = InMemoryUserStore()
        mail = CapturingEmailSender()

        # register -> unverified user + verification email (use the REAL emailed token)
        await register(store, mail, secret="s", email="Ann@Example.com ",
                       password="pw1", link_builder=_links)
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
        await start_password_reset(store, mail, secret="s", email="ann@example.com",
                                   link_builder=_links)
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
        await register(store, mail, secret="s", email="a@b.com", password="pw",
                       link_builder=_links)
        # re-register same email: uniform, creates nothing new, sends nothing new
        await register(store, mail, secret="s", email="a@b.com", password="other",
                       link_builder=_links)
        assert len(mail.verifications) == 1
        # forgot on unknown email: no email sent, no error
        await start_password_reset(store, mail, secret="s", email="nobody@b.com",
                                   link_builder=_links)
        assert len(mail.resets) == 0

    run(scenario())
