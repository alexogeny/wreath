---
description: Hosted payments, subscriptions, Stripe Connect and Managed Payments without direct card capture.
keywords: API reference billing payments subscriptions Stripe Connect Managed Payments PCI DSS SAQ A
---

# Billing and subscriptions

Wreath's billing surface deliberately accepts provider price identifiers, customers and
payment identifiers, but never a PAN, CVC, track value or payment-method token. Checkout
always redirects to the provider's hosted page. That architecture can reduce a deployment's
PCI DSS questionnaire to SAQ A when every other eligibility condition is met; it is not a
claim that an application is compliant. Confirm the applicable validation method with the
acquirer or payment brand and retain the provider's own compliance evidence.

## Stripe Checkout

Create Stripe's outbound client through the application so lifespan management and
infrastructure inference can see it. Product and Price objects remain owned by Stripe; the
catalog is a startup-validated map from stable application SKUs to those Price IDs.

```python
from wreath import Wreath
from wreath.billing import DeploymentMerchant, HostedRedirect, PostgresBillingLedger
from wreath.billing.providers.stripe import StripeBilling
from wreath.config import Secret
from wreath.subscriptions import Plan, PlanCatalog

app = Wreath()
app.postgres("main", dsn="postgresql://wreath@postgres/app")
stripe_http = app.http_client("stripe", base_url="https://api.stripe.com")
stripe = StripeBilling(
    client=stripe_http,
    api_key=Secret("rk_live_..."),
    api_version="2026-08-26.dahlia",
    allowed_return_origins=("https://app.example",),
)
billing = app.billing(
    "commerce",
    backend=stripe,
    catalog=PlanCatalog(
        Plan("pro", "price_live_pro", entitlements=frozenset({"api", "export"})),
        Plan("credits", "price_live_credits", mode="payment"),
    ),
    merchant=DeploymentMerchant(),
    capture=HostedRedirect(),
    customer_for=provider_customer_for_subject,
    payment_for=provider_payment_for_subject,
    ledger=PostgresBillingLedger(),
    database="main",
)
```

The ledger contributes `billing_commands`, `billing_payments`, `billing_subscriptions`,
`billing_invoices` and `billing_reconciliation` to Wreath's schema lifecycle. One-time payment
state advances monotonically; subscription and invoice facts are merged in either arrival order.
In a multi-database app, `database` makes that ownership explicit. Without a durable ledger,
`billing.compliance_posture()` reports the missing production control.

The application chooses the SKU, quantity and HTTPS return locations. Wreath resolves the
provider price and uses the opaque reference as Stripe's idempotency key and Checkout
`client_reference_id`.

```python
session = await billing.checkout(
    subject="organization:acme",
    plan="pro",
    quantity=3,
    success_url="https://app.example/billing/success",
    cancel_url="https://app.example/billing/cancel",
    reference="01JCOMMERCECHECKOUT",
)
```

The browser return is presentation only. Grant access from a verified webhook projection,
not from arrival at `success_url`.

Synchronous Stripe calls use Stripe's idempotency record as the outbound source of truth: after
a process dies following provider success, repeat the same opaque reference and identical request
to recover the same Stripe result. The local `billing_commands` lifecycle is for queue-backed
dispatchers; such a dispatcher registers and fences the command before I/O and marks an expired
post-send lease `unknown` for explicit reconciliation. Wreath does not hold a PostgreSQL
transaction open across Stripe network I/O.

## Managed Payments

Managed Payments makes Stripe the merchant of record for eligible digital goods. It requires
Stripe API version `2025-03-31.basil` or later, an activated account, accepted terms and an
eligible tax code on every product. Wreath adds
`managed_payments[enabled]=true`, refuses a Connect combination, and requires the declaration
to use `ProviderMerchant`.

```python
from wreath.billing import ProviderMerchant

stripe = StripeBilling(
    client=stripe_http,
    api_key=Secret("rk_live_..."),
    api_version="2026-08-26.dahlia",
    allowed_return_origins=("https://app.example",),
    managed_payments=True,
)
billing = app.billing(
    "commerce",
    backend=stripe,
    catalog=PlanCatalog(Plan("pro", "price_managed_pro")),
    merchant=ProviderMerchant(),
    capture=HostedRedirect(),
)
```

Stripe still leaves product eligibility, product support and some jurisdictional tax cases
with the deployment. `billing.compliance_posture()` therefore reports unresolved work rather
than returning a compliance boolean.

## Connect

Connect account selection is a topology decision, separate from tenancy and merchant-of-record
responsibility. `ConnectedMerchants` resolves the account from the billing subject; request
callers cannot override it.

