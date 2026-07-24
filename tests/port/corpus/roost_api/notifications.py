"""SMS notifications (Twilio) + feature flags (Unleash).

External integrations with no wreath equivalent — the codemod copies these
verbatim and flags them ``unsupported`` (nothing to translate to).
"""
from twilio.rest import Client
from UnleashClient import UnleashClient

from .settings import settings

_sms = Client(
    settings.twilio.TWILIO_ACCOUNT_SID, settings.twilio.TWILIO_SMS_API_KEY_NAME
)
_flags = UnleashClient(
    url=settings.unleash.UNLEASH_URL,
    app_name="roost",
    instance_id=settings.unleash.UNLEASH_INSTANCE_ID,
)


def send_boarding_sms(to_number: str, body: str) -> str:
    message = _sms.messages.create(
        to=to_number,
        messaging_service_sid=settings.twilio.TWILIO_MESSAGING_SERVICE_ID,
        body=body,
    )
    return message.sid


def reminders_enabled(wrangler_id: str) -> bool:
    return _flags.is_enabled("roost.boarding_reminders", {"userId": wrangler_id})
