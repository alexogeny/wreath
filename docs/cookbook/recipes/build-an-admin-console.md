# Build an admin console

Wreath does not ship an admin UI, and that is deliberate. The popular ones in the
Python ecosystem are shaped around SQLAlchemy models, which Wreath does not have,
and an admin UI is a large, opinionated, permanently-maintained surface that
tends to fit nobody's product exactly.

What Wreath ships instead are the three pieces an admin console is made of, all
of which you already have: generated CRUD endpoints, a generated typed client,
and policy-based authorization. This recipe wires them together.

## 1. Generate the endpoints

`wreath.crud` builds the list/retrieve/create/update/delete routes for a model,
including the double-opt-in guard that stops a sensitive field being written by
accident.

```python
from wreath import Wreath
from wreath.crud import crud_router
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text

class Customer(Model, table="customers"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    plan: Mapped[str] = column(Text)

app = Wreath()
app.include_router(
    crud_router(Customer, prefix="/admin/customers", registry="main")
)
```

## 2. Gate every route with Cedar

An admin console is the highest-value target in your application, so authorize on
the *action*, not just on "is an admin". Cedar policies live in one place and
cover these routes exactly as they cover the rest of the API — see
[Authorize with Cedar](cedar-rbac.md).

```python
from wreath.authorization import requires_policy

app.include_router(
    crud_router(
        Customer,
        prefix="/admin/customers",
        registry="main",
        dependencies=(requires_policy("admin.customers.manage"),),
    )
)
```

Writes deserve a second gate. Reading a customer list and changing a customer's
plan are different actions with different blast radii; give them different
policies rather than one `admin` role.

## 3. Generate the client

```bash
uv run wreath typegen --target typescript --out ./admin/src/api.ts
```

That emits the request/response types **and** the fetch client from the same
route definitions, so the console cannot drift from the API. Add the React Query
target and the console gets hooks with the same types:

```bash
uv run wreath typegen --target react-query --out ./admin/src/hooks.ts
```

## 4. Build the console against those hooks

Any front end works, because the generated client is plain TypeScript. The point
is that the console has no hand-written knowledge of your schema — regenerate
after a migration and the compiler tells you what broke.

```tsx
import { useListCustomers, useUpdateCustomer } from "./hooks";

export function Customers() {
  const { data, isLoading } = useListCustomers({ page: 1, size: 50 });
  const update = useUpdateCustomer();
  if (isLoading) return <p>Loading…</p>;
  return (
    <table>
      <tbody>
        {data.items.map((customer) => (
          <tr key={customer.id}>
            <td>{customer.email}</td>
            <td>
              <select
                value={customer.plan}
                onChange={(event) =>
                  update.mutate({ id: customer.id, plan: event.target.value })
                }
              >
                <option value="free">free</option>
                <option value="pro">pro</option>
              </select>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

## 5. Make the console auditable

An admin console changes production data, so the interesting question is always
"who changed this, and when". Two things worth turning on:

- **Server-side sessions** ([`wreath.session_store`](../../reference/session_store.md)),
  so an admin session can be revoked immediately rather than waiting for a cookie
  to expire. Call `rotate_session(request)` when a user enters the admin area.
- **The Flight Recorder**, scoped to the admin prefix. Admin traffic is low
  volume and high value, which is the ideal shape for detailed capture — you can
  afford to record all of it.

## What you are giving up

A generated console is not a product. There is no schema-driven form widget
library, no inline relation editor, no saved views. If you need those, build them
in the front end where they belong: the API side is already complete, and nothing
here stops you from replacing step 4 with a full application.
