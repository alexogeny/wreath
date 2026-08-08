# Verified agents

For most of the web's life, the only answer to "who is making this request?" was
a string the caller chose for itself. `User-Agent` was a claim, `robots.txt` was
a request politely made, and both worked exactly as well as the caller's manners.

That arrangement is under real strain. A large and growing share of traffic is
software acting on someone's behalf, and the well-behaved half of it would very
much like to prove it is the well-behaved half. **HTTP Message Signatures**
(RFC 9421) is how: the caller signs the parts of the request that matter with a
private key, names where its public key lives, and the receiver checks the
arithmetic. **Web Bot Auth** is the profile that pins this to bots and agents —
an Ed25519 key, a `Signature-Agent` header, and a directory of keys published at
a well-known URL.

Wreath verifies it at ingress, and hands the answer to your policies.

## The one idea worth holding on to

**The boundary establishes a fact. The policy set decides what it means.**

`wreath.signatures` never allows or denies. It answers one question — *did this
request carry a signature that checks out, and whose?* — and puts the answer
where a Cedar policy can read it. Everything that follows from that answer is a
policy you write, in the same vocabulary as every other rule in your
application.

That separation matters more than it might look. **Verified is not trusted.** A
valid signature proves *which* agent is calling; it says nothing about whether
that agent is welcome. Wiring the check straight to an allow/deny would rebuild
the `User-Agent` allow-list with more code and better maths — and you would
still be maintaining a list.

## Verifying inbound requests

```python
from wreath import Wreath
from wreath.signatures import NonceLedger, Signatures

signatures = Signatures(
    directories=(
        "https://openai.com/.well-known/http-message-signatures-directory",
    ),
    max_age=60.0,
    nonces=NonceLedger(max_entries=16_384, ttl=300.0),
)

app = Wreath(signatures=signatures)
```

That is the whole setup. `Signatures` registers itself as global middleware — so
it covers route misses, static files and authorization failures, which is the
point, since aggressive traffic is trying to make your server pay for request
construction before anything gets to say no — and it refreshes the key
directories once during lifespan startup.

Now write the policy. Compose the facts into whatever context provider your
authorizer already uses:

```python
from wreath.authorization import CedarAuthorizer

def context(request):
    return {
        "method": request.method,
        "path": request.path,
        **signatures.cedar_context(request),
    }

app.configure_auth(backend, CedarAuthorizer(engine, context=context))
```

and then say what you actually mean:

```cedar
permit (principal, action == Action::"read", resource is Article)
unless { context.signature_verified == false && resource.paid };
```

`context.signature_verified` is **always present and always a boolean**, so a
`when` policy and an `unless` policy read the same way on an unsigned request.
`context.signature_agent` is present only when a signature verified, so a policy
that tests it with `has` fails closed rather than matching an empty string.

### What the signature covered

A signature covers the components the caller listed, and nothing else. Wreath
demands `@method`, `@authority`, `@path` and `@query` of every signature, and
`content-digest` of any request that carries a body — a signature that omits the
query is a signature over an *endpoint*, replayable with whatever parameters the
observer likes, and an uncovered body is a body anyone may swap. The digest is
recomputed from the bytes when your handler reads them, so a mismatch is a 400
before the handler sees a thing. Covering the *header* would prove only that the
sender typed it.

`context.signature_covered` is the component list, present whenever a signature
verified, so a policy that needs the strong form can ask for it rather than
assume it:

```cedar
permit (principal, action == Action::"write", resource is Order)
when { context.signature_covered.contains("content-digest") };
```

### What it will not do

**It never fetches a key while serving a request.** Keys come from the
directories, refreshed off the request path. A signature naming a key this
process has not got is simply unverified — the same outcome as no signature at
all, and your policy already handles that case.

This is deliberate. Resolving an unknown `keyid` by going and fetching it turns
one inbound request from an anonymous caller into one outbound request to a host
that caller named. That is an amplifier with a header for a trigger. If you need
rotation picked up sooner than a restart, call `await signatures.refresh()` on
your own schedule — from a `wreath.jobs` schedule, for instance.

### Replay, and the ledger that fails closed

`max_age` bounds how far a signature's `created` timestamp may sit from now, in
*both* directions — a forged future timestamp buys nothing. Inside that window,
a `NonceLedger` remembers the nonces it has seen.

