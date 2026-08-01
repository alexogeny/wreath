# Authentication and authorization

These two words are often blurred together, and blurring them is where security
bugs hide. Wreath keeps them apart, in plain terms:

- **Authentication** establishes *who* a request is. It lives in `wreath.auth`.
- **Authorization** decides *what* that identity is allowed to do. It lives in
  `wreath.authorization`.

Identity first, permission second — always in that order.

## User story: a "who am I" endpoint for signed-in users

> *As an API author, my `/me` endpoint should work for any signed-in user and
> turn away anyone who isn't — and inside it I want the caller's identity, not to
> re-parse the token myself.*

```python
from wreath.auth import BearerTokenBackend, Identity, authenticated

async def verify(token: str) -> Identity | None:
    user = await lookup(token)
    return Identity(user.id, roles=frozenset(user.roles)) if user else None

app.configure_auth(BearerTokenBackend(verify))

@app.get("/me")
@authenticated()
async def me(request) -> dict:
    return {"id": request.identity.id, "roles": sorted(request.identity.roles)}
```

`configure_auth` installs the backend that turns a bearer token into an
`Identity`; `@authenticated()` rejects a request without one with a `401` and a
`WWW-Authenticate: Bearer` challenge. Past that gate, `request.identity` is
guaranteed to be there. Deciding *what* that identity may do is the next step.

### Cancelling a token early

`JwtVerifier(revoked=...)` — or `verify_jwt(revoked=...)` — takes
`revoked(claims) -> bool`, checked after the signature and the registered
claims. Nothing ships behind it: a real revocation list is a lookup on the
busiest path in the framework, and that is the application's call. A hook that
raises **denies**, because a revocation store that is unreachable must not be
one that says yes. Short token lifetimes remain the primary answer.


## Establishing identity

Configure a backend that turns a credential into an `Identity`, or into nothing
if the credential doesn't check out:

```python
from wreath.auth import BearerTokenBackend, Identity

async def verify(token: str) -> Identity | None:
    user = await lookup(token)
    return Identity(user.id, roles=frozenset(user.roles)) if user else None

app.configure_auth(BearerTokenBackend(verify))
```

Once configured, a handler can ask whether the request is authenticated and who
it belongs to, and the `authenticated` decorator will turn away anyone who
isn't.

### A backend that reads the session must be behind global middleware

`SessionIdentityBackend` reads `request.state.session` while it authenticates.
`SessionMiddleware` is route middleware by default — compiled into a route's
tape, so a miss or a static file never pays to decode a cookie — and route
middleware runs *after* authorization. Register the two that way and the session
arrives after the backend has already been asked who the caller is, so every
protected route answers `401` to a cookie the server itself just issued.

```python
app.add_global_middleware(SessionMiddleware(secret=...))   # yes
app.add_middleware(SessionMiddleware(secret=...))          # refused
app.include_router(Router(middleware=[SessionMiddleware(secret=...)]))   # refused
```

Wreath refuses every route-scoped spelling when the routes compile — the
application's, a router's, a nested router's, and one route's — naming the
remedy, because the symptom is a `401` indistinguishable from a genuinely
anonymous request. A session that only handlers read has no such ordering
requirement, and route scope stays the cheaper registration for it.

## Deciding what they may do

Require a role or a permission:

```python
from wreath.authorization import roles, permissions, authorize

@app.get("/admin")
@roles("admin")
async def admin(request) -> dict:
    return {"ok": True}
```

Both take `mode="any"` to accept *one* of several values, where the default
`mode="all"` requires every one. Keep the `any` checks few: they compile to a
list of capability combinations, so each one multiplies that list by its number
of values, and the checks accumulate down a chain of nested routers. Wreath
refuses at declaration once the combinations pass a ceiling rather than
expanding them — the alternative is an application that starts slowly and then
matches every route slowly. Two `any` checks of three values each is fine; a
dozen is a policy question, and the answer is Cedar.

