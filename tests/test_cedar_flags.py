"""Feature flags as Cedar context.

A flag and a policy are two decision points answering one question -- *may this
caller do this now?* -- and separately they drift. Exposed as context, the flag
becomes an input to the one decision the authorizer already makes.

The representation is a **set of enabled names**, never a map. A map invites
`context.flags["x"] == false`, which reads as "explicitly off" when it may mean
"no such flag"; an authorization expression that cannot tell those apart will
eventually permit something by typo. Absent from the set means false, and deny
is the safe direction.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._auth.cedar_engine import CedarPolicies, EntityUid
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize
from wreath.flags import FeatureFlags, Flag
from wreath.testing import TestClient

FLAG_POLICY = """
permit(principal, action == Action::"read", resource)
when { context.flags.contains("new_ui") };
"""


ROLLOUT_POLICY = """
permit(principal, action == Action::"read", resource)
when { context.flags.contains("rollout") };
"""

MANIFEST_POLICY = """
permit(principal in Role::"rider", action == Action::"Llama::read", resource)
when { context.flags.contains("new_ui") };
"""

class CountingFlags(FeatureFlags):
    """A `FeatureFlags` that records each resolution, to prove there is only one."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        #: One resolution asks each referenced name once, so with a policy set
        #: naming a single flag the invariant is exactly one consultation.
        self.calls = 0
        #: How many policy evaluations that one resolution served. Read by the
        #: tests to prove the assertion above is not vacuous.
        self.evaluations = 0
        #: Every (name, answer) pair seen. A percentage flag re-bucketed within
        #: one request would put two answers here for one name.
        self.answers: set[tuple[str, bool]] = set()

    def resolve(self, flag: Flag[Any], context: Any = None) -> Any:
        self.calls += 1
        answer = super().resolve(flag, context)
        self.answers.add((flag.name, bool(answer)))
        return answer

    def all(self, context: Any = None) -> dict[str, bool]:
        self.calls += 1
        resolved = super().all(context)
        self.answers.update(resolved.items())
        return resolved


class CountingEngine:
    """`CedarPolicies`, counting evaluations onto the flag provider beside it."""

    def __init__(self, source: str, counter: Any) -> None:
        self._engine = CedarPolicies(source)
        self._counter = counter

    @property
    def source(self) -> str:
        return self._engine.source

    def referenced_flags(self) -> frozenset[str]:
        return self._engine.referenced_flags()

    def is_authorized(self, **ask: Any) -> Any:
        self._counter.evaluations += 1
        return self._engine.is_authorized(**ask)


def manifest_app(flags: Any, source: str = MANIFEST_POLICY) -> Wreath:
    """An app whose permission manifest drives many evaluations per request."""
    from wreath._auth.permissions import permissions_router

    app = Wreath()

    # Several actions across two resource types, so the manifest genuinely
    # drives many evaluations for one request. With a single action the
    # once-per-request assertion below would hold whether or not anything was
    # cached, and would be measuring nothing.
    @app.get("/llamas/{llama_id}")
    @authorize(action="Llama::read", resource="Llama::*")
    async def read_llama(request: Any) -> dict:
        return {"ok": True}

    @app.patch("/llamas/{llama_id}")
    @authorize(action="Llama::edit", resource="Llama::*")
    async def edit_llama(request: Any) -> dict:
        return {"ok": True}

    @app.delete("/llamas/{llama_id}")
    @authorize(action="Llama::delete", resource="Llama::*")
    async def delete_llama(request: Any) -> dict:
        return {"ok": True}

    @app.get("/treks/{trek_id}")
    @authorize(action="Trek::read", resource="Trek::*")
    async def read_trek(request: Any) -> dict:
        return {"ok": True}

    app.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(engine=CountingEngine(source, flags), flags=flags),
    )
    app.include_router(permissions_router(app))
    return app


