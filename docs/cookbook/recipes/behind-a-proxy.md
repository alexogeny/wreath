# Deploy behind a proxy or load balancer

Most production Wreath apps don't face the internet directly — a proxy or load
balancer terminates TLS and forwards the request on. That's fine, but it hides
something important: by the time the request reaches Wreath it looks like plain
HTTP from the proxy's address, and the real scheme and client are tucked away in
`X-Forwarded-*` headers. If Wreath believes the connection is insecure, it will
make the wrong calls about HSTS, secure cookies, and CSRF.

`ProxyPolicy` fixes this by trusting those forwarded headers — but
only from your proxy, never from a client that might forge them:

```python
from wreath import Wreath
from wreath.policy import HttpPolicy, ProxyPolicy, TrustedHostPolicy

app = Wreath(http_policy=HttpPolicy(
    proxy=ProxyPolicy(trusted=["10.0.0.0/8"]),
    trusted_host=TrustedHostPolicy(("api.example",)),
))
```

Restrict `trusted` to the network your proxy actually sits on. With that in
place, scheme- and host-dependent behaviour is correct again, and a
`TrustedHostPolicy` turns away requests for hostnames you don't serve.