When that ledger fills up, it **refuses** rather than making room. That is worth
saying plainly, because it is the opposite of what a cache does and it is not an
oversight: the nonce space here is controlled by callers you have not
authenticated, so evicting under pressure is an attacker flushing the ledger and
replaying whatever fell out. Refusing is a denial-of-service trade made
deliberately in the safe direction, it is counted on `ledger.refusals`, and a
rising number means the ledger is undersized or something is flooding you.

## Signing outbound requests

The same base construction, run the other way, for when your service is the
caller:

```python
from wreath.signatures import SigningKey, sign_request

key = SigningKey(key_id="prod-2026", sign=my_ed25519_signer, agent=MY_DIRECTORY)

headers = sign_request(key, method="GET", url="https://api.example.com/v1/items")
response = await client.get("/v1/items", headers=headers)
```

**Pass `body=` when there is one.** It is hashed into a `Content-Digest` header,
added to the returned headers and to the covered set, and it is the only thing
that makes the signature a signature over your payload:

```python
payload = json.dumps({"quantity": 1}).encode()
headers = sign_request(
    key, method="POST", url="https://api.example.com/v1/orders", body=payload
)
response = await client.post("/v1/orders", content=payload, headers=headers)
```

`sign` is a callable you supply rather than a key wreath holds, because wreath's
built-in cryptography is verify-only — there is no private-key handling and no
nonce generation to get wrong. Your key already lives somewhere: an HSM, a KMS,
or `cryptography` in your own code. Hand over the signing function.

## Telling crawlers what is what

Two files every site ends up hand-maintaining, and both drift from reality on the
first refactor. Wreath derives them from the route table instead:

```python
from wreath.signatures import llms_txt, robots_txt

@app.get("/robots.txt")
async def robots(request) -> str:
    return robots_txt(app, sitemap="https://example.com/sitemap.xml")


@app.get("/llms.txt")
async def llms(request) -> str:
    return llms_txt(app, title="Example", summary="An example service.")
```

"Public" here is not a new opinion — it is `AuthRequirement.access_level == 0`,
the same definition both dispatchers, `Wreath._authorize_request` and the MCP
server already use for *does this endpoint ask anything of the caller*. Put
`@authenticated()` on a route and it moves from `Allow` to `Disallow` with no
second file to remember. A path with parameters is reduced to its static prefix,
because `robots.txt` has no notion of `{slug}`.

Be clear-eyed about what these are: **honour-system controls**. A crawler that
ignores your `robots.txt` is not stopped by it. Their value here is that they
cannot go stale. The enforced half of the story is the signature check above and
the policy behind it.

## Asking for payment instead of saying no

Sometimes the right answer to an unrecognised agent is not a wall but a price:

```python
from wreath.signatures import PaymentRequired

@app.get("/archive/{item}")
async def archive(request, item: str) -> dict:
    if not signatures.facts(request).verified and not paid(request):
        raise PaymentRequired(
            amount="0.002", currency="USD", pay_to="https://pay.example/x"
        )
    return await load(item)
```

`PaymentRequired` is an ordinary `HTTPException`, so it renders as an RFC 9457
problem document and carries its `Accept-Payment` challenge exactly the way a 401
carries its own. It is a **response shape and nothing more** — wreath ships no
payment integration and blesses no protocol, because several were still
competing for the role as of mid-2026. `scheme` is yours to choose and
settlement is your application's business.

## A note on cost

One Ed25519 verification in wreath's dependency-free implementation costs about
2.5 ms, against roughly 17 µs for parsing the headers and building the signature
base. So every cheap refusal — a signature that covers too little, a stale
timestamp, an unknown key, a replayed nonce — is ordered *above* the signature
check, and a caller who fails one of those costs you microseconds rather than
milliseconds.

That ordering shrinks the cost of a rejected request. It does not shrink the
cost of an accepted one, so keep your rate limiter configured: verification is
work, and work that anyone can ask for wants a bound.

## The profile is still moving

Web Bot Auth is an application profile of RFC 9421 being standardised through an
IETF working group chartered in 2026, with documents still in flight through
2026-27. `Signatures(profile=...)` names which profile you are enforcing;
`WEB_BOT_AUTH_2026` is the one this build implements, and a `Signatures` asked
for an unknown profile refuses at construction rather than verifying under rules
nobody chose. Expect the well-known directory path to move at least once.

Reference: [`wreath.signatures`](../reference/signatures.md).
