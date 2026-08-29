"""`.objects.` chain classification. A chain's verdict turns on its arguments
and on the verbs chained after it, so each verb is classified in place and
the chain bills once at its head."""

from __future__ import annotations

import ast

from .nodes import _is_true

# ormar QuerySet verb -> rule. The verb immediately after `.objects.` is the one
# that names the shape of the rewrite; a trailing `.all()`/`.get_or_none()` is
# just how ormar spells "now run it", which wreath spells `session.fetch(...)`.
_QUERY_RULE = {
    "filter": "orm.query.filter",
    "exclude": "orm.query.filter",
    "get_or_none": "orm.query.get_or_none",
    "get": "orm.query.get",
    "create": "orm.query.create",
    "all": "orm.query.all",
    "select_related": "orm.query.eager",
    "select_all": "orm.query.select_all",
    "prefetch_related": "orm.query.eager",
    "values": "orm.query.values",
    "values_list": "orm.query.values",
    "fields": "orm.query.values",
    "paginate": "orm.query",
    "bulk_create": "orm.query.bulk",
    "bulk_update": "orm.query.bulk",
    "count": "orm.query.count",
    "exists": "orm.query.exists",
    "delete": "orm.query.delete",
    "first": "orm.query.first",
    "last": "orm.query.first",
    "get_or_create": "orm.query.get_or_create",
    "update_or_create": "orm.query.get_or_create",
    # `order_by` heads a chain often enough to deserve its own verdict. Without
    # one it fell through to the generic `orm.query`, which is *unsupported* —
    # so the one shape wreath handles best (an explicitly ordered read) was
    # reported as the shape it cannot do at all.
    "order_by": "orm.query.order",
    "limit": "orm.query",
    "offset": "orm.query",
}


