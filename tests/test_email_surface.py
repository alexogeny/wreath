"""The shipped email machinery has one public first-party namespace."""

from __future__ import annotations

from wreath import email
from wreath._dkim import DkimSigner
from wreath._userkit import MailClass


def test_email_surface_owns_delivery_policy_and_dkim() -> None:
    assert email.MailClass is MailClass
    assert email.DkimSigner is DkimSigner
    message = email.Message(
        to="person@example.test",
        subject="Receipt",
        body="Paid",
        mail_class=email.MailClass.TRANSACTIONAL,
    )
    assert message.to == "person@example.test"
