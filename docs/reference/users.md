# `wreath.users`

User-management lifecycle flows — register / login / verify / password-reset — on top of the native auth primitives, with pluggable user store and email sender.

It also carries the second-factor kit: `second_factor_router` mounts TOTP enrolment, verification, step-up and removal — and, when it is given an `rp_id`, WebAuthn registration and assertion as well. `SecondFactorStore` is where credentials live, and `user_router(second_factors=...)` is what turns a login into a *pending* one until the code or the passkey arrives. Verification stamps `second_factor_at` on the session, which is what [`wreath.auth.second_factor`](auth.md) and the Cedar context's `second_factor_age` read, so a route can demand a *recent* factor rather than merely an enrolled one. Start with the [second factors guide](../guides/second-factors.md) for why the flow is shaped the way it is; the signatures are below.

Two things about the pair are worth knowing before you read them. **`SecondFactorStore.touch` is a conditional advance that returns whether it won** — it stores the counter only when it is strictly greater than the one already there, and a store that answers `False` is telling the caller that somebody else spent that step, which verification treats as a replay. A store of your own that updates unconditionally re-opens the race two requests carrying one code make. And **the two routers are not independent**: `second_factor_router` records itself against the `UserStore` it is built with, so a `user_router` over that same store that was never given `second_factors=` refuses — rather than completes — the login of a user who has a factor enrolled, and clears any half-finished enrolment or WebAuthn ceremony from the session at both login and logout.

::: wreath.users
