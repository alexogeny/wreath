"""One set-valued Cedar context key, declared once and resolved once per request.

`context.flags` and `context.regions` were written twice — two request-state
slots, two resolvers, two vocabulary probes, two startup validations, two
manifest accessors. They answer an identical question in an identical shape,
and four more facts were queued behind them (organisation membership, roles
within an organisation, entitlements, and whichever fact a verified-signature
check eventually contributes). Six copies of a security-critical caching rule
is how the copies drift apart, and the direction they drift in is *permit*.

So this is the one implementation, parameterised by the attribute. A `SetFact`
carries the four properties every one of them needs and every one of them can
get subtly wrong:

* **Resolved once per request**, cached on `request.state`. A route behind
  several policies asks the authorizer once per policy; a fact re-resolved per
  policy could answer differently inside one decision, and a `permit` and a
  `forbid` disagreeing about the same caller is not a decision anybody wrote.
* **Resolved only when a policy reads it.** The vocabulary walk is the laziness
  mechanism, not merely an optimisation: an empty vocabulary means no policy
  names the key, so nothing is resolved at all. For `flags` that saves a hash;
  for `organizations` it saves a database round trip on the authorization path,
  which is the difference between the fact being free and being the most
  expensive thing in the request.
* **Fail-closed.** The empty set is *always supplied*, even with no provider —
  see `always_supplied` below for why absence is not a neutral default.
* **Refused at startup** when a policy names something the provider does not
  hold, so a typo fails where it is written rather than denying forever with
  nothing to see.

The engine side was already generic: `cedar_engine._referenced_members` takes
the attribute name. This is the adapter side catching up.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any, cast

#: The answer for a fact nobody provides, and for one no policy reads. Shared
#: rather than rebuilt, and *always supplied* -- an absent set key is not a
#: neutral default. Measured against the engine:
#: `forbid(...) unless { context.X.contains("bypass") }` against a context with
#: no `X` at all evaluates to **allowed** -- the forbid is skipped rather than
#: standing -- so an application that never configured a provider, or a custom
#: context mapper that dropped the key, would silently stop forbidding. An
#: empty set denies in both the `when` and the `unless` shape, which is the only
#: fail-closed answer.
EMPTY: frozenset[str] = frozenset()


def referenced_names(engine: Any, capability: str) -> frozenset[str] | None:
    """The names `engine`'s policies test for `capability`, or None for "all".

    Optional capability, probed the way `fingerprint`/`source`/`policies` are:
    the `CedarEngine` protocol does not require it. An engine that cannot
    introspect its own policy set answers `None` here, which costs the eager
    resolution rather than risking a short list -- an outside evaluator whose
    policies this walk has never seen must not have its vocabulary guessed at.
    """
    referenced = getattr(engine, capability, None)
    if not callable(referenced):
        return None
    names = referenced()
    return None if names is None else frozenset(names)


def validate_names(
    referenced: frozenset[str] | None,
    provider: Any,
    *,
    noun: str = "feature flags",
    singular: str = "flag",
    attribute: str = "flags",
) -> None:
    """Refuse, at startup, a policy naming something the provider does not hold.

    A typo inside `context.flags.contains("new_iu")` is the likeliest mistake
    here and the least visible one: the name is simply absent from the set, the
    condition is false, and the policy denies forever with nothing to see. This
    turns that into a boot failure naming the flag. Every other declared
    vocabulary -- regions, roles within an organisation, entitlements -- fails
    in exactly the same silent way and gets the same treatment from this code.

    Two branches, because only some providers can enumerate. One that offers
    `names()` is checked. One that cannot -- an external service that would need
    a network call to list its vocabulary -- gets a warning at the point the
    authorizer is built instead, which is the same shape `second_factor_router`
    uses for its own half-knowable condition: say what is unverifiable where it
    is written, rather than either failing on a guess or staying silent.

    A configured-but-empty provider is still a provider, so `names()` returning
    nothing means every referenced name is unknown and the raise is correct.
    Passing no provider at all skips validation entirely: that application has
    decided the capability is off, every set is empty, and every test denies.
    """
    if referenced is not None and not referenced:
        return  # no policy reads one; nothing to check and nothing to say
    if provider is None:
        return  # deliberately off: every test denies, and that is written down
    enumerate_names = getattr(provider, "names", None)
    if referenced is None:
        # The policy set reads them in a shape whose names cannot be listed, so
        # the provider has to supply all of them -- and this one cannot be
        # enumerated either. Nothing can ever be resolved, so every test
        # denies forever: working, fail-closed, and completely silent, which is
        # the shape worth a warning rather than a shrug.
        if not callable(enumerate_names):
            warnings.warn(
                f"cedar policies read context.{attribute} in a shape whose names "
                "cannot be read off the source (isEmpty(), or a computed "
                f"argument), and the {singular} provider {type(provider).__name__} "
                f"cannot enumerate its names either, so no {singular} can be "
                f"resolved and every {singular} test will deny",
                RuntimeWarning,
                stacklevel=3,
            )
        return  # no name list to validate against either way
    if not callable(enumerate_names):
        warnings.warn(
            f"cedar policies reference {noun} "
            f"({', '.join(sorted(referenced))}) but the {singular} provider "
            f"{type(provider).__name__} cannot enumerate its names, so they "
            f"cannot be checked; a misspelled {singular} will deny silently",
            RuntimeWarning,
            stacklevel=3,
        )
        return
    known = {str(name).lower() for name in enumerate_names()}
    unknown = sorted(name for name in referenced if name.lower() not in known)
    if unknown:
        raise ValueError(
            f"cedar policies reference {noun} the provider does not "
            f"hold: {', '.join(unknown)}. A {singular} absent from the provider "
            f"is absent from context.{attribute}, so the policy would deny forever."
        )


class SetFact:
    """One declared set-valued context key, bound to an engine and a provider.

    Built once per `CedarAuthorizer`, at construction, so the vocabulary walk
    and the startup validation happen at boot rather than per request.

    Args:
        attribute: the Cedar context key, e.g. `"flags"`. The engine capability
            probed for its vocabulary is `referenced_<attribute>`, and the
            request-state cache slot is derived from it, so the three cannot
            drift apart by being spelled separately.
        engine: the `CedarEngine`, probed for the vocabulary walk
        provider: whatever answers the fact, or `None` for "this application
            switched the capability off". `None` never resolves and never
            validates: every test against the key denies, deliberately.
        resolve: `(request, vocabulary) -> frozenset[str]`. Called at most once
            per request, and **not at all** when no policy names the key.
            `vocabulary` is `None` when the names are not statically knowable
            and the resolver must answer for everything.
        noun: plural, for the startup message ("feature flags")
        singular: for the same ("flag")
        validate: whether the referenced names are a *declared vocabulary* that
            can be checked at startup. False for a key whose members are
            application data rather than configuration -- an organisation id in
            a policy is a row, and refusing to boot because a row does not exist
            yet would be wrong.
    """

    __slots__ = ("_provider", "_resolve", "_slot", "attribute", "vocabulary")

    def __init__(
        self,
        attribute: str,
        *,
        engine: Any,
        provider: Any,
        resolve: Callable[[Any, frozenset[str] | None], frozenset[str]],
        noun: str,
        singular: str,
        validate: bool = True,
    ) -> None:
        self.attribute = attribute
        self._slot = f"_cedar_fact_{attribute}"
        self._provider = provider
        self._resolve = resolve
        self.vocabulary = referenced_names(engine, f"referenced_{attribute}")
        if validate:
            validate_names(
                self.vocabulary,
                provider,
                noun=noun,
                singular=singular,
                attribute=attribute,
            )

    def for_request(self, request: Any) -> frozenset[str]:
        """This request's members, resolved at most once and cached on the request.

        Three short-circuits before any work happens, in the order that makes
        the common request free: no provider (the capability is off), no policy
        naming the key (nothing to resolve), and an answer already cached from
        an earlier policy in this same decision.
        """
        if self._provider is None:
            return EMPTY
        vocabulary = self.vocabulary
        if vocabulary is not None and not vocabulary:
            # No policy names this key, so no answer can change a decision.
            # Returning here is what keeps a store-backed fact off the request
            # path entirely for the applications that do not use it.
            return EMPTY
        state = request.state
        cached = state.get(self._slot)
        if cached is not None:
            return cast(frozenset[str], cached)
        resolved = self._resolve(request, vocabulary)
        state.__setattr__(self._slot, resolved)
        return resolved
