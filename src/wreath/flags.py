"""Feature flags with a small, dependency-free rule language.

A flag's value (from `WREATH_FLAG_<NAME>` env vars or an explicit mapping) is
evaluated per request:

- `on`/`true`/`1`/`yes`/`enabled` -> on
- `off`/`false`/`0`/`no`/`disabled`/`""` -> off
- `"25%"` -> a *deterministic* percentage rollout

Evaluation is fail-closed. A value that is neither a known word nor a parseable
percentage is off, and so is a flag nobody configured -- a typo never turns a
flag on, and neither does an unreachable provider.

The rollout is deterministic rather than random. The bucket is the first four
bytes of `blake2s(f"{flag}:{subject}")` taken modulo 100, and the subject is the
first truthy value among the context keys `id`, `key`, `user`, `subject`.
Nothing there is per-process state, so a subject lands in the same bucket in every
worker, on every host, after a restart, and in a replayed request. Hashing the
flag name alongside the subject means two flags at 25% do not select the same
quarter of the population.

A caller with no subject hashes against the empty string, so **every anonymous
caller shares one bucket** and a percentage flag is all-or-nothing for them.
Bucket on an authenticated principal when the split has to be a real split.

There is no source layering: a `FeatureFlags` answers from exactly one mapping,
and there is no fallback chain behind it. `TypedFlagProvider.resolve` is the
canonical provider operation: `FeatureFlags` serves env/mapping values and
`OpenFeatureProvider` adapts an application-owned OpenFeature client. The
original boolean `FlagProvider.enabled` protocol remains accepted through one
adapter, so existing providers keep working without creating a second internal
resolution path.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from ._auth.models import qualified_identity_key, qualified_identity_value
from .config import read_osenv

FLAG_PREFIX = "WREATH_FLAG_"

_TRUE = frozenset({"on", "true", "1", "yes", "enabled"})
_FALSE = frozenset({"off", "false", "0", "no", "disabled", ""})

__all__ = [
    "FLAG_PREFIX",
    "FeatureFlags",
    "Flag",
    "FlagProvider",
    "FlagSet",
    "FlagView",
    "OpenFeatureProvider",
    "TypedFlagProvider",
]

type FlagScalar = bool | str | int | float


@dataclass(frozen=True, slots=True)
class Flag[T: FlagScalar]:
    """One typed flag declaration with its fail-closed/default value."""

    name: str
    default: T

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("flag name must be a non-empty string")
        if type(self.default) not in (bool, str, int, float):
            raise TypeError("flag default must be bool, str, int, or float")


def _resolved[T: FlagScalar](flag: Flag[T], value: object) -> T:
    expected = type(flag.default)
    if type(value) is not expected:
        raise TypeError(
            f"flag {flag.name!r} expects {expected.__name__}; "
            f"provider returned {type(value).__name__}"
        )
    return cast(T, value)


@runtime_checkable
class FlagProvider(Protocol):
    """The original boolean provider contract, retained for compatibility.

    Framework integrations adapt this operation once to
    `TypedFlagProvider.resolve`; new providers should implement that typed
    contract. Keeping this protocol preserves existing structural providers and
    `isinstance(provider, FlagProvider)` checks.
    """

    def enabled(self, name: str, context: Mapping[str, Any] | None = None) -> bool:
        """Whether `name` is on for `context`."""
        ...


@runtime_checkable
class TypedFlagProvider(Protocol):
    """The canonical typed provider operation used inside Wreath."""

    def resolve[T: FlagScalar](
        self, flag: Flag[T], context: Mapping[str, Any] | None = None
    ) -> T: ...


class _LegacyFlagAdapter:
    """Layer the original boolean protocol over the canonical typed operation."""

    __slots__ = ("_provider",)

    def __init__(self, provider: FlagProvider) -> None:
        self._provider = provider

    def resolve[T: FlagScalar](self, flag: Flag[T], context: Mapping[str, Any] | None = None) -> T:
        if type(flag.default) is not bool:
            raise TypeError(
                f"flag {flag.name!r} has a {type(flag.default).__name__} default; "
                "a boolean FlagProvider can resolve only Flag(name, False). "
                "Implement TypedFlagProvider.resolve(flag, context) for typed flags"
            )
        return _resolved(flag, self._provider.enabled(flag.name, context))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


def _flag_resolver(provider: Any) -> TypedFlagProvider:
    """Normalize either public provider shape to the one internal operation."""
    if callable(getattr(provider, "resolve", None)):
        return cast(TypedFlagProvider, provider)
    if callable(getattr(provider, "enabled", None)):
        return _LegacyFlagAdapter(cast(FlagProvider, provider))
    raise TypeError(
        "feature-flag provider must expose resolve(flag, context), or the "
        "legacy enabled(name, context) boolean operation"
    )


class OpenFeatureProvider:
    """Adapt an OpenFeature client to Wreath's typed declaration surface.

    The adapter is structural: pass a client from the OpenFeature SDK you have
    configured, and Wreath adds no mandatory dependency or process-global
    provider. `from_default_client` is the opt-in convenience that imports the
    optional SDK and reads its configured default client.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        methods = (
            "get_boolean_value",
            "get_string_value",
            "get_integer_value",
            "get_float_value",
        )
        missing = tuple(name for name in methods if not callable(getattr(client, name, None)))
        if missing:
            raise TypeError("OpenFeature client is missing " + ", ".join(missing))
        self._client = client

    @classmethod
    def from_default_client(cls, name: str = "wreath") -> OpenFeatureProvider:
        try:
            api = importlib.import_module("openfeature.api")
        except ImportError as exc:
            raise RuntimeError(
                "OpenFeatureProvider.from_default_client requires the optional "
                "'openfeature-sdk' package; install openfeature-sdk"
            ) from exc
        return cls(api.get_client(name))

    def resolve[T: FlagScalar](self, flag: Flag[T], context: Mapping[str, Any] | None = None) -> T:
        method = {
            bool: self._client.get_boolean_value,
            str: self._client.get_string_value,
            int: self._client.get_integer_value,
            float: self._client.get_float_value,
        }[type(flag.default)]
        return _resolved(flag, method(flag.name, flag.default, context))

    def enabled(self, name: str, context: Mapping[str, Any] | None = None) -> bool:
        """Boolean convenience over the canonical typed provider seam."""
        return self.resolve(Flag(name, False), context)