When your rules grow beyond a simple check, write them in Cedar. Wreath ships
its own Cedar policy engine — no dependency, no service, evaluated natively —
and `CedarPolicies` parses your policy set once at startup, where a syntax
error is an application bug rather than a request-time surprise:

```python
from wreath.authorization import CedarAuthorizer, CedarPolicies, EntityUid, authorize

engine = CedarPolicies("""
    permit(principal in Role::"editor", action == Action::"Document::read", resource);
    forbid(principal, action, resource) when { context.method != "GET" };
""")
app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))

@app.get("/documents/{id}")
@authorize(
    action="Document::read",
    resource=lambda request: EntityUid("Document", request.path_params["id"]),
)
async def document(request) -> dict:
    ...
```

The default mappers model the common case: the authenticated identity becomes
the principal, its roles become `Role::"..."` parents (so `principal in
Role::"editor"` works with no further wiring), and the request method and path
arrive as `context` — along with `second_factor_age` when the caller has proved
a second factor, so a policy can insist on a *recent* one before something
destructive. `@second_factor(max_age=...)` says the same thing on a route
without writing a policy; see [Second factors](second-factors.md). Forbid
overrides permit, the default is deny, and a
policy that errors is skipped and reported in the decision's diagnostics —
never silently satisfied. The engine covers the Cedar core; extension types
(`ip`, `decimal`, `datetime`) and schema validation are not implemented yet
and fail loudly at parse time. A different evaluator can be swapped in through
the same `CedarEngine` protocol the built-in engine satisfies.

### Feature flags in a policy

A flag and a policy answer one question between them — *may this caller do this
now?* — and kept apart they drift, silently, in whichever direction nobody
checks. Hand the authorizer a [flag provider](health-flags-versioning.md) and
the flags arrive as context, so the rollout and the rule are one decision:

```python
engine = CedarPolicies("""
    permit(principal in Role::"editor", action == Action::"Invoice::void", resource)
    when { context.flags.contains("new_billing") };
""")
app.configure_auth(
    BearerTokenBackend(verify),
    CedarAuthorizer(engine=engine, flags=app.flags(new_billing="25%")),
)
```

`context.flags` is a **set of the enabled flag names**, never a map of name to
boolean. `context.flags["x"] == false` reads as "explicitly off" when it may
equally mean "no such flag", and an authorization expression that cannot tell
those apart eventually permits something because of a typo. Absent from the set
is false, and deny is the safe direction. Use `.contains()`, `.containsAny([…])`
and `.containsAll([…])`.

Three properties are worth knowing before you write the first one:

- **A flag can never permit on its own.** It is an input to the decision; Cedar
  still makes it. There is no shape of flag configuration that grants access no
  policy grants.
- **The set is resolved once per request**, so a route behind several policies
  cannot see two answers. That matters most for a percentage rollout: without
  it, a `permit` and a `forbid` could disagree about whether the same caller is
  inside the same 25%. The bucket is the one
  [`flags_dependency`](health-flags-versioning.md) uses, so a rollout places a
  principal identically in a policy and in a handler.
- **A misspelled flag fails at startup**, naming it, when the provider can
  enumerate its own names — which `FeatureFlags` can. Without that check the
  name is simply absent from the set and the policy denies forever with nothing
  to see. A provider that cannot list its vocabulary (an external service that
  would need a network call) is warned about where the authorizer is built
  instead, because refusing on a guess would break a working deployment.

An application with no provider gets an empty set, so a flag-reading policy
denies. That is deliberate rather than incidental: with no `flags` key in the
context at all, `forbid(...) unless { context.flags.contains("bypass") }`
evaluates to *allowed* — the forbid is skipped rather than left standing — which
would silently disable every kill-switch written in that shape. The empty set
denies in both the `when` and the `unless` form, so the authorizer always
supplies it.

Only the flags your policies actually name are resolved. Against a provider
holding fifty flags, resolving all of them measured +21.7us per request for
on/off values and +56.5us for percentages; resolving the three a policy names
costs +1.5us and +3.9us. A policy set that reads flags in a shape whose names
cannot be read off the source — `context.flags.isEmpty()`, or an argument
computed at evaluation time — falls back to resolving everything, because a
short list would change the answer rather than merely cost less.