# ormar keyword lookup -> the wreath comparison it becomes, as an operator.
LOOKUP_OPERATOR: dict[str, str] = {
    "": "==",
    "exact": "==",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
# ormar keyword lookup -> the column method it becomes, and how the value is
# spelled inside it. `%s` is the value as written.
# The pattern lookups were held back for a long time on the grounds that
# wrapping a value in wildcards is "a decision about what the author meant".
# It is not: ormar's own `icontains` compiles to `ILIKE '%' || value || '%'`,
# so writing the same thing is a translation of what they wrote and nothing
# more. Holding them back left 170 ordinary searches for a human to redo.
LOOKUP_METHOD: dict[str, tuple[str, str]] = {
    "in": ("in_", "%s"),
    "iexact": ("ilike", "%s"),
    "contains": ("like", 'f"%%{%s}%%"'),
    "icontains": ("ilike", 'f"%%{%s}%%"'),
    "startswith": ("like", 'f"{%s}%%"'),
    "istartswith": ("ilike", 'f"{%s}%%"'),
    "endswith": ("like", 'f"%%{%s}"'),
    "iendswith": ("ilike", 'f"%%{%s}"'),
    "jsonb_contains": ("contains", "%s"),
    "jsonb_contained_by": ("contained_by", "%s"),
    "jsonb_has_key": ("has_key", "%s"),
    "jsonb_has_any": ("has_any", "%s"),
    "jsonb_has_all": ("has_all", "%s"),
}
# `__isnull` is the odd one: which method it becomes depends on the *value*,
# so it only translates when that value is written out.
_NULL_METHOD = {True: "is_null", False: "is_not_null"}
_MECHANICAL_LOOKUPS = frozenset(LOOKUP_OPERATOR) | frozenset(LOOKUP_METHOD) | {"isnull"}

# Chain verbs that keep a mechanical head mechanical, and how their own
# arguments have to look. `first`/`last` are absent because wreath makes the
# ordering explicit and the source has none to carry over; `delete` because a
# bulk delete has no query form; `values` because the rows come back as models
# rather than dicts, which the caller sees; `update` because it is a write.
#   "kwargs"   — keyword filters, checked the same way the head's are
#   "value"    — one argument carried across untouched (limit/offset)
#   "columns"  — string literals naming columns, which resolve to `Model.<col>`
#   "relations" — string literals naming relations, one `.include(...)` each
_MECHANICAL_TAIL: dict[str, str] = {
    "all": "kwargs",
    "count": "kwargs",
    "exists": "kwargs",
    "get_or_none": "kwargs",
    "get": "kwargs",
    # A second `filter` is another `.where(...)`; wreath ands them together the
    # same way ormar does. `exclude` is deliberately absent — it negates, which
    # is a different call, and the translated message does not describe it.
    "filter": "kwargs",
    "limit": "value",
    "offset": "value",
    "order_by": "columns",
    "paginate": "page",
    # `filter(a=1).select_related("owner")` is two rewrites that are each
    # already determined on their own — a `.where(...)` and an `.include(...)`.
    # Leaving them off this table meant that putting two translatable calls next
    # to each other produced a verdict neither of them earns, and putting them
    # next to each other is how a query chain is usually written.
    "select_related": "relations",
    "prefetch_related": "relations",
    "delete": "empty",
    "update": "write_values",
}

# Head verb -> the rule to use when the arguments are mechanical. A verb absent
# here is never auto-translated whatever its arguments look like.
_QUERY_EXACT_RULE = {
    "filter": "orm.query.filter_exact",
    "get_or_none": "orm.query.get_or_none_exact",
    "get": "orm.query.get_exact",
    "create": "orm.query.create_exact",
    "all": "orm.query.all",
    "count": "orm.query.count",
    "exists": "orm.query.exists",
    "order_by": "orm.query.order_exact",
    "select_related": "orm.query.eager_exact",
    "prefetch_related": "orm.query.eager_exact",
    "select_all": "orm.query.select_all_exact",
    "get_or_create": "orm.query.get_or_create_exact",
    "values": "orm.query.values_exact",
    "values_list": "orm.query.values_exact",
    "paginate": "orm.query.page_exact",
    "limit": "orm.query.page_exact",
    "offset": "orm.query.page_exact",
}


def _projection_names(call: ast.Call | None) -> tuple[str, ...] | None:
    """Literal field names selected by an ormar ``values`` call."""
    if call is None:
        return None
    allowed = {"fields", "exclude_through"}
    if any(keyword.arg not in allowed for keyword in call.keywords):
        return None
    exclude = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "exclude_through"),
        None,
    )
    if exclude is not None and not _is_true(exclude):
        return None
    fields = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "fields"),
        None,
    )
    arguments: tuple[ast.expr, ...]
    if fields is not None:
        if call.args:
            return None
        arguments = (fields,)
    else:
        arguments = tuple(call.args)
    names: list[str] = []
    for argument in arguments:
        values = (
            argument.elts if isinstance(argument, (ast.List, ast.Tuple, ast.Set)) else (argument,)
        )
        for value in values:
            if not (
                isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value
            ):
                return None
            names.append(value.value)
    return tuple(names) if names else None


def _projection_is_mechanical(
    call: ast.Call | None,
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
) -> bool:
    """Whether a literal dictionary projection names known model fields."""
    names = _projection_names(call)
    if names is None:
        return False
    declared = set((columns or {}).get(model, ())) | set((relations or {}).get(model, ()))
    return not declared or set(names) <= declared


def _pagination_values(call: ast.Call | None) -> tuple[ast.expr, ast.expr] | None:
    """The page and page-size expressions of a static Ormar paginate call."""
    if call is None or len(call.args) > 2:
        return None
    values: dict[str, ast.expr] = {}
    for name, argument in zip(("page", "page_size"), call.args, strict=False):
        values[name] = argument
    for keyword in call.keywords:
        if keyword.arg not in {"page", "page_size"} or keyword.arg in values:
            return None
        values[keyword.arg] = keyword.value
    page = values.get("page")
    page_size = values.get("page_size")
    return (page, page_size) if page is not None and page_size is not None else None


