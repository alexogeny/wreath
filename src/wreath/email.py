"""First-party email declarations, transports, suppression, and DKIM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._dkim import (
        DkimError,
        DkimSigner,
        Ed25519Key,
        RsaKey,
        canonicalize_body_relaxed,
        canonicalize_header_relaxed,
        load_private_key,
    )
    from ._userkit import (
        CapturingEmailSender,
        EmailSender,
        InMemorySuppressionList,
        LogEmailSender,
        MailClass,
        Message,
        SmtpEmailSender,
        SuppressedError,
        SuppressionList,
        SuppressionReason,
        Unsubscribe,
    )

__all__ = [
    "CapturingEmailSender",
    "DkimError",
    "DkimSigner",
    "Ed25519Key",
    "EmailSender",
    "InMemorySuppressionList",
    "LogEmailSender",
    "MailClass",
    "Message",
    "RsaKey",
    "SmtpEmailSender",
    "SuppressedError",
    "SuppressionList",
    "SuppressionReason",
    "Unsubscribe",
    "canonicalize_body_relaxed",
    "canonicalize_header_relaxed",
    "load_private_key",
]

_EXPORTS = {
    "CapturingEmailSender": "_userkit",
    "DkimError": "_dkim",
    "DkimSigner": "_dkim",
    "Ed25519Key": "_dkim",
    "EmailSender": "_userkit",
    "InMemorySuppressionList": "_userkit",
    "LogEmailSender": "_userkit",
    "MailClass": "_userkit",
    "Message": "_userkit",
    "RsaKey": "_dkim",
    "SmtpEmailSender": "_userkit",
    "SuppressedError": "_userkit",
    "SuppressionList": "_userkit",
    "SuppressionReason": "_userkit",
    "Unsubscribe": "_userkit",
    "canonicalize_body_relaxed": "_dkim",
    "canonicalize_header_relaxed": "_dkim",
    "load_private_key": "_dkim",
}

_MODULE_EXPORTS = {
    "_dkim": (
        "DkimError",
        "DkimSigner",
        "Ed25519Key",
        "RsaKey",
        "canonicalize_body_relaxed",
        "canonicalize_header_relaxed",
        "load_private_key",
    ),
    "_userkit": (
        "CapturingEmailSender",
        "EmailSender",
        "InMemorySuppressionList",
        "LogEmailSender",
        "MailClass",
        "Message",
        "SmtpEmailSender",
        "SuppressedError",
        "SuppressionList",
        "SuppressionReason",
        "Unsubscribe",
    ),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    loaded = import_module(f".{module}", __package__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
