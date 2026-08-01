# `wreath.infra`

The infrastructure an application requires, derived from the declarations it
already made. `wreath infra infer myapp:app` reads a built application and
returns a typed plan: the databases, the object stores, the outbound origins,
the listener, the tables each subsystem owns, and the environment keys a
deployment has to supply. Nothing is applied and nothing is contacted — this is
a plan to read before anything touches an account.

Start with [the guide](../guides/infra.md), which walks the camera-trap example
end to end and shows the output. The names below are all re-exported from
`wreath.infra`, so `from wreath.infra import infer, render_text` works whichever
submodule defines them; the sections are grouped by submodule because that is
where the docstrings live.

::: wreath.infra

::: wreath.infra.model

::: wreath.infra.inference

::: wreath.infra.settings

::: wreath.infra.render