class FlagSet:
    """A startup-validated vocabulary bound to one typed provider."""

    __slots__ = ("_by_name", "_provider")

    def __init__(
        self,
        provider: TypedFlagProvider | FlagProvider,
        flags: tuple[Flag[Any], ...],
    ) -> None:
        by_name: dict[str, Flag[Any]] = {}
        for flag in flags:
            key = flag.name.lower()
            if key in by_name:
                raise ValueError(f"duplicate flag declaration: {flag.name}")
            by_name[key] = flag
        resolved_provider = _flag_resolver(provider)
        if isinstance(resolved_provider, _LegacyFlagAdapter):
            for flag in flags:
                if type(flag.default) is not bool:
                    raise TypeError(
                        f"flag {flag.name!r} has a "
                        f"{type(flag.default).__name__} default; a boolean "
                        "FlagProvider supports only bool declarations. Implement "
                        "TypedFlagProvider.resolve(flag, context) for typed flags"
                    )
        self._provider = resolved_provider
        self._by_name = by_name

    def value[T: FlagScalar](self, flag: Flag[T], context: Mapping[str, Any] | None = None) -> T:
        declared = self._by_name.get(flag.name.lower())
        if declared is None:
            raise KeyError(f"flag {flag.name!r} is not declared; add that Flag to FlagSet")
        if type(declared.default) is not type(flag.default):
            raise TypeError(
                f"flag {flag.name!r} was declared as {type(declared.default).__name__}, "
                f"not {type(flag.default).__name__}"
            )
        return _resolved(flag, self._provider.resolve(declared, context))

    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)


def _subject(context: Mapping[str, Any] | None) -> str:
    if not context:
        return ""
    identity_key = context.get("_wreath_identity_key")
    if identity_key:
        return str(identity_key)
    for key in ("id", "key", "user", "subject"):
        value = context.get(key)
        if value:
            return str(value)
    return ""


def _bucket(name: str, subject: str) -> int:
    digest = hashlib.blake2s(f"{name}:{subject}".encode()).hexdigest()[:8]
    return int(digest, 16) % 100


def evaluate_rule(value: str, name: str, context: Mapping[str, Any] | None = None) -> bool:
    """Evaluate one flag value against the rule language (see module docstring).

    `name` is hashed into the percentage bucket, so pass the spelling the provider
    stores -- `FeatureFlags` lower-cases -- or two call sites bucket a subject
    differently for the same flag. Comparison of the value itself is
    case-insensitive and ignores surrounding whitespace.

    A percentage is parsed as a `float`, so `"12.5%"` is a valid split; `0%`
    is never on and `100%` always is. Anything unparseable is off.
    """
    token = value.strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    if token.endswith("%"):
        try:
            percent = float(token[:-1])
        except ValueError:
            return False
        if not 0 <= percent <= 100:
            return False
        return _bucket(name, _subject(context)) < percent
    return False


