# Notifications: email that arrives, and push that needs no vendor

Two things happen to every application that grows past its first few screens.
Something needs to tell a person that something happened, and the person needs a
say in how often that occurs. Both start out as one `send_email(...)` call at the
point of the event, and both end up as a tangle — because that call has no name,
so nothing downstream can recognise two of them as the same thing.

Wreath's answer is a **kind**: a notification declared once, with an identity, so
the rest becomes possible.

```python
from dataclasses import dataclass
from wreath.notifications import Email, Notifications, Recipient, WebPush

notify = Notifications(channels=[Email(sender), WebPush(vapid, subscriptions)])

@notify.kind("photo_shared", digest=3600)
@dataclass
class PhotoShared:
    photo_id: str
    actor: str

    def title(self) -> str:
        return f"{self.actor} shared a photo with you"

    def navigate(self) -> str:
        return f"/photos/{self.photo_id}"

await notify.send(PhotoShared(photo.id, actor.name), to=Recipient("u1", email=address))
```

`digest=3600` means a second `photo_shared` for the same person inside the hour
is collapsed rather than sent. That is only expressible because the kind has a
name; a stream of bare send calls has nothing to collapse *on*, which is why
applications that start there never get digests and never get deduplication.

Delivery per channel is a `wreath.jobs` job when you pass `enqueue=`, so retries,
backoff and dead-lettering are the durable ones rather than a second set. One
channel failing does not stop the others: a push service outage must not withhold
somebody's email, so `SendResult.failed` names the channel and the rest still go.

## The one field with no default

Every message says whether it is **transactional** or **marketing**, and there is
no default, because both wrong answers are expensive and neither is recoverable
by guessing.

```python
from wreath.users import MailClass, Message, Unsubscribe

Message(to=address, subject="Reset your password", body=link,
        mail_class=MailClass.TRANSACTIONAL)
```

Since May 2026, Google, Yahoo and Microsoft reject non-compliant bulk mail with a
permanent 550 rather than filing it in a spam folder. Above 5,000 messages a day
that means SPF, DKIM and DMARC alignment, a complaint rate under 0.10%, and — for
marketing mail — RFC 8058 one-click unsubscribe. So:

- **Marketing** mail must carry an `Unsubscribe`, and is refused for an address on
  the suppression list.
- **Transactional** mail needs no unsubscribe header and is delivered *even to a
  suppressed address*. Someone who marked a newsletter as spam must still be able
  to reset their own password; if they cannot, the complaint has locked them out
  of their account.

Get that backwards in one direction and you are a non-compliant bulk sender. Get
it backwards in the other and your support queue fills with people who cannot log
in. It is a legal question with a technical encoding, so wreath makes you write it
down rather than inferring it.

## Signing what you send

`SmtpEmailSender` takes a `DkimSigner`, and signs the exact bytes it hands to the
MTA:

```python
from wreath.users import SmtpEmailSender
from wreath._dkim import DkimSigner, load_private_key

sender = SmtpEmailSender(
    host="smtp.example.net",
    from_addr="accounts@example.com",
    dkim=DkimSigner("example.com", "sel", load_private_key(open("dkim.pem").read())),
    suppression=suppression_list,
)
```

RSA and Ed25519 keys both work, in PKCS#1 or PKCS#8. Every signature is verified
with the public exponent before it is returned — a wrong DKIM signature is worse
than no signature, because DMARC reads it as `fail` rather than `none`, and mail
arrives pre-condemned.

## Finding out before your users do

The part of email correctness that lives outside your code is DNS, and it is
where this usually goes wrong. `wreath.doctor` asks:

```python
from wreath.doctor import check_email_deliverability

for finding in check_email_deliverability(sender):
    print(finding)
```

```
DKIM signs as d=mail.example.com but mail is sent From example.com: the signature
will verify and DMARC will still fail, because DMARC requires the signing domain
to align with the From domain
```

That is the failure that looks like success — every DKIM debugger reports "pass",
and the mail is still rejected. The check also names a missing or revoked
selector record, a domain with two SPF records, an SPF record ending `+all`, and
a DMARC policy of `p=none`. An unreachable nameserver reports that it could not
tell, which is deliberately a different sentence from "it is not configured": a
check that turns a slow resolver into a wall of false findings is a check people
learn to skip.

## Web push, with nothing to install

Every other Python web-push stack pulls in `pywebpush` and `cryptography`. Wreath
cannot take a runtime dependency, and it turns out not to need one — VAPID is a
P-256 keypair and a signed JWT, and the payload encryption is ECDH, HKDF and
AES-128-GCM, all of which ship here.

```python
from wreath.notifications import VapidKeys, WebPush

vapid = VapidKeys.generate("mailto:ops@example.com")
print(vapid.application_server_key)   # hand this to pushManager.subscribe()
```

Store `vapid.private_bytes` somewhere durable: the public half is what a browser
pins when it subscribes, so rotating the key invalidates every existing
subscription.

Notifications are sent as **Declarative Web Push**, the format Safari 18.4 and
later display with no service worker involved — the payload declares the
notification, so there is no JavaScript to wake and none to fail silently on a
locked phone. It is not yet universal, so keep a service worker for the browsers
that still need one; the same payload drives both.

When a push service answers `404` or `410`, the subscription is permanently gone
and the channel deletes it. That is required rather than tidy: a store that never
prunes becomes a slow leak and a rising error rate nobody attributes to anything.

## What this deliberately is not

Not an email service provider. Warm-up scheduling, reputation management,
per-domain throttling and IP rotation are a product, and a framework that starts
down that road ends up maintaining a deliverability team's worth of heuristics.
Wreath emits correct, signed, unsubscribable mail through whatever transport you
configure, and says loudly when the DNS does not back it up.

Reference: [`wreath.notifications`](../reference/notifications.md) and
[`wreath.doctor`](../reference/doctor.md).