def _order_argument_is_mechanical(argument: ast.expr, model: str = "") -> bool:
    """A literal column name or an already-explicit model order expression."""
    if isinstance(argument, ast.Constant):
        return isinstance(argument.value, str) and bool(argument.value.strip("-"))
    expression = argument
    if (
        isinstance(expression, ast.Call)
        and not expression.args
        and not expression.keywords
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr in {"asc", "desc"}
    ):
        expression = expression.func.value
    if not isinstance(expression, ast.Attribute):
        return False
    return bool(model) and ast.unparse(expression.value) == model


def _eager_paths(call: ast.Call | None) -> tuple[str, ...] | None:
    if call is None or not call.args or call.keywords:
        return None
    paths: list[str] = []
    for argument in call.args:
        values = (
            argument.elts if isinstance(argument, (ast.List, ast.Tuple, ast.Set)) else (argument,)
        )
        for value in values:
            if not (
                isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value
            ):
                return None
            paths.append(value.value)
    return tuple(paths) if paths else None


def _relation_path_resolves(model: str, path: str, relations: dict[str, dict[str, str]]) -> bool:
    current = model
    for name in path.split("__"):
        target = relations.get(current, {}).get(name)
        if target is None:
            return False
        current = target
    return True


def _eager_names_are_literal(
    call: ast.Call | None,
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
) -> bool:
    """Whether `select_related(...)` names relations this analyzer can resolve.

    `select_related("llama")` is `.include(Model.llama.selectin())` — a
    rename. `select_all()` is not: it means "every relation", and wreath has
    no such switch, so the set has to be written out by someone who knows which
    ones the caller actually reads. A non-literal is a runtime name. A nested
    ``a__b`` trail carries across when the tree index resolves every relation.
    """
    paths = _eager_paths(call)
    if paths is None:
        return False
    if not model or relations is None or model not in relations:
        return all("__" not in path for path in paths)
    return all(_relation_path_resolves(model, path, relations) for path in paths)


# Tail verbs that only a specific head makes mechanical. `first` is the case:
# `orm.query.first` is held back because "first without an order is not
# deterministic" — an objection that does not apply when the head *is* the
# order. Keyed by head so `filter(a=1).first()` keeps the old verdict.
_TAIL_NEEDS_HEAD: dict[str, frozenset[str]] = {
    "first": frozenset({"order_by"}),
    "last": frozenset(),  # reversing the declared order is a decision
}


def split_lookup(keyword: str) -> tuple[str, str]:
    """`("name", "icontains")` for `name__icontains`; `("name", "")` for `name`."""
    column, separator, suffix = keyword.rpartition("__")
    if not separator or suffix not in _MECHANICAL_LOOKUPS:
        return keyword, ""
    return column, suffix


def _lookup_is_mechanical(
    keyword: str,
    value: ast.expr | None = None,
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
) -> bool:
    """Whether `filter(<keyword>=v)` becomes a wreath predicate on its own.

    A bare column name is an equality test. A name with `__` is a column plus
    a lookup *only* when the trailing segment is one wreath has a spelling for;
    otherwise it is a relation traversal (`owner__name`) or a container lookup
    (`tags__jsonb_has_any`). The container lookup needs an operator someone
    chooses. The relation does *not* need a join chosen — `Model.owner.name`
    is a `RelatedColumnExpr` and the compiler emits the INNER JOIN itself — it
    needs the relation *resolved*, and `owner`'s target model is usually
    declared in another module. Reading the suffix is what separates them, and
    getting it wrong in the permissive direction would emit an attribute chain
    against a column that is not a relation at all.

    `__isnull` is the one lookup whose *value* decides the answer: `is_null()`
    and `is_not_null()` are different calls, so a variable there is unreadable.
    """
    column, suffix = split_lookup(keyword)
    if not suffix:
        return "__" not in keyword or _resolved_column_path(
            model, keyword, relations or {}, columns or {}
        )
    if not column:
        return False
    if "__" in column and not _resolved_column_path(model, column, relations or {}, columns or {}):
        return False
    if suffix == "isnull":
        return isinstance(value, ast.Constant) and value.value in _NULL_METHOD
    return True


