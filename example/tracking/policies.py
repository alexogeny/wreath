"""Who is told where an animal is, and how precisely. Written once, in Cedar.

The camera-trap example asks a yes/no question about a station's coordinates and
answers it with a Cedar policy. This one asks the same question about an animal
and gets back a *resolution* -- exact, a kilometre, ten kilometres, or nothing
at all. That generalisation is this example's argument, and this file is where
it is made.

**The trick is that Cedar still answers yes or no.** There is no graded
decision, no numeric attribute, and nothing here scores a principal. There are
three separate actions -- one per grade -- and :func:`precision_for` asks about
each in turn, coarsest first, and takes the first permission it is given. A
policy set therefore stays a set of independent statements that an auditor can
read one at a time, and the ladder is in the *order the questions are asked*
rather than inside any rule.

The whole grid, which `tests/tracking/test_policy.py` holds as data:

| protection | ranger | partner | volunteer | public |
|---|---|---|---|---|
| `open` | exact | exact | exact | exact |
| `sensitive` | exact | 1 km | 10 km | absent |
| `restricted` | exact | absent | absent | absent |

**Why the public is a principal here and is not one in the camera trap.** That
example refuses an absent identity before it consults Cedar, and gives a good
reason: building a principal for somebody who is not there is the sort of
convenience that later grows a policy permitting it. Every route it serves is
behind `@authenticated()`, so an anonymous caller is a request that should not
have arrived.

This application is different in a way that matters. Its live map is
*deliberately* public for the animals nobody is hiding -- that is what a
conservancy publishes, and it is half the reason the collars are funded. So an
unauthenticated reader is a real principal with a real entitlement, not an
absence, and the rule about what they may see belongs in the policy set with
every other rule rather than in an `if` above it. The `Public` entity type
carries no roles and cannot be given one, so the only statements that can ever
reach it are the ones written with an unconstrained `principal`.
"""

from __future__ import annotations

from wreath.auth import Identity
from wreath.authorization import CedarEntity, CedarPolicies, EntityUid

from .models import PROTECTIONS
from .place import GRADES, Precision

#: The roles an identity may hold. Cedar sees them as `Role::"..."` parents of
#: the principal, which is what wreath's default identity mapper builds from
#: `Identity.roles` -- no extra wiring, and no second list.
#:
#: `ranger` and `volunteer` are the camera trap's roles, and mean the same
#: people: this is one conservancy with one staff list. `partner` is new here
#: because a collar programme is funded by institutions that get data and do not
#: get access to the reserve.
ROLES = ("ranger", "partner", "volunteer")

#: The principal for a caller who has not signed in. Its own entity *type*, so
#: no `principal in Role::"..."` statement can ever match it however the roles
#: are later edited.
PUBLIC = EntityUid("Public", "anonymous")

#: One action per grade in `tracking.place.GRADES`, derived rather than listed.
#: A fourth grade is then a constant in `place` and a statement here, and cannot
#: be added in one place and forgotten in the other.
LOCATE = {grade.name: f"Position::locate_{grade.name}" for grade in GRADES}