```python
from wreath.billing import ConnectedMerchant, ConnectedMerchants
from wreath.billing.providers.stripe import DirectCharges, StripeConnect

stripe = StripeBilling(
    client=stripe_http,
    api_key=Secret("rk_live_..."),
    api_version="2026-08-26.dahlia",
    allowed_return_origins=("https://app.example",),
    connect=StripeConnect(
        DirectCharges(
            application_fee_percent="12.5",
            refund_application_fee=True,
        )
    ),
)
billing = app.billing(
    "marketplace",
    backend=stripe,
    catalog=PlanCatalog(Plan("pro", "price_connected_pro")),
    merchant=ConnectedMerchant(),
    topology=ConnectedMerchants(
        account_for=merchant_account_for_subject,
        price_for=provider_price_for_subject_plan_account,
        sku_for_price=sku_for_subject_provider_price_account,
    ),
    capture=HostedRedirect(),
)
```

Direct charges use the connected account's Price and the `Stripe-Account` header. Because
Stripe Prices belong to an account, direct charges require paired forward and inverse Price
resolvers. Wreath compares the inverse with the active subject and account before accepting a
webhook projection. Destination charges use the platform Price and put the destination in the
PaymentIntent or subscription. Their refund liability is fixed at declaration:

```python
from wreath.billing import DeploymentMerchant
from wreath.billing.providers.stripe import DestinationCharges, DestinationRefunds

connect = StripeConnect(
    DestinationCharges(
        application_fee_percent="8",
        refunds=DestinationRefunds(
            reverse_transfer=True,
            refund_application_fee=True,
        ),
    )
)
```

Use `ConnectedMerchant` instead when `on_behalf_of=True`; otherwise destination charges use
`DeploymentMerchant`. Separate charges and transfers are intentionally not accepted: they need
a separate one-to-many settlement ledger rather than a Checkout option.
For destination charges, Wreath writes the original destination account into namespaced Checkout,
PaymentIntent or Subscription metadata. Authoritative projections recover that immutable route,
so a later refund cannot silently follow a tenant's changed account mapping.

## Portal, refunds and webhook truth

`billing.portal()` creates a host-validated Stripe Customer Portal session. `billing.refund()`
preserves Stripe's pending, action-required, succeeded, failed and canceled outcomes. Both
operations use idempotency keys. Direct Connect operations carry the stored account scope;
destination-refund transfer and application-fee recovery come from the declaration above.
The application-owned `customer_for` resolver may return `None` for a first purchase; an
existing Customer is sent to Checkout so a later portal session sees the same subscription.
The `payment_for` resolver returns `ProviderPayment`, including currency and the original
Connect account, so a partial refund cannot silently change currency or route through the
subject's current account.

Use Wreath's existing Stripe signature verifier and durable webhook inbox. The projection
policy separately pins the event destination's API version, live/test mode and account scope.
Projection policies require `2025-03-31.basil` or later because the invoice parser accepts only
Stripe's current typed `parent` shape. Zero-total Checkout sessions project as succeeded Checkout
facts keyed by their Session ID; they do not invent a refundable PaymentIntent.

```python
from wreath.billing.providers.stripe import StripeWebhookPolicy
from wreath.webhooks import StripeWebhookVerifier

source = app.webhooks("billing").source(
    "stripe-account",
    path="/webhooks/stripe",
    verifier=StripeWebhookVerifier("whsec_..."),
    inbox=inbox,
    session_factory=session_factory,
)
projection_policy = StripeWebhookPolicy(
    event_version="2026-08-26.dahlia",
    livemode=True,
    scope="account",
)
billing.stripe_webhooks(
    source,
    webhook=projection_policy,
    checkout_subject_for=subject_for_checkout_reference,
    subscription_subject_for=subject_for_stripe_customer,
)
```

Webhook handlers run inside the inbox transaction, so applying the projection and marking the
event processed commit together. The ledger makes invoice-first and subscription-first delivery
equivalent across workers and restarts, deduplicates invoice IDs, retains the furthest
paid-through instant and refuses a payment or subscription that changes subject, account,
currency or amount. A subscription status event never advances paid-through on its own.

For direct charges, use a distinct Connect webhook endpoint and secret with
`scope="connected_accounts"`. The verified event must carry its top-level Stripe account, and
the customer/account pair must resolve through the application's central mapping before any
tenant scope is entered. Construct its projection with
`plan_for_price=billing.plan_for_provider_price`; connected-account projections refuse to start
without that inverse resolver.