def _resolved_column_path(
    model: str,
    path: str,
    relations: dict[str, dict[str, str]],
    columns: dict[str, set[str]],
) -> bool:
    """Whether ``owner__name`` is a tree-proven relationship column path."""
    parts = path.split("__")
    current = model
    if not current or len(parts) < 2:
        return False
    for relationship in parts[:-1]:
        target = relations.get(current, {}).get(relationship)
        if target is None:
            return False
        current = target
    return parts[-1] in columns.get(current, set())


def _call_is_mechanical(
    call: ast.Call | None,
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
    plain_mappings: frozenset[str] = frozenset(),
) -> bool:
    """Whether every argument of a `.objects.<verb>(...)` call maps across.

    Positional arguments are never mechanical: in ormar they are `Q` objects
    or raw clauses, and neither has a form this analyzer can read. `**kwargs`
    likewise — the keys are a runtime value, so there is nothing to check.
    """
    if call is None:
        return True  # `.objects.all` with no call at all
    if call.args:
        return False
    return all(
        (
            keyword.arg is None
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id in plain_mappings
        )
        or (
            keyword.arg is not None
            and _lookup_is_mechanical(
                keyword.arg,
                keyword.value,
                model=model,
                relations=relations,
                columns=columns,
            )
        )
        for keyword in call.keywords
    )


def plain_filter_mappings(call: ast.Call | None, parents: dict[int, ast.AST]) -> frozenset[str]:
    """Names of ``**mapping`` arguments proven to contain plain field keys.

    This is deliberately a tiny data-flow proof, not an ormar dictionary
    compatibility layer. A mapping starts from a literal/dict constructor and
    may gain literal subscript keys or literal ``update`` keys in the same
    function. A parameter, dynamic key, aliasing call, or unknown assignment
    makes it unreadable and keeps the query under review.
    """
    if call is None:
        return frozenset()
    wanted = {
        keyword.value.id
        for keyword in call.keywords
        if keyword.arg is None and isinstance(keyword.value, ast.Name)
    }
    if not wanted:
        return frozenset()
    ancestor: ast.AST | None = call
    while ancestor is not None and not isinstance(
        ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        ancestor = parents.get(id(ancestor))
    if not isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return frozenset()
    parameters = {
        argument.arg
        for argument in (
            *ancestor.args.posonlyargs,
            *ancestor.args.args,
            *ancestor.args.kwonlyargs,
        )
    }
    safe: set[str] = set()
    unsafe = wanted & parameters

    def plain_key(value: ast.AST | None) -> bool:
        return (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and "__" not in value.value
        )

    def plain_dict(value: ast.AST | None) -> bool:
        if isinstance(value, ast.Dict):
            return all(plain_key(key) for key in value.keys)
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "dict"
            and not value.args
            and all(
                keyword.arg is not None and "__" not in keyword.arg for keyword in value.keywords
            )
        )

    for node in ast.walk(ancestor):
        target: ast.AST | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id in wanted:
            (safe if plain_dict(value) else unsafe).add(target.id)
            continue
        if isinstance(node, ast.AugAssign):
            augmented = node.target
            if isinstance(augmented, ast.Name) and augmented.id in wanted:
                unsafe.add(augmented.id)
            elif (
                isinstance(augmented, ast.Subscript)
                and isinstance(augmented.value, ast.Name)
                and augmented.value.id in wanted
            ):
                unsafe.add(augmented.value.id)
            continue
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id in wanted
        ):
            (safe if plain_key(target.slice) else unsafe).add(target.value.id)
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in wanted
        ):
            valid = (
                not node.args
                and all(
                    keyword.arg is not None and "__" not in keyword.arg for keyword in node.keywords
                )
            ) or (len(node.args) == 1 and not node.keywords and plain_dict(node.args[0]))
            (safe if valid else unsafe).add(node.func.value.id)
            continue
        if isinstance(node, ast.Call) and node is not call:
            escaped = {
                argument.id
                for argument in node.args
                if isinstance(argument, ast.Name) and argument.id in wanted
            }
            escaped.update(
                keyword.value.id
                for keyword in node.keywords
                if isinstance(keyword.value, ast.Name) and keyword.value.id in wanted
            )
            unsafe.update(escaped)
    return frozenset((safe & wanted) - unsafe)


