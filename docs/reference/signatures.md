# `wreath.signatures`

Cryptographic proof of *who is calling*, at the edge of the application, in both
directions. An inbound request carrying an RFC 9421 signature is checked before a
route is ever activated, and what the check learns becomes a fact your policies
can read — never a decision this module makes on its own. The same signature base
runs in reverse when your service is the one calling out.

Reach for it when the traffic you care about is not a browser: verified crawlers
and AI agents that publish a key and sign what they send, and service-to-service
calls that want proof stronger than a shared bearer token.

For the reasoning, the shape of a policy that uses it, and how the crawler
declarations stay in step with your route table, read the
[Verified agents](../guides/verified-agents.md) guide first.

::: wreath.signatures
