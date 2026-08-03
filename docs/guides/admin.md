# The admin

Every application eventually grows the same five screens. Somebody needs to find
a customer by email, look at what the nightly job did to their account, correct a
typo somebody made in support, and — carefully, with the right person's name
against it — delete a row. Writing those screens is not interesting work, and
not writing them is worse: the alternative is a database console, which has no
permissions, no validation and no memory of who did what.

Django's answer to this is its admin, and it is a principal reason teams stay on
Django. Async Python has had no equivalent, so the gap gets filled by per-seat
vendors that connect to your database, re-derive your relationships and
permissions by hand, and drift the next time you run a migration.

Wreath's answer is different in one specific way, and the difference is the whole
argument for it: **it is not a tool that connects to your application, it is your
application drawing itself.** The models are already declared. The policy set
already decides who may do what. The audit trail already records who changed
which field, inside the transaction that changed it. The admin is those things
wearing a user interface, so there is nothing to keep in sync and nothing to
drift.

## Mounting it

```python
from wreath.admin import Admin, FieldAccess
from wreath.crud import Access

admin = Admin(
    open_session,
    authorize={
        "read":  Access.roles("staff"),
        "write": Access.roles("staff").within(300),
    },
    csrf=verify_admin_form,
)
admin.register(Photo, list_columns=("taken_at", "species", "owner_id"))
admin.register(Account, field_access={"email": FieldAccess(read="read_contact")})

app.include_router(admin.router("/admin"))
```

That is the whole integration. `authorize` takes the same `Access` vocabulary
generated CRUD takes, over the same five operations, so a rule you have already
written for `crud_router` reads correctly here.

**You opt in three times**, and the repetition is deliberate. An admin
concentrates read access to every registered model into one authenticated
surface, which is exactly what makes it useful and exactly why it must never
appear by accident. So: an explicit `authorize` rule, with no default and
`Access.public()` refused outright; an explicit `register` for each model,
because nothing is exposed by existing; and an explicit `include_router`.

## What it will not show you

The admin inherits `wreath.crud`'s judgement about what may leave the server
rather than making a second one. A column whose name looks like a secret —
`password_hash`, `api_token`, `session_key` — is absent from every list, every
detail page and every form, and no admin route will ever set one. A retrieval
column, a `Vector` embedding or a `TsVector`, is absent for a different reason:
it is how rows are *found* rather than what they say, and a page of it is
thousands of floats nobody can read.

Both are opt-in-able with `expose=(...)`, which makes showing one a deliberate,
reviewable act rather than a default. Exposing a sensitive column makes it
*readable*; nothing makes it writable through a generated form. Change a password
through a purpose-built endpoint.

## Per-field authorization

This is the part a third-party admin cannot do, because it does not hold your
policy set.

Cedar decides per action. An admin needs per **field**: a support agent sees the
customer's email address, a contractor working on the same screen sees it
redacted. `FieldAccess` says which Cedar action permits reading or writing one
column:

```python
admin.register(Account, field_access={
    "email":       FieldAccess(read="read_contact"),
    "credit_limit": FieldAccess(read="read_billing", write="change_billing"),
})
```

No new decision point is invented for this. `read_contact` is an ordinary action
in the same policy set as everything else, asked the same way
`PrecisionLadder` asks its rungs: once per request, cached for the length of it,
and fail-closed — a policy set that says nothing about `read_contact` withholds
the column, and a deployment that declared a field rule but configured no
authorizer withholds it too, because publishing would be answering a question
nobody can make.

Two properties are worth stating plainly, because they are what make this a
control rather than a decoration:

- **A withheld value never reaches the template.** The filter is applied where
  the row is read, not where it is drawn, so there is no second projection to
  forget and no template edit that could publish it. What the page shows is a
  constant marker — the same one `wreath.audit_log` uses — because *"this field
  exists and you may not see it"* and *"this field does not exist"* are different
  facts and collapsing them loses the one the reader wants.