def query_rule(
    verb: str | None,
    call: ast.Call | None = None,
    tail: tuple[tuple[str, ast.Call | None], ...] = (),
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
    tables: dict[str, str] | None = None,
    unique_constraints: dict[str, tuple[frozenset[str], ...]] | None = None,
    plain_mappings: frozenset[str] = frozenset(),
) -> str:
    """The rule for a `.objects.<verb>` chain, or the generic one.

    Shared with the emitter so the `# TODO(wreath-port: …)` it writes into the
    source says exactly what the report said — a porter grepping one and reading
    the other must not find two different verdicts for the same line.

    `call` and `tail` are what promote a verb from "here is the shape of the
    rewrite" to "here is the rewrite": the arguments have to carry across
    unchanged, and every verb layered on top has to as well. Called without them
    the answer is the conservative one, so a caller that cannot see the chain
    never gets a translated verdict by omission.
    """
    base = _QUERY_RULE.get(verb or "", "orm.query")
    exact = _QUERY_EXACT_RULE.get(verb or "")
    if exact is None:
        return base
    if verb == "get_or_create":
        if call is None or call.args or not call.keywords:
            return base
        names = {keyword.arg for keyword in call.keywords}
        if None in names or "_defaults" in names or "defaults" in names:
            return base
        declared = (columns or {}).get(model)
        if declared is not None and not names <= declared:
            return base
    elif verb in ("values", "values_list"):
        if not _projection_is_mechanical(call, model=model, relations=relations, columns=columns):
            return base
    elif verb == "select_all":
        # An unknown model is not a relation-free model. `select_all()` is an
        # exact no-op only where the tree index saw the declaration and found
        # no relationships on it.
        if relations is None or model not in relations or relations[model]:
            return base
        if call is None or call.args or call.keywords:
            return base
    elif verb == "order_by":
        # The head's own arguments are column names, not filters.
        if not _tail_step_is_mechanical("order_by", call, model=model):
            return base
    elif verb in ("limit", "offset"):
        if not _tail_step_is_mechanical(verb, call):
            return base
    elif verb == "paginate":
        if _pagination_values(call) is None:
            return base
    elif verb in ("select_related", "prefetch_related"):
        if not _eager_names_are_literal(call, model=model, relations=relations):
            return base
    elif verb == "create":
        if call is None or call.args:
            return base
    elif not _call_is_mechanical(
        call,
        model=model,
        relations=relations,
        columns=columns,
        plain_mappings=plain_mappings,
    ):
        return base
    ordered = verb == "order_by"
    pending_projection = False
    for step, node in tail:
        if step == "fields":
            if not _projection_is_mechanical(
                node, model=model, relations=relations, columns=columns
            ):
                return base
            pending_projection = True
            continue
        if step == "values":
            empty_values = node is not None and not node.args and not node.keywords
            if not (
                (pending_projection and empty_values)
                or _projection_is_mechanical(
                    node, model=model, relations=relations, columns=columns
                )
            ):
                return base
            pending_projection = False
            continue
        if step == "values_list":
            empty_values = node is not None and not node.args and not node.keywords
            if not (
                (pending_projection and empty_values)
                or _projection_is_mechanical(
                    node, model=model, relations=relations, columns=columns
                )
            ):
                return base
            pending_projection = False
            continue
        if pending_projection:
            return base
        context = "order_by" if ordered else verb or ""
        if not _tail_step_is_mechanical(
            step,
            node,
            context,
            model=model,
            relations=relations,
            columns=columns,
            plain_mappings=plain_mappings,
        ):
            return base
        ordered = ordered or step == "order_by"
    return exact


