"""SCIM 2.0 provisioning -- RFC 7643 and RFC 7644 -- as an adapter, not a store.

`wreath.organizations.scim_router` is the public name for everything here. The
package is split by what each part can be reasoned about on its own:

* `filters` -- the filter grammar of RFC 7644 section 3.4.2.2, parsed and
  evaluated against a SCIM representation.
* `patch` -- `PATCH` and `PUT` as pure functions producing the next
  representation, with the operation semantics of section 3.5.
* `resources` -- the mapping between SCIM's `User`/`Group` and wreath's user
  store and `wreath.organizations`, plus the discovery documents.
* `router` -- the endpoints, which authenticate and authorize through the
  application's own backend and Cedar authorizer and decide nothing themselves.

**SCIM owns no data.** Every user is a `wreath.users` record, every group is a
role in an `OrganizationStore`'s declared vocabulary, and every membership is a
`wreath.organizations.Membership`. Where the two models disagree, the
disagreement is documented in `resources` and surfaced to the client as a
refusal -- never resolved by keeping a second copy.
"""

from __future__ import annotations

from .router import ScimResponse, scim_router

__all__ = ["ScimResponse", "scim_router"]
