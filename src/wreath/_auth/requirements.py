"""Compiled endpoint authorization requirements."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True, slots=True)
class OAuthStepUpRequirement:
    """RFC 9470 authentication recency and class required by a resource."""

    max_age: int | None = None
    acr_values: tuple[str, ...] = ()
    challenge: str = field(init=False, compare=False)

    def __post_init__(self) -> None:
        max_age = self.max_age
        if max_age is not None:
            if isinstance(max_age, bool) or not isinstance(max_age, int):
                raise TypeError("OAuth step-up max_age must be a non-negative integer")
            if max_age < 0:
                raise ValueError("OAuth step-up max_age must be a non-negative integer")
        acr_values = tuple(self.acr_values)
        if len(set(acr_values)) != len(acr_values):
            raise ValueError("OAuth step-up acr_values must be unique")
        for value in acr_values:
            if (
                not isinstance(value, str)
                or not value
                or not value.isascii()
                or not value.isprintable()
                or any(character.isspace() for character in value)
                or '"' in value
                or "\\" in value
            ):
                raise ValueError(
                    "OAuth step-up acr_values entries must be non-empty ASCII "
                    "strings without whitespace"
                )
        if max_age is None and not acr_values:
            raise ValueError("OAuth step-up requires max_age or acr_values")
        object.__setattr__(self, "acr_values", acr_values)
        if max_age is not None and acr_values:
            description = (
                "More recent authentication and a different authentication level are required"
            )
        elif max_age is not None:
            description = "More recent authentication is required"
        else:
            description = "A different authentication level is required"
        parameters = [
            'Bearer error="insufficient_user_authentication"',
            f'error_description="{description}"',
        ]
        if max_age is not None:
            parameters.append(f'max_age="{max_age}"')
        if acr_values:
            parameters.append(f'acr_values="{" ".join(acr_values)}"')
        object.__setattr__(self, "challenge", ", ".join(parameters))

    def satisfied_by(self, identity: Any, now: float | None = None) -> bool:
        """Whether the token claims meet every declared RFC 9470 requirement."""
        claims = getattr(identity, "claims", None)
        if not isinstance(claims, Mapping):
            return False
        max_age = self.max_age
        if max_age is not None:
            stamp = claims.get("auth_time")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                return False
            if now is None or max(0, int(now - stamp)) > max_age:
                return False
        acr_values = self.acr_values
        return not acr_values or claims.get("acr") in acr_values


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
    #: Maximum age in seconds of the identity's second-factor proof.
    second_factor: float | None = None
    #: OAuth token authentication recency/class required under RFC 9470.
    oauth_step_up: OAuthStepUpRequirement | None = None
    #: Whether the endpoint explicitly admits anonymous callers.
    public: bool = False

    @property
    def needs_backend(self) -> bool:
        """Whether this endpoint needs identity resolution or an access check."""
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
            or self.oauth_step_up is not None
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


_EMPTY_REQUIREMENT = AuthRequirement()


def requirement_for(endpoint: Any) -> AuthRequirement:
    return getattr(endpoint, _METADATA, _EMPTY_REQUIREMENT)


def merge_requirements(*requirements: AuthRequirement) -> AuthRequirement:
    """Combine inherited requirements without allowing a child to weaken a parent."""
    public = False
    protected = False
    authenticated = False
    identify = False
    second_factor: float | None = None
    oauth_step_up: OAuthStepUpRequirement | None = None
    role_checks: list[SetRequirement] = []
    permission_checks: list[SetRequirement] = []
    policies: list[PolicyRequirement] = []
    for requirement in requirements:
        public = public or requirement.public
        protected = protected or requirement.identify or requirement.access_level > 0
        authenticated = authenticated or requirement.authenticated
        identify = identify or requirement.identify
        window = requirement.second_factor
        if window is not None and (second_factor is None or window < second_factor):
            second_factor = window
        oauth_step_up = _merge_oauth_step_up(oauth_step_up, requirement.oauth_step_up)
        role_checks.extend(requirement.role_checks)
        permission_checks.extend(requirement.permission_checks)
        policies.extend(requirement.policies)
    if public and protected:
        raise ValueError(
            "public() cannot be combined with authentication or authorization requirements"
        )
    if second_factor is not None and oauth_step_up is not None:
        raise ValueError(
            "session second_factor and OAuth oauth_step_up requirements cannot be combined"
        )
    return AuthRequirement(
        public=public,
        second_factor=second_factor,
        oauth_step_up=oauth_step_up,
        authenticated=authenticated,
        identify=identify,
        role_checks=tuple(role_checks),
        permission_checks=tuple(permission_checks),
        policies=tuple(policies),
    )


def _merge_oauth_step_up(
    left: OAuthStepUpRequirement | None,
    right: OAuthStepUpRequirement | None,
) -> OAuthStepUpRequirement | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.max_age is None:
        max_age = right.max_age
    elif right.max_age is None:
        max_age = left.max_age
    else:
        max_age = min(left.max_age, right.max_age)
    if left.acr_values and right.acr_values:
        accepted = frozenset(right.acr_values)
        acr_values = tuple(value for value in left.acr_values if value in accepted)
        if not acr_values:
            raise ValueError("OAuth step-up requirements have no authentication class in common")
    else:
        acr_values = left.acr_values or right.acr_values
    return OAuthStepUpRequirement(max_age=max_age, acr_values=acr_values)


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
    if current.oauth_step_up is not None:
        raise ValueError(
            "second_factor() cannot be combined with oauth_step_up(); choose the "
            "session or OAuth remediation flow"
        )
    window = max_age if current.second_factor is None else min(current.second_factor, max_age)
    return set_requirement(endpoint, replace(current, authenticated=True, second_factor=window))


def add_oauth_step_up(endpoint: Any, step_up: OAuthStepUpRequirement) -> Any:
    """Demand RFC 9470 token authentication claims. Implies authentication."""
    current = _protected_requirement(endpoint)
    if current.second_factor is not None:
        raise ValueError(
            "oauth_step_up() cannot be combined with second_factor(); choose the "
            "OAuth or session remediation flow"
        )
    merged = _merge_oauth_step_up(current.oauth_step_up, step_up)
    return set_requirement(endpoint, replace(current, authenticated=True, oauth_step_up=merged))


def add_roles(endpoint: Any, values: frozenset[str], mode: Mode) -> Any:
    return _append_check(endpoint, "role_checks", SetRequirement(values, mode))


def add_policy(endpoint: Any, action: str, resource: object | Callable[[Any], object]) -> Any:
    return _append_check(endpoint, "policies", PolicyRequirement(action, resource))


def add_permissions(endpoint: Any, values: frozenset[str], mode: Mode) -> Any:
    return _append_check(endpoint, "permission_checks", SetRequirement(values, mode))


def _append_check(endpoint: Any, field: str, check: Any) -> Any:
    current = _protected_requirement(endpoint)
    return set_requirement(
        endpoint,
        replace(
            current,
            authenticated=True,
            **{field: getattr(current, field) + (check,)},
        ),
    )