def _tail_step_is_mechanical(
    verb: str,
    call: ast.Call | None,
    head: str = "",
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
    plain_mappings: frozenset[str] = frozenset(),
) -> bool:
    """Whether a verb chained after the head carries across with its arguments.

    Checked rather than assumed: `.all(name__icontains=x)` is a filter wearing
    a terminal's name, and treating the verb alone as safe would let the lookup
    the head check exists to catch through the back door.

    `head` is what lets `first` be mechanical after `order_by` and nowhere
    else — the verb alone does not decide it.
    """
    if verb in _TAIL_NEEDS_HEAD:
        return head in _TAIL_NEEDS_HEAD[verb]
    kind = _MECHANICAL_TAIL.get(verb)
    if kind is None:
        return False
    if call is None:
        return True  # referenced, not called
    if kind == "kwargs":
        return _call_is_mechanical(
            call,
            model=model,
            relations=relations,
            columns=columns,
            plain_mappings=plain_mappings,
        )
    if kind == "value":
        # `limit(n)`/`offset(n)`: one argument, whatever it is, carried across.
        return len(call.args) == 1 and not call.keywords
    if kind == "page":
        return _pagination_values(call) is not None
    if kind == "empty":
        return not call.args and not call.keywords
    if kind == "write_values":
        return not call.args and bool(call.keywords)
    if kind == "relations":
        return _eager_names_are_literal(call, model=model, relations=relations)
    # `order_by("name")` / `order_by("-created")` resolve to `Model.<col>` and
    # `.desc()`. A non-literal is a runtime column name, which is a lookup this
    # analyzer cannot do.
    return (
        bool(call.args)
        and not call.keywords
        and all(_order_argument_is_mechanical(argument, model) for argument in call.args)
    )


def chain_tail(
    head: ast.AST, parents: dict[int, ast.AST]
) -> tuple[tuple[str, ast.Call | None], ...]:
    """The verbs applied after the head of a `.objects.<head>(...)` chain.

    `Model.objects.filter(x=1).all()` bills once, at `filter` — so whether
    that finding is honest depends on what came after it, and the head node
    cannot see downstream without walking back up the tree. Each verb comes back
    with its own call, because a tail verb's arguments decide as much as the
    head's do.
    """
    steps: list[tuple[str, ast.Call | None]] = []
    current = parents.get(id(head))
    while isinstance(current, ast.Call):
        parent = parents.get(id(current))
        if not isinstance(parent, ast.Attribute):
            break
        outer = parents.get(id(parent))
        steps.append((parent.attr, outer if isinstance(outer, ast.Call) else None))
        current = outer
    return tuple(steps)


def query_chain_runs(head: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Whether a `Model.objects.…` chain *executes*, rather than only building.

    A chain that ends at `filter(...)` is a query object and needs nothing to
    make it; one that ends at `all()`, `count()`, `exists()` or `get_or_none()`
    has to be run, and in wreath that means a session. Only the second kind
    forces a session onto the function it sits in.
    """
    verbs = {head.attr if isinstance(head, ast.Attribute) else ""}
    verbs.update(verb for verb, _ in chain_tail(head, parents))
    return bool(verbs & _RUNNING_VERBS)


#: The chain verbs that execute a query.
_RUNNING_VERBS = frozenset(
    {
        "all",
        "get_or_none",
        "get",
        "first",
        "create",
        "count",
        "exists",
        "update",
        "delete",
        "get_or_create",
    }
)


#: Query verdicts the emitter writes out in full. Defined here rather than in
#: `emit` because `TreeContext` has to agree with it while deciding which
#: functions need a session, and a second copy of the list would drift.
QUERY_TRANSLATED = frozenset(
    {
        "orm.query.filter_exact",
        "orm.query.get_or_none_exact",
        "orm.query.get_exact",
        "orm.query.create_exact",
        "orm.query.all",
        "orm.query.page_exact",
        "orm.query.count",
        "orm.query.exists",
        "orm.query.order_exact",
        "orm.query.eager_exact",
        "orm.query.select_all_exact",
        "orm.query.get_or_create_exact",
        "orm.query.values_exact",
    }
)
