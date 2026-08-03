"""A generated administration surface, built entirely out of what already ships.

Django's admin auto-generates list, detail and edit screens from models, and it
is a principal reason teams stay on Django. There is no equivalent in async
Python, and the gap is filled by per-seat vendors that connect to your database,
re-derive your relationships and permissions by hand, and drift on the next
migration.

Wreath's answer adds **no new primitive**. Every input already existed:

| It needs | It uses |
| --- | --- |
| The model, its columns, its types | the ORM declaration set |
| Which columns are secrets | `wreath.crud.sensitive_fields` |
| Which columns are retrieval indexes | `wreath.crud.retrieval_fields` |
| Per-operation authorization | `wreath.crud.Access` -- the same five operations |
| Per-**field** authorization | Cedar, asked the way `PrecisionLadder` asks |
| Paging, sorting, the column allow-list | `wreath.pagination` |
| HTML that escapes by default | `wreath.templates` |
| Every write attributed | `wreath.audit_log.actor` |
| WCAG 2.1 A/AA conformance | `wreath.audit`, in the test suite |

```python
from wreath.admin import Admin, FieldAccess
from wreath.crud import Access

admin = Admin(
    open_session,
    authorize={
        "read": Access.roles("staff"),
        "write": Access.roles("staff").within(300),   # step up before writing
    },
    csrf=verify_admin_form,
)
admin.register(Photo, list_columns=("taken_at", "species"))
admin.register(User, field_access={"email": FieldAccess(read="read_contact")})
app.include_router(admin.router("/admin"))
```

**It is not a customer-facing surface.** It concentrates read access to every
registered model into one authenticated place, which is exactly what makes it
useful to an operator and exactly why it must never be the UI a customer is
given. There is no theming beyond the basics, no custom pages, and no workflow
builder, and those absences are the feature: every generated admin that grew
them became the thing nobody could reason about.

Three properties worth knowing before mounting it:

* **The admin is a client of the ordinary stack, not a bypass of it.** Writes go
  through the same ORM session, the same validation and the same `_orm_events`
  as a hand-written route, so the audit trail and the response cache see them.
* **Step-up, field policy and audit are preconditions, not enhancements.**
  `Access.within(...)` composes onto any rule; `FieldAccess` decides per column;
  a write to an audited model with no actor bound raises rather than recording
  an anonymous change.
* **No JavaScript, so `script-src 'none'`.** The admin ships no script at all,
  which is what lets its Content-Security-Policy be the strongest one available
  rather than the permissive policy an inline-script page would need.
"""

from __future__ import annotations

from ._admin.fields import FieldAccess
from ._admin.pages import CONTENT_SECURITY_POLICY
from ._admin.registry import WITHHELD_MARKER, Admin, AdminError, ModelAdmin

__all__ = [
    "CONTENT_SECURITY_POLICY",
    "WITHHELD_MARKER",
    "Admin",
    "AdminError",
    "FieldAccess",
    "ModelAdmin",
]
