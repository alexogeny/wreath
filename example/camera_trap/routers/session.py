"""Signing in, and what the session carries.

**This is a deliberately small login and the example says so out loud.** It
takes an observer's email, looks the row up, and writes who they are into a
signed session cookie. There is no password, no OIDC dance, and no second
factor, because every one of those would double the setup a reader must do
before they can see an authorization rule work — and the cookbook already owns
that ground: [OIDC / OAuth2](../../docs/cookbook/recipes/oauth2-login.md) and
[API keys](../../docs/cookbook/recipes/api-key-auth.md) are real recipes against
the same seam this router uses. Swap this file for one of those and nothing
else in the example changes, which is the point of the seam.

**What the session carries is the interesting part.** The principal written here
is `{"sub", "type", "roles"}`, and `SessionIdentityBackend` reads exactly that
back into a `wreath.auth.Identity` on every later request. The role comes out of
the `observers` table at sign-in, so Cedar's `principal in Role::"ranger"` works
with no further wiring: the framework's default identity mapper turns
`Identity.roles` into `Role::"..."` parents.

Reading the role once, at sign-in, is a real trade and it is the usual one. It
costs a stale session when someone is promoted or suspended — bounded by the
cookie's `max_age` — and it saves a database read on every authenticated
request. An application that cannot tolerate that staleness re-reads the row in
its own backend; this one revokes by clearing the session.
"""

from __future__ import annotations

from typing import Annotated

from wreath import Request, Response, Router
from wreath.exceptions import UnprocessableEntity
from wreath.orm import FromORM, Session

from ..models import Observer
from ..policies import ROLES

ReadSession = Annotated[Session, FromORM("main", workload="read")]

#: The key `SessionIdentityBackend` reads by default, and the one
#: `wreath.users.user_router` writes when this router is eventually replaced by
#: it. Naming it once here keeps the two ends of that swap honest.
PRINCIPAL_KEY = "principal"

session = Router(prefix="/session", tags=("session",))


@session.post("/", summary="Sign in as an observer")
async def sign_in(request: Request, db: ReadSession, email: str) -> dict:
    """Establish a session for the observer with this email address.

    `email` is bound from the form or query rather than a JSON body so the
    console's plain HTML form posts to the same route the API uses. There is
    exactly one identity source, which is what makes the console and the API
    agree about who you are.
    """
    found = await db.fetch_one(Observer.select().where(Observer.email == email.lower()))
    if found is None:
        # 422 rather than 404: the address is the *input* that is wrong, and a
        # 404 on a login route tells an unauthenticated caller which addresses
        # are registered.
        raise UnprocessableEntity("no observer with that email address")
    if found.role not in ROLES:
        # A row whose role is outside the vocabulary would sign in with no Cedar
        # parents at all and see nothing, which reads as a policy bug rather
        # than the data bug it is. Refusing names it.
        raise UnprocessableEntity(f"observer {found.email!r} has unknown role {found.role!r}")
    request.state.session[PRINCIPAL_KEY] = {
        "sub": str(found.id),
        "type": "Observer",
        "roles": [found.role],
        # Carried so the registry's row-level check can compare a station's
        # reserve against the observer's without re-reading the `observers` row
        # for every row of every page. Null for researchers and rangers, who
        # work across reserves -- the nullability is the domain's.
        "reserve_id": found.reserve_id,
    }
    return {"id": found.id, "display_name": found.display_name, "role": found.role}


@session.delete("/", summary="Sign out")
async def sign_out(request: Request) -> Response:
    """Clear the session.

    `pop` rather than assigning an empty principal: the backend tests for the
    key's presence, and an empty mapping under it is an identity with no
    subject, which is a shape nothing downstream expects.

    The status is set by returning a `Response` rather than by a `status_code=`
    route argument, which wreath does not have: a handler either returns a value
    to be coerced into a 200 or returns the response it wants. One mechanism,
    visible at the point it applies.
    """
    request.state.session.pop(PRINCIPAL_KEY, None)
    return Response(b"", status=204)


@session.get("/", summary="Who am I, and what may I do")
async def whoami(request: Request) -> dict:
    """The signed-in identity, or an explicit anonymous answer.

    A 200 with `signed_in: false` rather than a 401, because "am I signed in"
    is a question an anonymous caller is entitled to ask. The console calls it
    on load to decide whether to render the sign-in form.

    **This reads the session rather than `request.identity`, and it has to.**
    `request.identity` is populated only on an endpoint that declares an
    authentication requirement — `authenticated()` is what asks the backend, and
    every other auth decorator implies it. This route cannot declare one without
    turning the anonymous case into the 401 it exists to avoid, so reading
    `request.identity` here returns `None` for *everyone*, and the console shows
    a sign-in form to someone who is already signed in. It did, until a test
    signed in and then asked.

    Reading the session directly is not a workaround for that. It is the
    narrower question: this route reports whether a session cookie is present
    and valid, which is precisely what the console needs, and `SessionPolicy`
    is global so the session is on every request whether or not anyone was
    authenticated. What this route deliberately does *not* answer is what the
    observer may do — that is `permissions_router`'s job, derived from the same
    `@authorize` declarations that enforce it rather than from a second list
    maintained here.
    """
    principal = request.state.session.get(PRINCIPAL_KEY)
    if principal is None:
        return {"signed_in": False}
    return {
        "signed_in": True,
        "id": principal["sub"],
        "roles": sorted(principal["roles"]),
    }
