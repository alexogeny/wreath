---
description: Purpose-scoped action tokens with key rotation and optional single-use replay refusal.
keywords: action token, email verification token, password reset token, invite token
---

# Action tokens

`wreath.tokens` signs bounded, expiring actions such as an invitation, email
verification, or password reset. Every token names its declared purpose and key.
Single-use purposes require an explicit ledger; local memory never pretends to
protect replay across workers.

This module does not replace OAuth access tokens, API credentials, or HTTP
message signatures. Those live in [`wreath.oauth`](oauth.md),
[`wreath.auth`](auth.md), and [`wreath.signatures`](signatures.md).

::: wreath.tokens
