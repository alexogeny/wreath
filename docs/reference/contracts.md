---
description: Transport-neutral, startup-compiled dataclass contracts.
keywords: data contract, schema validation, native JSON, dataclass codec
---

# Contracts

`wreath.contracts` gives one dataclass a reusable validation and JSON boundary.
It compiles the same native plan request binding uses, once, when the contract is
declared.

Use it when the data shape belongs to more than one transport. Use
[`wreath.binding`](binding.md) when the shape belongs only to an HTTP handler.

::: wreath.contracts
