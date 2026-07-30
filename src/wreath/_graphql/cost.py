"""What a parsed document will *cost*, weighed against the schema.

The parser bounds a document's shape while it reads it -- depth, alias count,
parse steps, and a selection count it calls `max_complexity`. That last one is
the crude member of the set: it counts selections, so `posts { id }` and a
resolver that calls a payment provider both count one.

`SchemaField.cost` and `RootField.cost` exist to say otherwise, and until this
module they were read by nothing. They could not be read by the parser, which
is the point: parsing happens before the schema is known, so a weight that
depends on *which field* a name resolves to has to be applied afterwards. That
is the whole reason this walk exists as a second pass rather than another
counter in `_Parser`.

**Additive, never multiplicative.** A list field does not multiply its
children. Fan-out is what a declaration is *for* -- `cost=` on the field that
fans out is how an author says "this one is expensive" -- and inferring a
multiplier from `limit` arguments would make the budget depend on values a
client chooses, which is a different and much harder contract to explain. Page
size is bounded separately by `max_page_size`.

**Unknown fields cost nothing.** A field the schema does not have is a client
mistake that the executor reports with a message naming it; refusing it here
would answer a typo with `complexity`, which is both wrong and confusing. The
same goes for an unknown fragment, which `_flatten` raises on: this pass
swallows it and lets execution produce the error that names it, so a bad
document gets one diagnosis rather than two different ones depending on which
limit happened to be reached.

Fragment expansion is `execute._flatten` itself, deliberately rather than a
copy. The weigher's answer is only meaningful if it agrees with the executor
about *which fields actually run*, and two implementations of that would be two
things to keep in step -- with the failure mode that a document is billed for
work it does not do, or worse, not billed for work it does.
"""

from __future__ import annotations

from typing import Any

from .execute import ExecutionError, _flatten
from .parser import GraphQLSyntaxError

__all__ = ["weigh"]


def _fields(selections: tuple[Any, ...], document: Any, type_name: str) -> list[Any]:
    """`_flatten`, with an unknown fragment left for the executor to report."""
    try:
        return _flatten(selections, document, type_name)
    except ExecutionError:
        return []


def weigh(schema: Any, document: Any, operation: Any, *, max_complexity: int) -> int:
    """Total declared cost of `operation`, refusing it past `max_complexity`.

    Shares the parser's budget rather than introducing a second one. The two
    measure the same thing at different resolutions -- selections, then
    selections weighted by what they resolve to -- so a document that passed
    the parser can still be refused here, which is exactly the case
    `SchemaField.cost` was added for.

    Returns:
        The weighed total, for a caller that wants to log or report it.

    Raises:
        GraphQLSyntaxError: with `code="complexity"`, the same shape the
            parser's own refusal has, so one client handler covers both.
    """
    roots = schema.mutations if operation.operation == "mutation" else schema.roots
    root_type = "Mutation" if operation.operation == "mutation" else "Query"
    total = 0
    for field in _fields(operation.selection_set.selections, document, root_type):
        root = roots.get(field.name)
        if root is None:
            continue
        total += root.cost
        if field.selection_set is not None:
            total += _weigh_type(schema, document, root.type_name, field.selection_set)
        if total > max_complexity:
            raise GraphQLSyntaxError(
                f"document costs more than {max_complexity}; a field may declare "
                "a weight above 1, so this is not a count of selections",
                code="complexity",
            )
    return total


def _weigh_type(schema: Any, document: Any, type_name: str, selection_set: Any) -> int:
    """The cost of one selection set, read against the object type it is on.

    Recursion is bounded by the parser's `max_depth`, which has already run:
    a document deep enough to overflow the stack here never reaches here.
    """
    object_type = schema.type_of(type_name)
    if object_type is None:
        return 0
    total = 0
    for field in _fields(selection_set.selections, document, type_name):
        declared = object_type.fields.get(field.name)
        if declared is None:
            continue
        total += declared.cost
        if field.selection_set is not None:
            total += _weigh_type(schema, document, declared.type_name, field.selection_set)
    return total
