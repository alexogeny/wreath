"""`Model.objects.…` chains: the plan a chain folds into, and the wreath query
written in its place."""

from __future__ import annotations

import ast

from ..analyzer import (
    _NULL_METHOD,
    LOOKUP_METHOD,
    LOOKUP_OPERATOR,
    _eager_paths,
    _order_argument_is_mechanical,
    _pagination_values,
    _projection_names,
    _resolved_column_path,
    chain_tail,
    plain_filter_mappings,
    query_rule,
    split_lookup,
)
from .buffer import _Positioned
from .state import _EmitterState

#: Query verdicts whose target is fully determined, and which the emitter
#: therefore writes out rather than describing. Everything else — a write, a
#: dynamic projection or relation traversal — keeps
#: its note, because a person still has to decide something.
_QUERY_TRANSLATED = frozenset(
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

#: How each chain verb contributes to the wreath query, and what runs it.
#: `None` means the verb only builds; a string names the `session` method.
_QUERY_RUNNER = {
    "all": "fetch",
    "get_or_none": "fetch_one",
    "get": "require_one",
    "create": "create",
    "count": "count",
    "exists": "exists",
}


def _snake(name: str) -> str:
    """A stable local-name spelling for one statically known model name."""
    pieces: list[str] = []
    for character in name:
        if character.isupper() and pieces:
            pieces.append("_")
        pieces.append(character.lower() if character.isalnum() else "_")
    return "".join(pieces).strip("_") or "row"


class _QueryPlan:
    """The wreath query one `Model.objects.…` chain becomes, assembled verb by verb."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.wheres: list[str] = []
        self.orders: list[str] = []
        self.includes: list[str] = []
        self.limit: str | None = None
        self.offset: str | None = None
        self.runner: str | None = None
        self.write_values: list[str] = []
        self.primary_key: str | None = None
        self.create_pairs: list[tuple[str, str]] = []
        self.existing_name: str | None = None
        self.projection_pairs: list[tuple[str, str]] = []
        self.row_name: str | None = None

    def step(self, emitter: _QueryRewrite, verb: str, call: ast.Call | None) -> bool:
        """Fold one verb into the plan; `False` if it is not one we can write."""
        if self.runner is not None:
            return False  # nothing chains after the run
        if verb == "get_or_create":
            if call is None or call.args or not call.keywords:
                return False
            pairs = [
                (keyword.arg, emitter._seg(keyword.value))
                for keyword in call.keywords
                if keyword.arg is not None
            ]
            names = {name for name, _value in pairs}
            if (
                len(pairs) != len(call.keywords)
                or not names
                or names & {"_defaults", "defaults"}
                or any("__" in name for name in names)
            ):
                return False
            self.create_pairs = pairs
            self.wheres = [
                f"{self.model}.{name} == {value}" for name, value in pairs
            ]
            self.write_values = [f"{name}={value}" for name, value in pairs]
            self.runner = "get_or_create"
            return True
        if verb == "select_all":
            return call is not None and not call.args and not call.keywords
        if verb == "values":
            names = _projection_names(call)
            if names is not None:
                pairs = [emitter._projection_pair(self.model, name) for name in names]
                if any(pair is None for pair in pairs):
                    return False
                self.projection_pairs = [pair for pair in pairs if pair is not None]
            elif call is None or call.args or call.keywords or not self.projection_pairs:
                return False
            self.runner = "values"
            return True
        if verb == "values_list":
            names = _projection_names(call)
            if names is not None:
                pairs = [emitter._projection_pair(self.model, name) for name in names]
                if any(pair is None for pair in pairs):
                    return False
                self.projection_pairs = [pair for pair in pairs if pair is not None]
            elif call is None or call.args or call.keywords or not self.projection_pairs:
                return False
            self.runner = "values_list"
            return True
        if verb == "fields":
            names = _projection_names(call)
            if names is None:
                return False
            pairs = [emitter._projection_pair(self.model, name) for name in names]
            if any(pair is None for pair in pairs):
                return False
            self.projection_pairs = [pair for pair in pairs if pair is not None]
            return True
        if verb in ("filter", "all", "get_or_none", "get", "count", "exists"):
            if verb == "get" and call is not None and len(call.keywords) == 1:
                keyword = call.keywords[0]
                if keyword.arg in ("id", "pk"):
                    self.primary_key = emitter._seg(keyword.value)
                    self.runner = "require"
                    return not call.args
            for keyword in call.keywords if call else ():
                predicate = emitter._predicate(self.model, keyword)
                if predicate is None:
                    return False
                self.wheres.append(predicate)
            if call is not None and call.args:
                return False
            self.runner = _QUERY_RUNNER.get(verb)
            return True
        if verb == "create":
            if call is None or call.args:
                return False
            self.write_values = [
                f"{keyword.arg}={emitter._seg(keyword.value)}"
                if keyword.arg is not None
                else f"**{emitter._seg(keyword.value)}"
                for keyword in call.keywords
            ]
            self.runner = "create"
            return True
        if verb == "order_by":
            for argument in call.args if call else ():
                if not _order_argument_is_mechanical(argument, self.model):
                    return False
                if isinstance(argument, ast.Constant):
                    name = argument.value
                    if not isinstance(name, str):
                        return False
                    column = f"{self.model}.{name.lstrip('-')}"
                    self.orders.append(
                        f"{column}.desc()" if name.startswith("-") else column
                    )
                else:
                    self.orders.append(emitter._seg(argument))
            return bool(self.orders)
        if verb == "first":
            if call is None or call.args or call.keywords or not self.orders:
                return False
            self.limit = "1"
            self.runner = "fetch_one"
            return True
        if verb in ("select_related", "prefetch_related"):
            paths = _eager_paths(call)
            if paths is None:
                return False
            for path in paths:
                expression = emitter._include_expression(self.model, path)
                if expression is None:
                    return False
                self.includes.append(expression)
            return bool(self.includes)
        if verb in ("limit", "offset"):
            if call is None or len(call.args) != 1 or call.keywords:
                return False
            setattr(self, verb, emitter._seg(call.args[0]))
            return True
        if verb == "paginate":
            values = _pagination_values(call)
            if values is None:
                return False
            page, page_size = (emitter._seg(value) for value in values)
            self.limit = page_size
            self.offset = f"({page} - 1) * {page_size}"
            return True
        if verb == "delete":
            if call is None or call.args or call.keywords:
                return False
            self.runner = "delete_where"
            return True
        if verb == "update":
            if call is None or call.args or not call.keywords:
                return False
            self.write_values = [
                f"{keyword.arg}={emitter._seg(keyword.value)}"
                if keyword.arg is not None
                else f"**{emitter._seg(keyword.value)}"
                for keyword in call.keywords
            ]
            self.runner = "update_where"
            return True
        return False

    def render(self, session: str | None) -> str:
        if self.runner == "get_or_create" and self.existing_name is not None:
            query = f"{self.model}.select().where({', '.join(self.wheres)})"
            values = ", ".join(self.write_values)
            existing = self.existing_name
            return (
                f"(({existing}, False) if "
                f"({existing} := await {session}.fetch_one({query})) is not None "
                f"else (await {session}.create({self.model}, {values}), True))"
            )
        if self.runner == "create":
            suffix = f", {', '.join(self.write_values)}" if self.write_values else ""
            return f"await {session}.create({self.model}{suffix})"
        if self.runner == "require" and self.primary_key is not None:
            return f"await {session}.require({self.model}, {self.primary_key})"
        selected = ", ".join(
            f"{self.model}.{attribute}" for _key, attribute in self.projection_pairs
        )
        query = [
            f"{self.model}.select({selected})" if selected else f"{self.model}.select()"
        ]
        if self.wheres:
            query.append(f".where({', '.join(self.wheres)})")
        for relation in self.includes:
            query.append(f".include({relation})")
        if self.orders:
            query.append(f".order_by({', '.join(self.orders)})")
        if self.limit is not None:
            query.append(f".limit({self.limit})")
        if self.offset is not None:
            query.append(f".offset({self.offset})")
        rendered = "".join(query)
        if self.runner is None:
            return rendered
        if self.runner == "values" and self.row_name is not None:
            pairs = ", ".join(
                f"{key!r}: {self.row_name}.{attribute}"
                for key, attribute in self.projection_pairs
            )
            return f"[{{{pairs}}} for {self.row_name} in await {session}.fetch({rendered})]"
        if self.runner == "values_list" and self.row_name is not None:
            values = ", ".join(
                f"{self.row_name}.{attribute}"
                for _key, attribute in self.projection_pairs
            )
            if len(self.projection_pairs) == 1:
                values += ","
            return f"[({values}) for {self.row_name} in await {session}.fetch({rendered})]"
        if self.runner == "exists":
            # Wreath has no `exists()`; the count is the same round trip.
            return f"await {session}.count({rendered}) > 0"
        if self.runner == "update_where":
            values = ", ".join(self.write_values)
            return f"await {session}.update_where({rendered}, {values})"
        return f"await {session}.{self.runner}({rendered})"


class _QueryRewrite(_EmitterState):
    def _runs_a_query(self, node) -> bool:
        """Whether this body has a `Model.objects.…` chain that *runs*."""
        for inner in ast.walk(node):
            if not (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Attribute)
                and inner.value.attr == "objects"
            ):
                continue
            if not self.django.objects_is_every_row(
                self._seg(inner.value.value), reads_django=self.imports.reads_django
            ):
                continue
            call = self._parents.get(id(inner))
            rule_id = query_rule(
                inner.attr,
                call if isinstance(call, ast.Call) else None,
                chain_tail(inner, self._parents),
                model=self._seg(inner.value.value),
                relations=self.orm_relations,
                columns=self.orm_columns,
                tables=self.orm_tables,
                unique_constraints=self.orm_unique_constraints,
                plain_mappings=plain_filter_mappings(
                    call if isinstance(call, ast.Call) else None, self._parents
                ),
            )
            if rule_id not in _QUERY_TRANSLATED:
                continue
            plan = _QueryPlan(self._seg(inner.value.value))
            steps, _ = self._query_chain(inner)
            if all(plan.step(self, verb, call) for verb, call in steps) and plan.runner:
                return True
        return False

    def visit_Attribute(self, node: ast.Attribute) -> None:
        value = node.value
        self._track_reference(node, self.imports.origin(node))
        if (
            self.opinionated
            and isinstance(value, ast.Name)
            and (self._enclosing_callable_id(node), value.id) in self._http_responses
        ):
            if node.attr == "status_code":
                end = self.buf.end_of(node)
                self.buf._edits.append((end - len("status_code"), end, b"status"))
            elif node.attr == "content":
                end = self.buf.end_of(node)
                self.buf._edits.append((end - len("content"), end, b"body"))
            elif node.attr == "text":
                self._replace_all_of(node, f"{value.id}.body.decode('utf-8')")
        if node.attr == "status_code" and self._test_clients:
            end = self.buf.end_of(node)
            self.buf._edits.append((end - len("status_code"), end, b"status"))
        # Mirrors the analyzer: the verb after `.objects` names the rewrite, and
        # the `.objects` underneath it is claimed so one chain gets one note.
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "objects"
            and node.attr == "__class__"
        ):
            self._claimed_objects.add(id(value))
            self._annotate(value.lineno, "orm.manager_patch")
        elif isinstance(value, ast.Attribute) and value.attr == "objects":
            self._claimed_objects.add(id(value))
            call = self._parents.get(id(node))
            if not self.django.objects_is_every_row(
                self._seg(value.value), reads_django=self.imports.reads_django
            ):
                # A manager's `get_queryset()` is a predicate this line does not
                # show. Rewriting the verb alone would widen the query for
                # exactly the rows somebody meant to hide.
                self._annotate(value.lineno, "foreign.django.query")
                self.generic_visit(node)
                return
            rule_id = query_rule(
                node.attr,
                call if isinstance(call, ast.Call) else None,
                chain_tail(node, self._parents),
                model=self._seg(value.value),
                relations=self.orm_relations,
                columns=self.orm_columns,
                tables=self.orm_tables,
                unique_constraints=self.orm_unique_constraints,
                plain_mappings=plain_filter_mappings(
                    call if isinstance(call, ast.Call) else None, self._parents
                ),
            )
            if not self._rewrite_query(node, rule_id):
                self._annotate(value.lineno, rule_id)
        elif node.attr == "objects" and id(node) not in self._claimed_objects:
            self._annotate(
                node.lineno,
                "orm.manager_value"
                if self.django.objects_is_every_row(
                    self._seg(value), reads_django=self.imports.reads_django
                )
                else "foreign.django.query",
            )
        self.generic_visit(node)

    # -- queries -----------------------------------------------------------------
    def _query_chain(self, head: ast.Attribute):
        """`[(verb, call), …]` for a `Model.objects.…` chain, and its last node."""
        steps: list[tuple[str, ast.Call | None]] = []
        current: ast.AST = head
        verb = head.attr
        outer = self._parents.get(id(head))
        while True:
            call = outer if isinstance(outer, ast.Call) else None
            steps.append((verb, call))
            current = call or current
            following = self._parents.get(id(current))
            if not isinstance(following, ast.Attribute):
                return steps, current
            verb = following.attr
            outer = self._parents.get(id(following))
            current = following

    def _rewrite_query(self, head: ast.Attribute, rule_id: str) -> bool:
        """Turn `Llama.objects.filter(id=x).all()` into a real wreath query.

        Returns whether it happened. Three things have to line up, and when any
        of them does not the chain is left exactly as written and gets its note
        instead — a query that runs against the wrong session, or that quietly
        drops a lookup, is worse than one a person still has to move.

        1. Every verb in the chain has to be one of the handful below.
        2. Every lookup has to be one `LOOKUP_OPERATOR`/`LOOKUP_METHOD` spells.
        3. If the chain *runs* the query rather than just building it, a session
           has to be in scope. Inside a route handler wreath supplies one; the
           enclosing function otherwise has to take one, and that is a change to
           every caller, so it is left to the person making it.
        """
        if not rule_id.startswith("orm.query.") or rule_id not in _QUERY_TRANSLATED:
            return False
        objects = head.value
        if not isinstance(objects, ast.Attribute):
            return False  # not `<Model>.objects.<verb>`
        model = self._seg(objects.value)
        if not model:
            return False
        steps, last = self._query_chain(head)
        plan = _QueryPlan(model)
        for verb, call in steps:
            if not plan.step(self, verb, call):
                return False
        if plan.runner is not None and self._session is None:
            self._session_wanted = True
            return False
        if plan.runner == "get_or_create":
            plan.existing_name = self._fresh_name(f"_existing_{_snake(plan.model)}")
        elif plan.runner in ("values", "values_list"):
            plan.row_name = self._fresh_name("_row")
        target: _Positioned = last if isinstance(last, (ast.expr, ast.stmt)) else head
        awaited = self._parents.get(id(target))
        text = plan.render(self._session)
        if plan.runner is not None and isinstance(awaited, ast.Await):
            target = awaited  # our text carries its own `await`
        elif plan.runner is not None:
            text = f"({text})" if isinstance(awaited, ast.Attribute) else text
        self._replace_all_of(target, text)
        return True

    def _predicate(self, model: str, keyword: ast.keyword) -> str | None:
        """One `filter(**kw)` keyword as a wreath predicate expression."""
        if keyword.arg is None:
            if isinstance(keyword.value, ast.Name):
                self.needs.add("where_fields")
                return f"*where_fields({model}, {keyword.value.id})"
            return None
        column, suffix = split_lookup(keyword.arg)
        if "__" in column:
            if not _resolved_column_path(
                model, column, self.orm_relations, self.orm_columns
            ):
                return None
            column = ".".join(column.split("__"))
        value = self._seg(keyword.value)
        if suffix == "isnull":
            method = _NULL_METHOD.get(
                keyword.value.value if isinstance(keyword.value, ast.Constant) else None
            )
            return None if method is None else f"{model}.{column}.{method}()"
        if suffix in LOOKUP_METHOD:
            method, shape = LOOKUP_METHOD[suffix]
            return f"{model}.{column}.{method}({shape % value})"
        operator = LOOKUP_OPERATOR.get(suffix)
        return None if operator is None else f"{model}.{column} {operator} {value}"

    def _include_expression(self, model: str, path: str) -> str | None:
        """One resolved ``a__b`` relation trail as nested selectin options."""
        current = model
        parts: list[tuple[str, str]] = []
        for relation in path.split("__"):
            target = self.orm_relations.get(current, {}).get(relation)
            if target is None:
                return None
            parts.append((current, relation))
            current = target
        nested = ""
        for owner, relation in reversed(parts):
            suffix = f"({nested})" if nested else "()"
            nested = f"{owner}.{relation}.selectin{suffix}"
        return nested

    def _projection_pair(self, model: str, name: str) -> tuple[str, str] | None:
        """The legacy dictionary key and Wreath column selected for it."""
        if name in self.orm_relations.get(model, {}):
            return name, f"{name}_id"
        if name in self.orm_columns.get(model, set()):
            return name, name
        return None
