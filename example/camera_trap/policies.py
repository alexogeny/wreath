"""Who may see what, written once, in Cedar.

Publishing a rhino's location assists poachers. That sentence is the reason
conservation databases have access control at all, and it is why this file
exists rather than a scattering of `if observer.role == "ranger"` checks: the
rule is a policy question, it changes without the code changing, and an auditor
has to be able to read it.

Three protection tiers on `Species`, three roles on `Observer`, and one grid
between them:

| Tier | volunteer | researcher | ranger |
|---|---|---|---|
| `open` | yes | yes | yes |
| `sensitive` | no | yes | yes |
| `restricted` | no | no | yes |

Station coordinates follow the same shape for a different reason: the tier is on
the *place* rather than on the animal, because a nest is worth protecting
whether or not anything was photographed at it this week.

**The policy set is parsed once, at import.** `CedarPolicies` raises on a syntax
error there, so a malformed rule is a process that will not start rather than a
request that mysteriously 403s at 3am.

**One engine, two callers.** The same `ENGINE` answers route-level `@authorize`
decorators through `CedarAuthorizer`, and per-row questions from handlers
through `may_see()`. That matters more than it looks: a second implementation of
"can this observer see a restricted sighting", written in Python next to the
query that needed it, is how an authorization rule and its enforcement drift
apart. There is one rule and one evaluator; the handler asks it a question
rather than reimplementing it.
"""

from __future__ import annotations

from collections.abc import Iterable

from wreath.auth import Identity
from wreath.authorization import CedarEntity, CedarPolicies, EntityUid

#: The three roles an `Observer.role` may hold. Cedar sees them as
#: `Role::"..."` parents of the principal, which is what the default identity
#: mapper builds from `Identity.roles` -- no extra wiring, and no second list.
ROLES = ("volunteer", "researcher", "ranger")

#: The three tiers a `Species.protection` may hold, weakest first.
PROTECTIONS = ("open", "sensitive", "restricted")

#: Read a sighting, which means: see that this animal was here, at this time.
READ_SIGHTING = "Sighting::read"

#: Read a station's precise coordinates. Deliberately separate from reading the
#: station: a volunteer may know that station "Kopje North" exists and how much
#: it recorded, without being told where to walk to find it.
LOCATE_STATION = "Station::locate"

#: Administer the controlled vocabulary and the station register. Coarse by
#: design -- there is no per-row question to ask about a species record.
ADMINISTER = "Registry::administer"


POLICY_SOURCE = """
// --- sightings, by the protection tier of the species -----------------------

// Anyone signed in may see an open sighting. Most of the 40 species are open,
// and the public value of the data set is the reason the network exists.
permit(principal, action == Action::"Sighting::read", resource)
  when { resource.protection == "open" };

// Sensitive species -- pangolin, ground hornbill, the cats -- are withheld from
// volunteers. Researchers hold a permit; rangers are the people who respond.
permit(principal in Role::"researcher", action == Action::"Sighting::read", resource)
  when { resource.protection == "sensitive" };

permit(principal in Role::"ranger", action == Action::"Sighting::read", resource)
  when { resource.protection == "sensitive" };

// Restricted species -- rhino, and the two nesting raptors -- are rangers only.
permit(principal in Role::"ranger", action == Action::"Sighting::read", resource)
  when { resource.protection == "restricted" };

// --- station coordinates ----------------------------------------------------

// An ordinary station's location is not a secret. A waterhole is on the map.
permit(principal, action == Action::"Station::locate", resource)
  when { resource.sensitive == false };

// A sensitive station -- a midden, a nest tree -- is a ranger's to know.
permit(principal in Role::"ranger", action == Action::"Station::locate", resource)
  when { resource.sensitive == true };

// --- the registry -----------------------------------------------------------

// Editing the vocabulary and the station register is an ecologist's job, and in
// this network the ecologists are the researchers and the rangers.
permit(principal in Role::"researcher", action == Action::"Registry::administer", resource);
permit(principal in Role::"ranger", action == Action::"Registry::administer", resource);

// --- the standing refusal ---------------------------------------------------

// Forbid overrides permit unconditionally in Cedar, which is what makes this
// one line worth more than it looks: no rule added later, by anyone, can make a
// suspended account readable again. A rule that can be defeated by a subsequent
// permit is not a suspension.
forbid(principal, action, resource)
  when { principal has suspended && principal.suspended == true };
"""


#: Parsed at import. A syntax error here is a start-up failure, which is the
#: only time an authorization bug is cheap.
ENGINE = CedarPolicies(POLICY_SOURCE)


def principal_entity(identity: Identity) -> CedarEntity:
    """The Cedar entity for a signed-in observer.

    Carries `suspended` because the standing `forbid` reads it, and Cedar's
    `has` test is what keeps an identity that predates the attribute from
    erroring rather than evaluating.
    """
    return CedarEntity(
        EntityUid(identity.type, identity.id),
        attrs={"suspended": bool(identity.claims.get("suspended", False))},
        parents=tuple(EntityUid("Role", role) for role in sorted(identity.roles)),
    )


def _decide(
    identity: Identity | None,
    action: str,
    resource: EntityUid,
    attrs: dict[str, object],
) -> bool:
    """Ask the one engine one question.

    An absent identity is refused before the engine is consulted rather than
    passed to it as an anonymous principal: Cedar's default is deny, so both
    answers agree, but building a principal for someone who is not there is the
    kind of convenience that later grows a policy permitting it.
    """
    if identity is None:
        return False
    principal = principal_entity(identity)
    decision = ENGINE.is_authorized(
        principal=principal.uid,
        action=EntityUid("Action", action),
        resource=resource,
        context={},
        entities=(principal, CedarEntity(resource, attrs=attrs)),
    )
    return bool(getattr(decision, "allowed", decision))


def may_see_protection(identity: Identity | None, protection: str) -> bool:
    """May this observer see a sighting of a species at this protection tier?"""
    return _decide(
        identity,
        READ_SIGHTING,
        EntityUid("Species", protection),
        {"protection": protection},
    )


def may_locate(identity: Identity | None, *, sensitive: bool) -> bool:
    """May this observer be told where this station actually is?"""
    return _decide(
        identity,
        LOCATE_STATION,
        EntityUid("Station", "sensitive" if sensitive else "ordinary"),
        {"sensitive": sensitive},
    )


def visible_protections(identity: Identity | None) -> tuple[str, ...]:
    """The tiers this observer may see, as a filter for a query.

    Derived by asking the engine once per tier rather than by reading the role,
    so a policy change moves this function with it. Three evaluations of a
    parsed policy set is not a cost worth caching -- and a cache here would be
    one keyed on a role, which is the assumption the whole file avoids.
    """
    return tuple(tier for tier in PROTECTIONS if may_see_protection(identity, tier))


def redact_unreadable(identity: Identity | None, protections: Iterable[str]) -> set[str]:
    """The tiers this observer may *not* see. The complement of the above.

    Named rather than inlined because "which of these should I hide" reads
    backwards as `not in visible_protections(...)` at three call sites, and a
    negation repeated three times is a negation someone eventually gets wrong.
    """
    allowed = set(visible_protections(identity))
    return {tier for tier in protections if tier not in allowed}
