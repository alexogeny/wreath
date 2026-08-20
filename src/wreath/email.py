"""First-party email declarations, transports, suppression, and DKIM."""

from __future__ import annotations

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