## Geofencing: policies that read where the caller is

`context.regions` is the same idea for *place*. Declare the areas your policies
name, tell the authorizer how to find the caller's position, and a geofence
becomes a policy rather than a predicate rewritten at every call site:

```python
from wreath.authorization import CedarAuthorizer, Regions
from wreath.geospatial import BoundingBox, Coordinate

regions = Regions(
    depot=(Coordinate(lat=-23.68, lon=133.88), 5_000),   # centre and metres
    reserve=BoundingBox(-26.0, -24.0, 132.0, 135.0),
)

CedarAuthorizer(
    engine=engine,
    regions=regions,
    location=lambda request: request.state.get("device_fix"),
)
```

```cedar
permit (principal in Role::"ranger", action == Action::"read", resource is Collar)
when { context.regions.contains(resource.reserve) };
```

Like `flags`, it is a **set of names** and it is supplied unconditionally —
an absent `regions` makes `forbid(...) unless { context.regions.contains(...) }`
evaluate to *allowed*, so an application that never configured regions would
silently stop geofencing.

**Where the position comes from is yours.** `location` defaults to returning
`None`, because a device fix, an identity attribute and an IP lookup are
different evidence with different trust, and the framework guessing would put a
policy's geofence on something nobody chose. No location means every region set
is empty, and an empty set denies in both the `when` and the `unless` shape.

Only the regions your policies name are measured, and the saving is larger than
for flags because a region test is a great-circle distance rather than a
dictionary lookup: against fifty declared regions, resolving the one a policy
names measured **+0.81us** per request and resolving all fifty **+6.08us**,
over a 0.24us noise floor. Note that `contains(resource.reserve)` — the most
natural way to write a geofence — computes its argument, so its names cannot be
read off the source and every region is resolved. Keeping the declared set small
is the tuning knob.

## Precision as an outcome, not a verdict

A withheld field is a boolean: present or absent. Real deployments want a scale,
and want the policy engine to choose it — a ranger sees an endangered animal's
exact position, a partner sees it to a kilometre, a volunteer to ten, and the
public not at all. A `PrecisionLadder` is an ordered set of ordinary actions,
asked finest-first until one permits:

```python
from wreath.authorization import PrecisionLadder

ladder = PrecisionLadder(
    ("Station::locate_exact", None),      # exact
    ("Station::locate_fine", 1_000),      # 1 km
    ("Station::locate_coarse", 10_000),   # 10 km
)

crud_router(Station, open_session, precision={"location": ladder})
```

A caller permitted no rung gets **no key at all** — absent, not null, because a
null says the row has no position and that is a different and false statement.

The degradation is a **fixed grid**, never per-request jitter, and that is the
security property rather than an implementation detail: jitter averages away, so
an attacker who can ask repeatedly recovers the true position, while a grid cell
reveals the cell however many times it is asked. It is applied in `serialize`,
the only path a column takes out of a generated route, so there is no
select-the-column-directly way around it.

A ladder is a real authorization query per laddered column: measured at
**+15.9us** on top of a geofenced request, because it *is* another Cedar
evaluation. It is resolved once per request and cached, so a page of a hundred
rows pays it once rather than a hundred times.

Today the ladder is asked at **resource-type** level, matching the permission
manifest's own split. Row-level precision — where one station is sensitive and
its neighbour is not — needs the row before deciding, which is
`object_authorizer`'s shape rather than this one.

When a request is denied, Wreath still runs your global finalizers — so the
`401` or `403` carries the same security headers and CORS treatment as a success
— but it skips the route middleware and your handler entirely. The person who
shouldn't be there never reaches your code, and the response that turns them away
is still well-formed.

**Reference:** [`wreath.auth`](../reference/auth.md),
[`wreath.authorization`](../reference/authorization.md).