`SubscriptionEntitlements` reads the local projection. Every non-trial state selected by an
`AccessPolicy` grants only through `paid_through`; trials grant only before `trial_ends_at`.
Past-due access is not selected by the default policy. Cedar resolves the plan and entitlements
from one snapshot, so an account change cannot combine facts from two reads.

## Read side and entitlements

`billing.queries()` builds the subject-scoped PostgreSQL read side. Every lookup takes the
application's internal billing subject; callers cannot supply a Stripe customer or Connect
account as authority. Invoice history is keyset-paginated and bounded to 100 rows per page.

```python
queries = billing.queries(session_factory)

subscription = await queries.subscription("organization:acme")
payment = await queries.payment("organization:acme", "pi_...")
page = await queries.invoices("organization:acme", limit=20)
```

The entitlement adapter performs one subscription query and gives Cedar one atomic plan and
entitlement snapshot. The identity-to-subject mapping remains application-owned.

```python
from wreath.authorization import CedarAuthorizer

entitlements = billing.entitlements(
    queries,
    subject_for=lambda identity: f"user:{identity.id}",
)
authorizer = CedarAuthorizer(engine=policies, entitlements=entitlements)
```

## Reconciliation, counters and alerts

`billing.reconciliation()` registers a durable Stripe reconciliation task on a Wreath
`JobRunner`. The provider page is fetched without holding a database transaction. Projection
writes and compare-and-swap cursor advancement then commit together. Pages are bounded to 100
objects, and every object must match the requested Connect account before the ledger sees it.

```python
jobs = app.jobs("billing", database="main")
reconciliation = billing.reconciliation(
    jobs=jobs,
    session_factory=session_factory,
    retrieve_page=retrieve_stripe_reconciliation_page,
    merchant_accounts=(None,),
    cron="*/15 * * * *",
)
```

The billing control plane is automatically visible to `wreath.metrics.collect(app)` through
`billing.operations`. Its cached counters perform no database or network I/O. Add its
non-critical alert to a health router when webhook lag, reconciliation age, unknown command
outcomes or dead outcomes need an operator rather than instance eviction.

`billing.preflight()` remains non-empty until the Stripe webhook binding and durable
reconciliation have both been declared. It performs no I/O, so deployment tooling can inspect
the built application before startup.

```python
alert = billing.operations.alert(
    webhook_lag=300,
    reconciliation_age=1800,
)
```

## Support and agent safety

The support facade derives the billing subject from the linked chat identity and tenant. Reads
are off until `SupportAccess` supplies both an application permission and Cedar authorization.
Money movement is a second, separately disabled capability.

```python
from wreath.billing import BillingSupport, SupportAccess

support = billing.support(
    reader=queries,
    subject_for=subject_for_support_identity,
    access=SupportAccess(authorize=authorize_support_read),
)
payment = await support.payment(chat_context, "pi_...")
```

A refund cannot be executed through the agent request path. Enabling refunds requires an
explicit `SupportMoneyMovement`, a linked principal with `billing.refund`, a Cedar permit at
proposal time and again at execution time, fresh authentication, a single-use human approval,
and a completed audit write before Stripe is called. The exact subject, payment, amount,
currency and reference are sealed into the approval record; button input carries only its
opaque approval ID. Use the PostgreSQL store in any multi-process or multi-instance deployment
so a different worker can safely receive the button callback.

```python
from wreath.agents import ChatApprovalFlow, PostgresApprovalStore
from wreath.billing import SupportMoneyMovement

approval_store = PostgresApprovalStore(session_factory, schema="wreath")
chat_approval_flow = ChatApprovalFlow(chat, approval_store)
money = SupportMoneyMovement(
    approvals=chat_approval_flow,
    authorize=authorize_refund,
    audit=write_durable_billing_audit,
)
support = billing.support(
    reader=queries,
    subject_for=subject_for_support_identity,
    access=SupportAccess(authorize=authorize_support_read),
    money=money,
)

proposal = await support.propose_refund(
    chat_context,
    payment="pi_...",
    reference="support-case-1842",
    amount=Money("USD", 1000),
)
```

The call above only renders Approve and Deny controls. Stripe is reached only after the bound
human clicks Approve. A delegated principal or any context carrying an agent request is refused,
even if it has the permission and Cedar would otherwise permit it.

::: wreath.payments

::: wreath.subscriptions

::: wreath.billing

::: wreath.billing.ledger

::: wreath.billing.operations

::: wreath.billing.queries

::: wreath.billing.reconciliation

::: wreath.billing.stripe_webhooks

::: wreath.billing.support

::: wreath.billing.providers.stripe

::: wreath.billing.providers.stripe_checkout
