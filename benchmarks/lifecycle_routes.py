"""The lifecycle benchmark's route table, shared by the apps and the runner.

Kept free of import side effects so both ``lifecycle_apps`` (which builds an
application on import) and ``lifecycle`` (which only needs the target URL) can
import it.

The literals here are words on purpose. The table used to be built from
numbered literals -- ``domain-7``, ``group-2``, ``resource-13`` -- which is not
what a real route table looks like: real path segments are nouns, and the
numbers in a real API are path *parameters*, which this table already has. The
distinction is not cosmetic. A router that keys on segment bytes behaves very
differently on ``resource-137`` (a long shared prefix, discriminated only by
digits deep in the segment) than on ``invoices``, and measuring the numbered
shape reports a number that no real application would see.

Shape, count, depth, and per-branch permissions are unchanged from the numbered
version, so this is the same benchmark with a realistic vocabulary -- but
results are not comparable across the change, because the route table is an
input to what it measures.
"""

from __future__ import annotations

ROUTE_BRANCHES = 24
ROUTES_PER_BRANCH = 16
TARGET_BRANCH = ROUTE_BRANCHES - 1
API_PREFIX_TEMPLATE = "/api/v2/organizations/{organization_id}"

#: One noun per domain branch. 24 of them, the sibling subtrees a
#: permission-aware router gets to prune.
BRANCH_WORDS = (
    "billing",
    "identity",
    "catalog",
    "shipping",
    "payments",
    "accounts",
    "inventory",
    "orders",
    "shipments",
    "returns",
    "invoices",
    "contracts",
    "documents",
    "messaging",
    "analytics",
    "reporting",
    "search",
    "media",
    "webhooks",
    "audit",
    "compliance",
    "support",
    "workflows",
    "admin-console",
)
#: One noun per group, 4 of them: the repeated mid-path literal.
GROUP_WORDS = ("public", "internal", "partner", "restricted")
#: One noun per leaf, 16 of them: the distinct per-leaf literal.
RESOURCE_WORDS = (
    "invoices",
    "members",
    "documents",
    "webhooks",
    "sessions",
    "tokens",
    "exports",
    "reports",
    "plans",
    "teams",
    "events",
    "audits",
    "policies",
    "secrets",
    "regions",
    "zones",
)

assert len(BRANCH_WORDS) == ROUTE_BRANCHES
assert len(RESOURCE_WORDS) == ROUTES_PER_BRANCH
assert len(GROUP_WORDS) == 4
assert len(set(BRANCH_WORDS)) == len(BRANCH_WORDS)
assert len(set(RESOURCE_WORDS)) == len(RESOURCE_WORDS)

TARGET_BRANCH_WORD = BRANCH_WORDS[TARGET_BRANCH]


def leaf_suffix(leaf: int, item_param: str) -> str:
    """The decoy leaf path below a branch, with `item_param` already formatted."""
    return f"/services/{GROUP_WORDS[leaf % len(GROUP_WORDS)]}/{RESOURCE_WORDS[leaf]}/{item_param}"


def request_path(organization_id: int, user_id: int) -> str:
    """The one URL the benchmark measures."""
    return f"/api/v2/organizations/{organization_id}/{TARGET_BRANCH_WORD}/admin/users/{user_id}"
