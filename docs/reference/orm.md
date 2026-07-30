# `wreath.orm`

Models, fields, relationships, query construction, and persistence mapping. Depends on `wreath.postgres`, never the reverse.

Almost every name here is re-exported from `wreath.orm`, so
`from wreath.orm import Model, Session, column` works no matter which submodule
defines it. Two are conventionally imported from their own module instead,
because they are what a query is written out of: the column types a model is
declared with (`from wreath.orm.types import Int64, Text, Vector`), and the
expression vocabulary a declared column then gives you — comparisons, JSONB
paths, array operators, the pgvector distances, and the full-text
`matches`/`rank` pair.

The sections below are grouped by submodule, because that is where the
implementations — and the docstrings this page is generated from — live. Reading
in order takes you from the errors the ORM raises, through declaring a model, to
running a query and validating the schema behind it.

::: wreath.orm

::: wreath.orm.errors

::: wreath.orm.types

::: wreath.orm.fields

::: wreath.orm.constraints

::: wreath.orm.relations

::: wreath.orm.model

::: wreath.orm.table

::: wreath.orm.schema

::: wreath.orm.registry

::: wreath.orm.expressions

::: wreath.orm.query

::: wreath.orm.session

::: wreath.orm.compiler

::: wreath.orm.validation

::: wreath.orm.introspection
