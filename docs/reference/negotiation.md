# `wreath.negotiation`

Serialize a response in the format the client asked for. The handler returns
plain data and `serialize` picks the encoding from `Accept`, so one endpoint
answers a browser in JSON and a mobile client in MessagePack.

`Accept` is parsed with q-values per RFC 9110 §12.5.1. A negotiated response
carries `Vary: Accept` so a shared cache keys on what it actually holds; an
unsatisfiable `Accept` yields `406 Not Acceptable` listing what is available,
and that one carries no `Vary`, because it varies with nothing worth reusing.
The guide is [Content negotiation](../guides/content-negotiation.md).

::: wreath.negotiation
