# `wreath.oauth`

An OAuth 2.1 authorization server for a deployment that issues its own tokens.
What it mints is what [`wreath.auth`](auth.md)'s `JwtVerifier` and `MCPAuth`
already verify — that is the first obligation, and the tests drive a minted
token through the real verifier rather than through a decoder written beside the
encoder.

See [the guide](../guides/sso.md) for when a deployment wants this at all.

::: wreath.oauth
