"""Compiled endpoint authorization requirements."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Literal

Mode = Literal["all", "any"]
_METADATA = "__wreath_auth_requirement__"

#: Where a completed second factor is recorded: on the session principal
#: `wreath.users` writes, and therefore on `Identity.claims`, as Unix seconds.
#: One name, read by the endpoint requirement and by the Cedar context mapper,
#: so a rename moves both rather than silently disagreeing with one of them.
SECOND_FACTOR_CLAIM = "second_factor_at"


def second_factor_age(identity: Any, now: float) -> int | None:
    """Seconds since `identity` last proved a second factor, or None for never.

    **None is a refusal, not a zero.** Every caller treats an absent stamp as
    "has not proved one", which is what makes the default fail closed for an
    identity minted by a flow that knows nothing about second factors -- a
    bearer token, an OIDC login -- rather than admitting it as infinitely fresh.

    A stamp in the future reads as age zero rather than as a negative age. A
    clock that stepped backwards, or a claim somebody wrote by hand, should not
    be able to be *fresher than fresh* in an arithmetic comparison elsewhere.

    Returns an `int` because it is handed to the Cedar engine as a context
    value, and Cedar has i64 longs and no floats.
    """
    claims = getattr(identity, "claims", None)
    if not isinstance(claims, Mapping):
        return None
    stamp = claims.get(SECOND_FACTOR_CLAIM)
    # `bool` is an `int`, and `True` would otherwise read as a 1970 timestamp
    # and so as an ancient-but-present factor.
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    return max(0, int(now - stamp))


@dataclass(frozen=True, slots=True)
class SetRequirement:
    values: frozenset[str]
    mode: Mode


@dataclass(frozen=True, slots=True)
class PolicyRequirement:
    action: str
    resource: object | Callable[[Any], object]


@dataclass(frozen=True, slots=True, init=False)
class AuthorizationVocabulary:
    """The complete, typed set of policy actions an application may declare.

    Build it from one or more `StrEnum` classes and use those enum members in
    `@authorize`. Wreath stores the wire value, so the route
    manifest and Cedar still see ordinary strings while a misspelled action is
    caught by the type checker before startup.
    """

    actions: tuple[str, ...]

    def __init__(self, *enums: type[StrEnum]) -> None:
        if not enums:
            raise ValueError("an authorization vocabulary needs at least one StrEnum")
        actions: list[str] = []
        seen: set[str] = set()
        duplicates: set[str] = set()
        for enum in enums:
            if not isinstance(enum, type) or not issubclass(enum, StrEnum):
                raise TypeError("authorization vocabularies are built from StrEnum classes")
            for member in enum:
                if member.value in seen:
                    duplicates.add(member.value)
                seen.add(member.value)
                actions.append(member.value)
        if any(not action for action in actions):
            raise ValueError("authorization actions cannot be empty")
        if duplicates:
            raise ValueError(
                "authorization actions must be unique: " + ", ".join(sorted(duplicates))
            )
        object.__setattr__(self, "actions", tuple(sorted(actions)))

    def unknown(self, actions: Iterable[str]) -> tuple[str, ...]:
        """Declared actions absent from this vocabulary, sorted for diagnostics."""
        declared = set(actions)
        return tuple(sorted(declared.difference(self.actions)))

    def unused(self, actions: Iterable[str]) -> tuple[str, ...]:
        """Vocabulary actions no route or mounted surface currently declares."""
        declared = set(actions)
        return tuple(sorted(set(self.actions).difference(declared)))


@dataclass(frozen=True, slots=True)
class AuthRequirement:
    authenticated: bool = False
    #: Run the backend and publish `request.identity`, but admit an anonymous
    #: caller. Deliberately *not* folded into `authenticated`: that flag means
    #: "refuse without an identity", and this one means "ask, and accept either
    #: answer". Collapsing them would make an identity-reading public route
    #: indistinguishable from a protected one, which is the whole distinction.
    identify: bool = False
    role_checks: tuple[SetRequirement, ...] = ()
    permission_checks: tuple[SetRequirement, ...] = ()
    policies: tuple[PolicyRequirement, ...] = ()
    #: Seconds within which the identity must have proved a second factor, or
    #: None for "no such requirement". Not a boolean, because *recency* is the
    #: whole point: an identity that entered a code eight hours ago has a second
    #: factor and has not proved it lately, and only one of those two facts is
    #: interesting before a destructive action.
    second_factor: float | None = None
    #: An explicit declaration that this endpoint intentionally admits an
    #: anonymous caller. It changes no request behavior; its value is that a
    #: strict application can distinguish a reviewed public route from an
    #: endpoint whose authentication decorator was forgotten.
    public: bool = False

    @property
    def needs_backend(self) -> bool:
        """Whether the authentication backend has to run for this endpoint.

        Both requirements need the backend; only `authenticated` refuses when it
        yields nothing. Dispatch asks this rather than reading `authenticated`
        directly, because an `identify()` route that skipped the backend would
        publish `None` to a handler holding a perfectly good session -- the
        defect this flag exists to fix.

        Derived from `access_level` rather than from `authenticated`, so every
        way of asking something of the caller runs the backend. The two spellings
        agreed only because every decorator sets `authenticated` alongside
        whatever else it sets; a requirement built directly does not have to,
        and one whose checks never ran because the backend never ran is a check
        that reports safety while providing none.
        """
        return self.identify or self.access_level > 0

    @property
    def declares_access(self) -> bool:
        """Whether the endpoint explicitly says who may call it."""
        return self.public or self.identify or self.access_level > 0

    @property
    def access_level(self) -> int:
        """How much this endpoint asks of the caller: 0 nothing, 1 a principal,
        2 an administrator.

        The single definition of "is anything required here", asked by the HTTP
        and WebSocket dispatchers, by `Wreath._authorize_request`, and by MCP's
        `_authorize` -- which is the point. Four spellings of this question
        existed, differing in which fields they remembered to name, and
        `second_factor` was in only one of them: MCP admitted a tool declaring
        `AuthRequirement(second_factor=300)` without ever asking for the factor,
        because it read a level that did not count it. Every field that can
        refuse a caller is named here exactly once.

        `identify` is deliberately absent: it asks the backend to answer, not
        the caller to satisfy anything, and an anonymous caller is one of its
        two acceptable answers. `needs_backend` adds it back for the one
        question it does change.
        """
        if not (
            self.authenticated
            or self.role_checks
            or self.permission_checks
            or self.policies
            or self.second_factor is not None
        ):
            # The public-route answer first, and without building a generator
            # for the admin scan: `Wreath._auth_handlers` decides this once per
            # compile, but MCP asks it per call and the cheap case is the
            # common one.
            return 0
        for check in self.role_checks:
            if check.mode == "all" and check.values == {"admin"}:
                return 2
        return 1


def requirement_for(endpoint: Any) -> AuthRequirement:
    return getattr(endpoint, _METADATA, AuthRequirement())


def merge_requirements(*requirements: AuthRequirement) -> AuthRequirement:
    """Combine inherited requirements without allowing a child to weaken a parent."""
    public = any(requirement.public for requirement in requirements)
    protected = any(
        requirement.identify or requirement.access_level > 0 for requirement in requirements
    )
    if public and protected:
        raise ValueError(
            "public() cannot be combined with authentication or authorization requirements"
        )
    # The strictest window wins, which is the same rule the rest of this
    # function follows in a different shape: a router that demands a factor
    # within five minutes must not be relaxed to an hour by a route mounted
    # inside it. `None` is absence rather than "unbounded", so it is skipped.
    windows = [
        requirement.second_factor
        for requirement in requirements
        if requirement.second_factor is not None
    ]
    return AuthRequirement(
        public=public,
        second_factor=min(windows) if windows else None,
        authenticated=any(requirement.authenticated for requirement in requirements),
        identify=any(requirement.identify for requirement in requirements),
        role_checks=tuple(
            check for requirement in requirements for check in requirement.role_checks
        ),
        permission_checks=tuple(
            check for requirement in requirements for check in requirement.permission_checks
        ),
        policies=tuple(policy for requirement in requirements for policy in requirement.policies),
    )


def set_requirement(endpoint: Any, requirement: AuthRequirement) -> Any:
    setattr(endpoint, _METADATA, requirement)
    return endpoint


def _protected_requirement(endpoint: Any) -> AuthRequirement:
    current = requirement_for(endpoint)
    if current.public:
        raise ValueError(
            "public() cannot be combined with authentication or authorization requirements"
        )
    return current


def add_public(endpoint: Any) -> Any:
    current = requirement_for(endpoint)
    if current.identify or current.access_level > 0:
        raise ValueError(
            "public() cannot be combined with authentication or authorization requirements"
        )
    return set_requirement(endpoint, replace(current, public=True))


def add_authenticated(endpoint: Any) -> Any:
    return set_requirement(endpoint, replace(_protected_requirement(endpoint), authenticated=True))


def add_identify(endpoint: Any) -> Any:
    return set_requirement(endpoint, replace(_protected_requirement(endpoint), identify=True))


def add_second_factor(endpoint: Any, max_age: float) -> Any:
    """Demand a second factor proved within `max_age` seconds. Implies authentication.

    Two decorators on one endpoint keep the shorter window, for the same reason
    `merge_requirements` does: stacking requirements adds, never subtracts.
    """
    current = _protected_requirement(endpoint)
    window = max_age if current.second_factor is None else min(current.second_factor, max_age)
    return set_requirement(endpoint, replace(current, authenticated=True, second_factor=window))


def add_roles(endpoint: Any, values: frozenset[str], mode: Mode) -> Any:
    return _append_check(endpoint, "role_checks", SetRequirement(values, mode))


def add_policy(endpoint: Any, action: str, resource: object | Callable[[Any], object]) -> Any:
    return _append_check(endpoint, "policies", PolicyRequirement(action, resource))


def add_permissions(endpoint: Any, values: frozenset[str], mode: Mode) -> Any:
    return _append_check(endpoint, "permission_checks", SetRequirement(values, mode))


def _append_check(endpoint: Any, field: str, check: Any) -> Any:
    """Append one authentication check without three copies of the merge."""
    current = _protected_requirement(endpoint)
    return set_requirement(
        endpoint,
        replace(
            current,
            authenticated=True,
            **{field: getattr(current, field) + (check,)},
        ),
    )
