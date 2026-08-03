# A read-only admin for support staff

The safest useful admin, and the one worth starting from: your support team can
find and read rows, and nobody can change anything through it. It needs no CSRF
verifier, because it generates no forms.

```python
from wreath.admin import Admin, FieldAccess
from wreath.crud import Access

admin = Admin(
    open_session,
    authorize=Access.roles("support"),
    title="Support console",
)

admin.register(
    Account,
    list_columns=("id", "name", "email", "plan", "created_at"),
    field_access={"email": FieldAccess(read="read_contact")},
    operations=("list", "retrieve"),
)
admin.register(Order, list_columns=("id", "account_id", "total", "placed_at"),
               operations=("list", "retrieve"))

app.include_router(admin.router("/support"))
```

Three things this gets you that are worth naming.

**Nobody can write.** `operations=("list", "retrieve")` generates no `POST`
route at all, so there is no form to protect and no path a mistake can take. This
is a stronger statement than a policy that denies writes, because the route does
not exist.

**The email address is a policy outcome.** A support agent whose principal
permits `read_contact` sees it; a contractor on the same screen sees the withheld
marker, and the value never reaches the page. Add the permission to the policy
set and the screen changes for that person with no code change here.

**`plan` and `created_at` are shown but never settable**, because nothing is
settable. When you later add `"update"` to `Account`'s operations, mark the
server-set columns explicitly:

```python
admin.register(
    Account,
    readonly=("created_at", "plan"),
    operations=("list", "retrieve", "update"),
)
```

## Growing it into a writable one

You need two things: a CSRF verifier, because
`wreath.middleware.CSRFMiddleware` is header-only and an HTML form cannot carry a
header, and a step-up window on the write operations.

```python
admin = Admin(
    open_session,
    authorize={
        "read":  Access.roles("support"),
        "write": Access.roles("support", "editor", mode="all").within(300),
    },
    csrf=verify_admin_form,
)
```

Every write from that point is attributed into the audit trail with the operator's
identity, inside the transaction that made the change — you do not have to do
anything to get that, and you cannot turn it off for an audited model.

See the [admin guide](../../guides/admin.md) for the full picture, and
[`wreath.admin`](../../reference/admin.md) for the API.
