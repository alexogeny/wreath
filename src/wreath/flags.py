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
and there is no fallback chain behind it. The provider is a `Protocol` --
env/mapping now, external providers (Unleash, LaunchDarkly, ...) are future
adapters implementing the same surface.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from .config import read_osenv

FLAG_PREFIX = "WREATH_FLAG_"

_TRUE = frozenset({"on", "true", "1", "yes", "enabled"})
_FALSE = frozenset({"off", "false", "0", "no", "disabled", ""})

__all__ = ["FLAG_PREFIX", "FeatureFlags", "FlagProvider", "FlagView"]


@runtime_checkable
class FlagProvider(Protocol):
    """Anything that can answer whether a flag is on for a context.

    `runtime_checkable`, so `isinstance(obj, FlagProvider)` holds for any object
    carrying an `enabled` attribute -- a structural check on the name alone, not
    on the signature. An implementation answers `False` for a flag it does not
    know rather than raising; that fail-closed default is what lets a caller treat
    "the provider had no answer" and "the flag is off" as the same thing.
    """

    def enabled(self, name: str, context: Mapping[str, Any] | None = None) -> bool:
        """Whether `name` is on for `context`, which also supplies the bucket."""
        ...


def _subject(context: Mapping[str, Any] | None) -> str:
    if not context:
        return ""
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
        return _bucket(name, _subject(context)) < percent
    return False


class FeatureFlags:
    """A mapping-backed `FlagProvider` (env-sourced or explicit).

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
            key[len(FLAG_PREFIX) :]: val
            for key, val in env.items()
            if key.startswith(FLAG_PREFIX)
        }
        return cls(values)

    def enabled(self, name: str, context: Mapping[str, Any] | None = None) -> bool:
        """Whether `name` is on for `context`; a flag not held here is off.

        `context` is read only to find the rollout subject, and only when the
        value is a percentage, so an on/off flag costs a dict lookup and a set
        membership test.
        """
        raw = self._values.get(name.lower())
        if raw is None:
            return False
        return evaluate_rule(raw, name.lower(), context)

    def all(self, context: Mapping[str, Any] | None = None) -> dict[str, bool]:
        """Resolve every configured flag for `context`, keyed by lower-cased name.

        Only flags this provider holds appear. A flag the application asks about
        but nobody configured is absent here and off at `enabled`, so this is a
        view of the configuration rather than of the application's flag vocabulary.
        """
        return {name: evaluate_rule(raw, name, context) for name, raw in self._values.items()}

    def names(self) -> frozenset[str]:
        """Every flag name this provider holds, lower-cased.

        The enumeration half of the provider surface, and deliberately *not* on
        the `FlagProvider` protocol: an external provider (Unleash,
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

    def __init__(self, provider: FlagProvider, context: Mapping[str, Any] | None = None) -> None:
        self._provider = provider
        self._context = context

    def enabled(self, name: str) -> bool:
        """Whether `name` is on for the bound context."""
        return self._provider.enabled(name, self._context)

    __contains__ = enabled


def flags_dependency(provider: FlagProvider):
    """Build a `Depends`-able yielding a request-scoped `FlagView`.

    The context is taken from `request.identity.id` when present, so percentage
    rollouts bucket per authenticated principal. An unauthenticated request gets an
    empty context and therefore the shared anonymous bucket described in the module
    docstring -- a percentage flag is all-or-nothing before login.

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
                context["id"] = subject
        return FlagView(provider, context)

    return _dependency