class FeatureFlags:
    """A mapping-backed typed and boolean provider (env-sourced or explicit).

    Flag names fold to lower case on the way in and on every lookup, so
    `WREATH_FLAG_NEW_UI` and `enabled("new_ui")` name the same flag. Values are
    stored raw and interpreted per request by `evaluate_rule`, so a rollout
    percentage changes by rewriting the mapping -- nothing is precomputed and no
    subject is bucketed until it asks.

    Args:
        values: flag name to raw value; names lower-cased, values coerced to `str`
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = {key.lower(): str(val) for key, val in (values or {}).items()}

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> FeatureFlags:
        """Collect `WREATH_FLAG_<NAME>` entries from the environment."""
        env = environ if environ is not None else read_osenv()
        values = {
            key[len(FLAG_PREFIX) :]: val for key, val in env.items() if key.startswith(FLAG_PREFIX)
        }
        return cls(values)

    def enabled(self, name: str, context: Mapping[str, Any] | None = None) -> bool:
        """Whether `name` is on for `context`; a flag not held here is off.

        `context` is read only to find the rollout subject, and only when the
        value is a percentage, so an on/off flag costs a dict lookup and a set
        membership test.
        """
        if type(self) is not FeatureFlags:
            return self.resolve(Flag(name, False), context)
        if not name:
            raise ValueError("flag name cannot be empty")
        key = name.lower()
        raw = self._values.get(key)
        return False if raw is None else evaluate_rule(raw, key, context)

    def resolve[T: FlagScalar](self, flag: Flag[T], context: Mapping[str, Any] | None = None) -> T:
        """Resolve one typed declaration; malformed or absent values use its default."""
        raw = self._values.get(flag.name.lower())
        if raw is None:
            return flag.default
        default = flag.default
        if type(default) is bool:
            return cast(T, evaluate_rule(raw, flag.name.lower(), context))
        try:
            if type(default) is str:
                value: FlagScalar = raw
            elif type(default) is int:
                value = int(raw)
            else:
                value = float(raw)
        except ValueError:
            return default
        return cast(T, value)

    def all(self, context: Mapping[str, Any] | None = None) -> dict[str, bool]:
        """Resolve every configured flag for `context`, keyed by lower-cased name.

        Only flags this provider holds appear. A flag the application asks about
        but nobody configured is absent here and off at `enabled`, so this is a
        view of the configuration rather than of the application's flag vocabulary.
        """
        if type(self) is not FeatureFlags:
            return {name: self.resolve(Flag(name, False), context) for name in self._values}
        return {name: evaluate_rule(raw, name, context) for name, raw in self._values.items()}

    def names(self) -> frozenset[str]:
        """Every flag name this provider holds, lower-cased.

        The enumeration half of the provider surface, and deliberately *not* on
        the `TypedFlagProvider` protocol: an external provider (Unleash,
        LaunchDarkly) may not be able to list its vocabulary without a network
        call, and a protocol method that some implementations cannot answer is
        worse than an optional one they can be asked for.

        A caller that needs the vocabulary probes for this with `getattr` and
        degrades when it is absent -- `CedarAuthorizer` validates the flag names
        its policies reference against it at startup when it is there, and warns
        where the policies were written when it is not.
        """
        return frozenset(self._values)

    def view(self, context: Mapping[str, Any] | None = None) -> FlagView:
        """Bind `context` to this provider, giving a one-argument `enabled`."""
        return FlagView(self, context)


class FlagView:
    """A context-bound view of a provider -- what a request handler receives.

    It holds the provider and one context and forwards every lookup, so a handler
    writes `flags.enabled("new_ui")` without rebuilding and re-passing the
    principal at each call site. The gain is correctness before cost: the bucketing
    context is fixed once, where the identity is known, so two checks in the same
    request cannot disagree because one of them was handed no subject and silently
    landed in the anonymous bucket.

    `__contains__` is the same method, so `"new_ui" in flags` reads as well.
    Nothing is cached: each lookup re-evaluates against the provider, so a value
    changed mid-request is honoured.

    Args:
        provider: the provider every lookup is forwarded to
        context: the bucketing context, fixed for this view's lifetime
    """

    __slots__ = ("_context", "_provider")

    def __init__(
        self,
        provider: TypedFlagProvider | FlagProvider,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self._provider = _flag_resolver(provider)
        self._context = None if context is None else dict(context)

    def enabled(self, name: str) -> bool:
        """Whether `name` is on for the bound context."""
        provider = self._provider
        if type(provider) is FeatureFlags:
            # The built-in provider already validates the string name and owns
            # the boolean fast path. Do not allocate and validate a temporary
            # typed declaration for every request-time boolean lookup.
            return provider.enabled(name, self._context)
        flag = Flag(name, False)
        return _resolved(flag, provider.resolve(flag, self._context))

    __contains__ = enabled


def flags_dependency(provider: TypedFlagProvider | FlagProvider):
    """Build a `Depends`-able yielding a request-scoped `FlagView`.

    The context is taken from the request identity's issuer-qualified id when
    present, so percentage rollouts bucket per authenticated principal. An
    unauthenticated request gets an empty context and therefore the shared anonymous
    bucket described in the module docstring -- a percentage flag is all-or-nothing
    before login.

    `provider` is captured here, once, rather than looked up per request:
    `Depends(flags_dependency(app.flags(...)))` binds the provider that
    `app.flags()` builds and registers on `app.state.flags`.
    """
    from .request import Request

    def _dependency(request: Request) -> FlagView:
        identity = getattr(request, "identity", None)
        context: dict[str, Any] = {}
        if identity is not None:
            subject = getattr(identity, "id", None)
            if subject is not None:
                namespace = getattr(identity, "namespace", "")
                context["id"] = qualified_identity_value(namespace, subject)
                if type(provider) is FeatureFlags:
                    context["_wreath_identity_key"] = qualified_identity_key(
                        str(getattr(identity, "type", "")),
                        str(namespace),
                        str(subject),
                    )
        return FlagView(provider, context)

    return _dependency