- **Unreadable implies unwritable.** A field its author cannot see is a field
  they cannot knowingly change, so it gets no form control, and a value submitted
  for it anyway is dropped rather than written.

## Forms, and the one conversion in the middle

A form transports strings. The ORM stores typed values and deliberately does not
parse: assigning `'4'` to a `bigint` column raises, because a driver that guessed
at strings would eventually guess wrong somewhere it mattered. So the admin
converts each submitted value against the column's declared type before it
reaches the instance, using the same converter `wreath.binding` uses for a query
parameter — a form field is the same problem arriving through a different door.

`numeric` is the one type that does not go through it, and lands on `Decimal`
instead: a float cannot hold a numeric exactly, and the ORM says so rather than
rounding. Text, `varchar`, `uuid`, `bytea` and the JSON types keep their string,
which is what they accept anyway.

When a value will not convert, the form comes back with the message naming the
field — and **repopulated from what the operator typed**, not from the stored
row. Somebody who mistyped one field does not lose the other four.

## Every write is attributed

`wreath.audit_log` records inside the transaction that made the change, driven by
the ORM rather than by anyone remembering to call it. What the admin owes it is an
actor, and it binds one around every write from the authenticated identity.

There is no fallback. An admin write that somehow arrives with no identity raises
rather than recording an anonymous change — a record complete except for the one
field that makes it evidence is worse than no record, because it looks like one.

## Step up before writing

`Access.within(seconds)` composes onto any rule, so demanding a fresh second
factor before writes is the mapping in the example above and nothing else:

```python
authorize={"read": Access.roles("staff"),
           "write": Access.roles("staff").within(300)}
```

The read screens stay usable all day; the destructive ones ask again.

## Cross-site request forgery, and a gap you must close

`wreath.middleware.CSRFMiddleware` reads its token from a request *header*. A
plain HTML form post cannot carry one, so that middleware cannot protect these
routes — mounting it would refuse every admin form rather than defend it.

Rather than ship an unprotected escalation path or grow a second CSRF
implementation, the admin **requires you to name the check**: `csrf=` takes a
`(request) -> bool` (optionally async) and no write operation is generated
without it. A `False` answer is a 403 and nothing is read or written.

If you want a read-only admin — a genuinely useful thing, and the safest way to
start — register the read operations and the requirement goes away:

```python
admin.register(Account, operations=("list", "retrieve"))
```

Form-field CSRF support in `CSRFMiddleware` is on
[the roadmap](../reference/roadmap.md); when it lands, `csrf=` becomes a
one-liner pointing at it.

## No JavaScript, and therefore a real CSP

The admin ships no script at all. That is a decision rather than an omission: a
page with no inline script needs no CSP nonce, so the admin can send
`default-src 'none'; script-src` absent entirely — the strongest policy available
— instead of the permissive one an inline-script page would need. It also means
no bundler, no `npm`, and no build step in a framework whose core takes no
runtime dependencies.

The pages are held to WCAG 2.1 A/AA by wreath's own accessibility auditor, in the
test suite rather than as a manual step. Shipping a UI that failed the gate this
project sells would be an embarrassment with a lint rule attached.

## What it deliberately is not

Django's own admin is widely advised against as a customer-facing UI, and the
advice is right. This one refuses the features that make the temptation grow:

- **No theming** beyond the basics.
- **No arbitrary custom pages.** If you need a screen that is not list, detail,
  create, edit or delete, write a route; you have a whole framework for it.
- **No workflow builder.**
- **Not a customer-facing surface.** It concentrates read access to every
  registered model into one place. That is what makes it valuable to an operator
  and what makes it the wrong thing to put in front of anybody else.

One more thing worth knowing before you mount it: route paths are literal per
model, so `/{slug}/new` is a static segment that wins over `/{slug}/{pk}`. A
model with a *text* primary key whose value is exactly `new` is therefore not
reachable through the admin. Every generated admin makes this trade; this one
writes it down.

Reference: [`wreath.admin`](../reference/admin.md).
