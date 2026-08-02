# `wreath.notifications`

Telling someone something, once, on the channels they actually want. Declare a
notification *kind* — a name, a mail class, a digest window — and send instances
of it; the layer handles preferences, deduplication and fan-out, and hands each
delivery to a durable job so retries are the ones wreath already has.

Reach for this when more than one thing in your application needs to reach a
person, and especially before you write the second `send_email(...)` call at the
point where something happened. The declaration is what later makes "batch this
one hourly" and "never tell me twice" possible at all.

::: wreath.notifications