async def invoke(app: Wreath, token: str, path: str = "/doc") -> dict[str, Any]:
    """Drive one authenticated request and return its response start message."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        },
        receive,
        send,
    )
    return sent[0]


async def verify(token: str) -> Identity | None:
    return Identity(token) if token in {"alice", "bob"} else None


def build(
    *,
    flags: Any = None,
    source: str = FLAG_POLICY,
    path: str = "/doc",
) -> Wreath:
    authorizer = CedarAuthorizer(engine=CedarPolicies(source), flags=flags)
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), authorizer)

    @app.get(path)
    @authorize(action="read", resource="Document::42")
    async def document(request: Any) -> str:
        return "allowed"

    return app


# --- the representation -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_enabled_flag_is_in_the_context_set_and_the_policy_permits() -> None:
    app = build(flags=FeatureFlags({"new_ui": "on"}))

    assert (await invoke(app, "alice"))["status"] == 200


@pytest.mark.asyncio
async def test_a_disabled_flag_is_absent_from_the_set_and_the_policy_denies() -> None:
    app = build(flags=FeatureFlags({"new_ui": "off"}))

    assert (await invoke(app, "alice"))["status"] == 403


@pytest.mark.asyncio
async def test_an_exactly_scoped_other_action_does_not_resolve_its_flags() -> None:
    flags = CountingFlags({"new_ui": "on"})
    app = build(
        flags=flags,
        source=(
            'permit(principal, action == Action::"read", resource);'
            'permit(principal, action == Action::"render", resource) '
            'when { context.flags.contains("new_ui") };'
        ),
    )

    assert (await invoke(app, "alice"))["status"] == 200
    assert flags.calls == 0


class OpaqueFlags:
    """A provider that answers one name at a time and cannot list its vocabulary.

    The shape an external service (Unleash, LaunchDarkly) has: `resolve` costs a
    lookup it can do, `names` would cost a round trip it may not want to make.
    It is what the `TypedFlagProvider` protocol requires, and it is the only
    place the "unknown flag denies at request time" path is reachable -- with an
    enumerable provider, startup validation refuses the policy set first.
    """

    def __init__(self, on: set[str]) -> None:
        self._on = on
        self.asked: list[str] = []

    def resolve(self, flag: Flag[Any], context: Any = None) -> Any:
        self.asked.append(flag.name)
        return flag.name in self._on


class LegacyOpaqueFlags:
    """The original public boolean provider shape, without enumeration."""

    def __init__(self, on: set[str]) -> None:
        self._on = on
        self.asked: list[str] = []

    def enabled(self, name: str, context: Any = None) -> bool:
        self.asked.append(name)
        return name in self._on


@pytest.mark.asyncio
async def test_a_flag_nobody_configured_is_absent_rather_than_false() -> None:
    """Absence is the whole argument for a set over a map.

    `contains` on a name the provider never held is false, exactly as it is for
    a name configured off -- so a typo in a policy denies rather than permitting
    whatever a map's missing key happened to compare as.
    """
    with pytest.warns(RuntimeWarning, match="cannot enumerate"):
        app = build(flags=OpaqueFlags({"other_flag"}))

    assert (await invoke(app, "alice"))["status"] == 403


@pytest.mark.asyncio
async def test_original_boolean_provider_uses_the_same_cedar_resolution_path() -> None:
    flags = LegacyOpaqueFlags({"new_ui"})
    with pytest.warns(RuntimeWarning, match="cannot enumerate"):
        app = build(flags=flags)

    assert (await invoke(app, "alice"))["status"] == 200
    assert flags.asked == ["new_ui"]


@pytest.mark.asyncio
async def test_no_provider_at_all_yields_an_empty_set_and_denies() -> None:
    """An application that never configured flags still evaluates flag policies.

    The set is empty rather than the key being missing, which is what makes the
    answer a denial rather than an evaluation accident.
    """
    app = build(flags=None)

    assert (await invoke(app, "alice"))["status"] == 403


# --- the empty set is the fail-closed choice, not a convenience ---------------


def test_an_absent_flags_key_lets_an_unless_forbid_fail_open() -> None:
    """Why the authorizer always supplies `flags`, even with no provider.

    This is the engine's behaviour, pinned as a regression guard: with no
    `flags` in the context at all, `forbid ... unless { flags.contains(...) }`
    is **skipped** and the request is allowed. An empty set makes the same
    policy deny. So an absent key is not a neutral default -- it silently
    disables every `unless`-shaped flag forbid, which is precisely the shape a
    kill-switch is written in.
    """
    engine = CedarPolicies(
        'permit(principal, action == Action::"read", resource);'
        'forbid(principal, action == Action::"read", resource)'
        ' unless { context.flags.contains("bypass") };'
    )
    ask = {
        "principal": EntityUid("User", "alice"),
        "action": EntityUid("Action", "read"),
        "resource": EntityUid("Doc", "1"),
        "entities": (),
    }

    absent = engine.is_authorized(context={}, **ask)
    empty = engine.is_authorized(context={"flags": frozenset()}, **ask)

    assert absent.allowed is True, "an absent key skips the forbid -- fails open"
    assert empty.allowed is False, "an empty set stands the forbid -- fails closed"


# --- resolved once per request ------------------------------------------------


@pytest.mark.asyncio
async def test_the_provider_is_asked_once_however_many_policies_evaluate() -> None:
    """A route behind several evaluations must not re-bucket between them.

    The manifest endpoint asks the authorizer once per (resource type, action),
    so one request drives many evaluations through one `CedarAuthorizer`. The
    provider must see exactly one resolution regardless.
    """
    counter = CountingFlags({"new_ui": "on"})
    app = manifest_app(counter)

    async with TestClient(app) as client:
        response = await client.acting_as("alice", roles=["rider"]).get(
            "/permissions/manifest"
        )

    assert response.status == 200
    assert counter.evaluations > 1, (
        "the manifest made one evaluation, so this asserts nothing about caching"
    )
    assert counter.calls == 1, f"resolved {counter.calls} times in one request"


@pytest.mark.asyncio
async def test_a_percentage_rollout_answers_the_same_way_all_request_long() -> None:
    """The determinism the whole design rests on.

    A percentage flag re-bucketed per policy could put one caller inside the
    rollout for a `permit` and outside it for a `forbid`. Resolving once makes
    that unrepresentable; this asserts the resolved set is one value, reused.
    """
    counter = CountingFlags({"rollout": "50%"})
    app = manifest_app(counter, source=ROLLOUT_POLICY)

    async with TestClient(app) as client:
        response = await client.acting_as("alice", roles=["rider"]).get(
            "/permissions/manifest"
        )

    assert response.status == 200
    assert counter.evaluations > 1, (
        "the manifest made one evaluation, so this asserts nothing about caching"
    )
    assert counter.calls == 1
    assert len(counter.answers) == 1, (
        f"the rollout was re-bucketed within one request: {counter.answers}"
    )


# --- validated where it is written --------------------------------------------


def test_a_policy_naming_an_unknown_flag_is_refused_at_startup() -> None:
    """The likeliest mistake, made loud.

    A misspelled name is simply absent from the set, so the condition is false
    and the policy denies forever with nothing to see. Refusing at startup turns
    that into a boot failure naming the flag.
    """
    with pytest.raises(ValueError, match="new_iu"):
        CedarAuthorizer(
            engine=CedarPolicies(
                'permit(principal, action, resource)'
                ' when { context.flags.contains("new_iu") };'
            ),
            flags=FeatureFlags({"new_ui": "on"}),
        )


def test_a_provider_that_cannot_enumerate_is_warned_about_not_guessed_at() -> None:
    """Half a condition is knowable, so say so where it is written.

    An external provider may not be able to list its vocabulary without a
    network call. Refusing on a guess would break a working deployment; staying
    silent would hide the same typo the branch above catches.
    """
    with pytest.warns(RuntimeWarning, match="cannot enumerate"):
        CedarAuthorizer(
            engine=CedarPolicies(
                'permit(principal, action, resource)'
                ' when { context.flags.contains("anything") };'
            ),
            flags=OpaqueFlags(set()),
        )


def test_a_policy_set_naming_no_flag_is_never_warned_about() -> None:
    """The overwhelmingly common app must not pay a warning for a feature it
    does not use."""
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        CedarAuthorizer(
            engine=CedarPolicies("permit(principal, action, resource);"),
            flags=OpaqueFlags(set()),
        )


def test_every_referenced_name_is_found_wherever_it_is_written() -> None:
    """`contains` is legal anywhere an expression is, so the walk is generic.

    A visitor that knew only the top-level shapes would validate half a policy
    set and silently pass the rest.
    """
    engine = CedarPolicies(
        'permit(principal, action, resource) when {'
        '  context.flags.contains("top")'
        '  && (context.flags.contains("nested") || context.method == "GET")'
        '};'
        'forbid(principal, action, resource)'
        ' unless { context.flags.containsAny(["either", "or"]) };'
    )

    assert engine.referenced_flags() == {"top", "nested", "either", "or"}


def test_a_policy_set_reading_flags_opaquely_reports_that_it_cannot_enumerate() -> None:
    """`isEmpty()` names no flag, so the referenced set is not the whole story.

    The authorizer resolves only the names a policy set references, which is
    exact when every reference is a literal and *wrong* when one is not: under
    `context.flags.isEmpty()` an unreferenced flag that is on still has to make
    the set non-empty. Reporting `None` is how the engine says "you need them
    all", and is what keeps the optimisation from changing an answer.
    """
    assert CedarPolicies(
        'permit(principal, action, resource) when { context.flags.isEmpty() };'
    ).referenced_flags() is None

    assert CedarPolicies(
        'permit(principal, action, resource)'
        ' when { context.flags.contains(context.method) };'
    ).referenced_flags() is None


@pytest.mark.asyncio
async def test_an_opaque_flag_read_still_sees_a_flag_no_policy_names() -> None:
    """The behaviour the report above protects, end to end.

    And it happens *silently*: an enumerable provider can supply every flag, so
    the opaque read costs a full resolution and nothing else. The warning is
    reserved for the case where nothing can be resolved at all, or it would fire
    for a working configuration and stop being read.
    """
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        app = build(
            flags=FeatureFlags({"unnamed": "on"}),
            source=(
                'permit(principal, action == Action::"read", resource)'
                ' when { !context.flags.isEmpty() };'
            ),
        )

    assert (await invoke(app, "alice"))["status"] == 200


def test_no_provider_is_neither_refused_nor_warned_about() -> None:
    """Deciding not to configure flags is a decision, not a mistake.

    Every flag test denies, which is documented and safe, so a policy set that
    reads flags against no provider must boot in silence -- warning here would
    train the reader to ignore the warning that means something.
    """
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        CedarAuthorizer(engine=CedarPolicies(FLAG_POLICY), flags=None)


def test_the_rollout_subject_is_the_principal_not_the_anonymous_bucket() -> None:
    """A percentage flag must bucket per caller, and identically to a handler.

    Every anonymous caller shares one bucket, so if the authorizer forgot to
    pass the identity, a rollout would be all-or-nothing for signed-in users
    too -- and would disagree with `flags_dependency` in the same request.
    """
    from wreath._auth.cedar import request_flags

    seen: list[Any] = []

    class Recording:
        def resolve(self, flag: Flag[Any], context: Any = None) -> bool:
            seen.append(context)
            return True

    class FakeRequest:
        def __init__(self, identity: Any) -> None:
            self.identity = identity
            self.state = type("S", (), {"get": lambda s, k, default=None: default,
                                        "__setattr__": object.__setattr__})()

    request_flags(FakeRequest(Identity("alice")), Recording(), frozenset({"f"}))
    request_flags(FakeRequest(None), Recording(), frozenset({"f"}))

    assert seen[0] == {"id": "alice"}, "the principal never reached the bucket"
    assert seen[1] == {}, "an anonymous caller must not invent a subject"


@pytest.mark.asyncio
async def test_the_eager_path_keeps_only_the_flags_that_are_on() -> None:
    """The `isEmpty()` fallback resolves every flag, and must still filter.

    Without the filter every *configured* flag would land in the set, so a flag
    explicitly turned off would read as on -- the exact inversion the whole
    feature exists to get right.
    """
    app = build(
        flags=FeatureFlags({"live": "on", "dark": "off"}),
        source=(
            'permit(principal, action == Action::"read", resource)'
            ' when { !context.flags.isEmpty() && !context.flags.contains("dark") };'
        ),
    )

    assert (await invoke(app, "alice"))["status"] == 200


@pytest.mark.asyncio
async def test_a_provider_that_can_neither_enumerate_nor_be_asked_gives_nothing() -> None:
    """The one combination with no answer available, and it denies.

    An opaque provider can only answer names it is given, and an `isEmpty()`
    policy supplies none -- so nothing can be resolved. The set is empty and the
    policy denies, rather than the request erroring or the flags being guessed.
    """
    with pytest.warns(RuntimeWarning, match="every flag test will deny"):
        app = build(
            flags=OpaqueFlags({"live"}),
            source=(
                'permit(principal, action == Action::"read", resource)'
                ' when { !context.flags.isEmpty() && context.flags.contains("live") };'
            ),
        )

    assert (await invoke(app, "alice"))["status"] == 403


@pytest.mark.asyncio
async def test_an_engine_that_cannot_introspect_falls_back_to_every_flag() -> None:
    """An outside evaluator must not have its flag vocabulary guessed at.

    `referenced_flags` is optional, so an engine without it reports nothing
    knowable and the authorizer resolves the provider's whole set. A short list
    there would silently drop a flag the foreign policy set actually reads.
    """
    class Opaque:
        """A `CedarEngine` with no introspection, permitting only on a flag."""

        def is_authorized(self, **ask: Any) -> bool:
            return "live" in ask["context"]["flags"]

    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(engine=Opaque(), flags=FeatureFlags({"live": "on"})),
    )

    @app.get("/doc")
    @authorize(action="read", resource="Document::42")
    async def document(request: Any) -> str:
        return "allowed"

    assert (await invoke(app, "alice"))["status"] == 200


# --- the manifest cannot serve chrome for a flag that has since flipped -------


@pytest.mark.asyncio
async def test_the_manifest_etag_moves_when_a_flag_flips() -> None:
    """A flag flip changes the answer, so it must move the tag.

    Without this a client holds a manifest drawn for the old flag state and
    keeps revalidating it successfully -- chrome for something now denied, with
    no event that could ever correct it.
    """
    flags = CountingFlags({"new_ui": "on"})
    app = manifest_app(flags)

    async with TestClient(app) as client:
        rider = client.acting_as("alice", roles=["rider"])
        before = await rider.get("/permissions/manifest")
        flags._values["new_ui"] = "off"          # the flip
        after = await rider.get("/permissions/manifest")

    assert before.status == 200 and after.status == 200
    assert before.header("etag") != after.header("etag"), "a flip left the tag still"
    assert before.json()["allowed"] != after.json()["allowed"]