POLICY_SOURCE = """
// --- the animals nobody is hiding -------------------------------------------

// A collared zebra in a gait study, a wildebeest in the movement survey: the
// track is the published output of the programme, at the resolution it was
// recorded. `principal` is unconstrained, so this reaches the public too, and
// that is the intent -- a conservancy that cannot show anybody anything cannot
// explain why the collars are worth funding.
permit(principal, action == Action::"Position::locate_exact", resource)
  when { resource.protection == "open" };

// --- the people who respond -------------------------------------------------

// A ranger drives out to a snared animal, and a snare is found by walking to a
// coordinate. Degrading this would not protect anything; it would leave a
// wire round a leg for another day. No `when`: a ranger's need does not
// depend on the tier, which is the point of the role existing.
permit(principal in Role::"ranger", action == Action::"Position::locate_exact", resource);

// --- the people who analyse -------------------------------------------------

// A partner institution asks about range, habitat use and seasonal movement.
// Those are kilometre-scale questions and a kilometre-scale answer is a
// complete answer to them, so this is not a grudging compromise -- it is the
// resolution the science needs, and withholding more would be theatre.
permit(principal in Role::"partner", action == Action::"Position::locate_coarse", resource)
  when { resource.protection == "sensitive" };

// --- the people who watch ---------------------------------------------------

// A volunteer's dashboard says which end of the conservancy the herd is in and
// that everything is alive. Ten kilometres is the whole conservancy's short
// axis, so this says "north" or "south" and nothing that helps anybody drive
// to an animal.
permit(principal in Role::"volunteer", action == Action::"Position::locate_approximate", resource)
  when { resource.protection == "sensitive" };

// --- the two rhinos ---------------------------------------------------------
//
// There is no statement for `restricted` other than the ranger's. That absence
// is the rule: Cedar's default is deny, so a tier with no permit written for it
// is withheld from everyone the ranger statement does not cover, and it stays
// withheld when somebody later adds a grade without thinking about rhinos.
// Writing `forbid` for it instead would be weaker, not stronger -- it would
// imply the other tiers are permitted by something, which is exactly the
// reading that grows a permissive default.

// --- the standing refusal ---------------------------------------------------

// `forbid` overrides `permit` unconditionally in Cedar, which is what makes
// this one line worth more than it looks: no rule anybody adds later can make a
// suspended account readable again. A suspension a subsequent permit could
// defeat is not a suspension. A ranger who has lost their post loses the exact
// coordinates of two rhinos, and this is the line that takes them away.
forbid(principal, action, resource)
  when { principal has suspended && principal.suspended == true };
"""


#: Parsed at import. `CedarPolicies` raises on a syntax error here, so a
#: malformed rule is a process that will not start rather than a request that
#: mysteriously answers 403 at 3am.
ENGINE = CedarPolicies(POLICY_SOURCE)


def principal_entity(identity: Identity | None) -> CedarEntity:
    """The Cedar entity for a caller, signed in or not.

    An absent identity becomes `PUBLIC` rather than being refused before the
    engine -- see the module docstring for why this application differs from the
    camera trap there. It carries `suspended: false` explicitly so the standing
    `forbid` evaluates rather than erroring, and so nothing later can suspend a
    principal that has no account to suspend.
    """
    if identity is None:
        return CedarEntity(PUBLIC, attrs={"suspended": False})
    return CedarEntity(
        EntityUid(identity.type, identity.id),
        attrs={"suspended": bool(identity.claims.get("suspended", False))},
        parents=tuple(EntityUid("Role", role) for role in sorted(identity.roles)),
    )


def _decide(identity: Identity | None, action: str, protection: str) -> bool:
    """Ask the one engine one question about one protection tier.

    The resource is the *tier* rather than a particular animal, because that is
    genuinely what every statement above reads. Passing an animal entity would
    suggest the rules can distinguish two sensitive animals, which they cannot
    and are not meant to -- and it would put a per-animal identifier into a
    decision that is asked once per request rather than once per row.
    """
    principal = principal_entity(identity)
    resource = EntityUid("Animal", protection)
    decision = ENGINE.is_authorized(
        principal=principal.uid,
        action=EntityUid("Action", action),
        resource=resource,
        context={},
        entities=(principal, CedarEntity(resource, attrs={"protection": protection})),
    )
    return bool(getattr(decision, "allowed", decision))


def precision_for(identity: Identity | None, protection: str) -> Precision | None:
    """The finest grade this caller may have for an animal at this tier.

    Walks `tracking.place.GRADES` finest-first and returns the first grade Cedar
    permits, or `None` when it permits none -- and `None` means the position is
    **absent from the response**, not null and not zeroed.

    Deriving the ladder from the policy set, rather than from a table keyed on
    role, is the whole reason this function exists. A dict from role to metres
    would be a second copy of the rules that a policy edit leaves behind, and
    the copy that gets left behind is always the one enforcement uses.
    """
    for grade in GRADES:
        if _decide(identity, LOCATE[grade.name], protection):
            return grade
    return None


def precision_grid(identity: Identity | None) -> dict[str, Precision | None]:
    """This caller's grade for every tier at once.

    One call per rendered page rather than one per row: a list of two hundred
    fixes spans at most three tiers, and asking Cedar three times beats asking
    it two hundred. The answers cannot differ per row, because `_decide` is a
    function of the tier and nothing else -- which is a property worth stating,
    since a caching layer that was *not* entitled to assume it would be a bug.
    """
    return {tier: precision_for(identity, tier) for tier in PROTECTIONS}
