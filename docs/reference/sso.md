# `wreath.sso`

`wreath.sso` drives the SAML and OIDC login flows over the verification wreath
already owned. It re-implements nothing: `SamlServiceProvider.consume` builds
what [`wreath.saml`](../guides/signed-xml.md)'s `verify_response` already takes,
and hands the answer to provisioning.

See [the guide](../guides/sso.md) for the per-organisation trust argument, which
is the part that is not glue.

::: wreath.sso
