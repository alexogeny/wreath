"""Pydantic models and pydantic-settings classes: the class header, its fields,
its validators and its config."""

from __future__ import annotations

import ast

from ..analyzer import (
    _config_extra,
    dataclass_needs_kw_only,
    pydantic_field_rule,
    redundant_literal_validator,
    settings_class_rule,
)
from .state import _EmitterState

# --------------------------------------------------------------------------- module predicates
# `_config_extra`, `_is_field_constraint`, `_is_lifespan`, `_is_false` and
# `_is_true` are imported from the analyzer rather than restated here: the
# emitter must recognize exactly what the analyzer billed, or the report and the
# output disagree about the same line.


def _mutable_factory(value: ast.AST | None) -> str | None:
    if isinstance(value, ast.List):
        return "list"
    if isinstance(value, ast.Dict):
        return "dict"
    if isinstance(value, ast.Set):
        return "set"
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in ("list", "dict", "set")
        and not value.args
    ):
        return value.func.id
    return None


class _ModelRewrite(_EmitterState):
    def _rewrite_settings_class(self, node: ast.ClassDef) -> None:
        """Move BaseSettings onto Environment-bound dataclasses."""
        self.needs_dataclass = True
        indent = self.buf.line_indent(node.lineno)
        self.buf.insert_before_line(node.lineno, f"{indent}@dataclass(kw_only=True)")
        origins = [self.imports.origin(base) for base in node.bases]
        settings_origins = {"pydantic_settings.BaseSettings", "pydantic.BaseSettings"}
        kept = [
            self._seg(base)
            for base, origin in zip(node.bases, origins, strict=True)
            if origin not in settings_origins
        ]
        if node.bases:
            if kept:
                self.buf.replace_span(node.bases[0], node.bases[-1], ", ".join(kept))
            else:
                self._strip_all_bases(node)
            for base, origin in zip(node.bases, origins, strict=True):
                if origin in settings_origins:
                    self._rewritten.add(id(base))
        rule_id = settings_class_rule(self.imports, node)
        self._resolve(node.lineno, rule_id)
        self._rewrite_settings_custom_init(node)
        for statement in node.body:
            if isinstance(statement, ast.ClassDef) and statement.name == "Config":
                self._replace_all_of(statement, "# wreath-port: BaseSettings Config removed")
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                target = (
                    statement.target
                    if isinstance(statement, ast.AnnAssign)
                    else statement.targets[0] if len(statement.targets) == 1 else None
                )
                if isinstance(target, ast.Name) and target.id == "model_config":
                    self._replace_all_of(statement, "# wreath-port: legacy settings config removed")
                    continue
            if not (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
            ):
                continue
            value = statement.value
            if (
                isinstance(value, ast.Call)
                and self.imports.origin(value.func).split(".")[-1] == "Field"
            ):
                alias = next(
                    (kw.value for kw in value.keywords if kw.arg in ("alias", "env")),
                    None,
                )
                if alias is not None:
                    self.needs.update({"Env"})
                    self.needs_annotated = True
                    annotation = self._seg(statement.annotation)
                    self.buf.replace(
                        statement.annotation,
                        f"Annotated[{annotation}, Env({self._seg(alias)})]",
                    )
                self._rewrite_field_marker(statement, value)
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in self.settings_models
                and not value.args
                and not value.keywords
            ):
                self.needs_field = True
                self._replace_all_of(value, f"field(default_factory={value.func.id})")
            else:
                factory = _mutable_factory(value) if value is not None else None
                if factory and value is not None:
                    self.needs_field = True
                    self._replace_all_of(value, f"field(default_factory={factory})")
            self._resolve(statement.lineno, "settings.field_complex")
            self._resolve(statement.lineno, "settings.nested")

    def _rewrite_settings_custom_init(self, node: ast.ClassDef) -> None:
        init = next(
            (
                statement
                for statement in node.body
                if isinstance(statement, ast.FunctionDef) and statement.name == "__init__"
            ),
            None,
        )
        if init is None:
            return
        parameterized = bool(
            init.args.posonlyargs
            or init.args.kwonlyargs
            or len(init.args.args) > 1
            or init.args.vararg
            or init.args.kwarg
        )
        # The call is carried out of the comprehension beside its statement: a
        # list of `ast.Expr` would widen `.value` back to `ast.expr` and lose the
        # `isinstance` the filter already established.
        super_calls = [
            (statement, statement.value)
            for statement in init.body
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "__init__"
            and isinstance(statement.value.func.value, ast.Call)
            and isinstance(statement.value.func.value.func, ast.Name)
            and statement.value.func.value.func.id == "super"
        ]
        if len(super_calls) != 1:
            return
        statement, call = super_calls[0]
        # A body that is *only* a `super().__init__(...)` forward does what the
        # dataclass already does, so the method goes entirely. Two shapes reach
        # here and both were broken:
        #
        #   def __init__(self):           -> renaming it to `__post_init__` and
        #       super().__init__()           deleting the call left a `def` with
        #                                    no body: a syntax error the
        #                                    round-trip guard turned into
        #                                    `EmitError` on a four-line input.
        #
        #   def __init__(self, **kw):     -> kept verbatim, so the ported class
        #       super().__init__(**kw)       called `object.__init__(host=...)`
        #                                    and raised `TypeError` on the first
        #                                    construction, from a module that
        #                                    parsed and compiled clean.
        forwards_only = len(init.body) == 1 and not call.args and (
            not call.keywords
            or all(k.arg is None for k in call.keywords)
            and bool(init.args.kwarg)
        )
        if forwards_only and not (
            init.args.posonlyargs or init.args.kwonlyargs
            or len(init.args.args) > 1 or init.args.vararg
        ):
            self._replace_all_of(init, "")
            return
        if not parameterized and not call.args and not call.keywords:
            header_end = self.buf.b.find(
                b":", self.buf.start_of(init), self.buf.start_of(init.body[0])
            )
            name_at = self.buf.b.find(b"__init__", self.buf.start_of(init), header_end)
            if name_at >= 0:
                self.buf._edits.append((name_at, name_at + len("__init__"), b"__post_init__"))
                self._replace_all_of(statement, "")
            return
        if call.args or any(keyword.arg is None for keyword in call.keywords):
            return
        supplied = {keyword.arg: self._seg(keyword.value) for keyword in call.keywords}
        assignments: list[str] = []
        for field_statement in node.body:
            if not (
                isinstance(field_statement, ast.AnnAssign)
                and isinstance(field_statement.target, ast.Name)
            ):
                continue
            name = field_statement.target.id
            value = supplied.get(name)
            if value is None and field_statement.value is not None:
                value = self._seg(field_statement.value)
            if value is not None:
                assignments.append(f"self.{name} = {value}")
        if assignments:
            indent = self.buf.line_indent(statement.lineno)
            self._replace_all_of(
                statement,
                ("\n" + indent).join(assignments),
            )

    def _rewrite_pydantic_class(self, node: ast.ClassDef) -> None:
        self.needs_dataclass = True
        indent = self.buf.line_indent(node.lineno)
        # Pydantic ignores what order the fields are written in; a dataclass
        # does not, and refuses to be built at all if a required field comes
        # after one with a default. `kw_only=True` removes the ordering rule,
        # and costs nothing at the boundary because wreath builds a body model
        # by keyword (`binding._validate_dataclass` calls `cls(**kwargs)`).
        offenders = dataclass_needs_kw_only(self.imports, node)
        header = "@dataclass(kw_only=True)" if offenders else "@dataclass"
        self.buf.insert_before_line(node.lineno, f"{indent}{header}")
        # Strip Pydantic's runtime bases while preserving ordinary mixins and
        # generic bases. PartialModelMixin itself disappears; a
        # ``model_as_partial()`` base becomes a local stdlib-dataclass helper.
        base_origins = [self.imports.origin(b) for b in node.bases]
        kept: list[str] = []
        for base, origin in zip(node.bases, base_origins, strict=True):
            if origin in {"pydantic.BaseModel", "pydantic_partial.PartialModelMixin"}:
                self._rewritten.update(id(item) for item in ast.walk(base))
                continue
            if (
                isinstance(base, ast.Call)
                and isinstance(base.func, ast.Attribute)
                and base.func.attr == "model_as_partial"
                and not base.args
                and not base.keywords
            ):
                kept.append(self._seg(base))
                continue
            kept.append(self._seg(base))
        if node.bases:
            if kept:
                self.buf.replace_span(node.bases[0], node.bases[-1], ", ".join(kept))
            else:
                self._strip_all_bases(node)
        self._resolve(node.lineno, "pydantic.model")
        for stmt in node.body:
            self._rewrite_pydantic_field(stmt)
        self._rewrite_pydantic_validators(node)

    def _rewrite_pydantic_validators(self, node: ast.ClassDef) -> None:
        """Run ordinary field validators from a dataclass ``__post_init__``."""
        calls: list[str] = []
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if redundant_literal_validator(statement, self._parents, self.imports):
                continue
            marker = next(
                (
                    decorator
                    for decorator in statement.decorator_list
                    if isinstance(decorator, ast.Call)
                    and self.imports.origin(decorator.func).split(".")[-1]
                    in {"validator", "field_validator"}
                ),
                None,
            )
            if marker is None or isinstance(statement, ast.AsyncFunctionDef):
                continue
            fields = [
                argument.value
                for argument in marker.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
            if not fields or len(fields) != len(marker.args):
                continue
            self._delete_decorator(marker)
            for decorator in statement.decorator_list:
                decorated = decorator.func if isinstance(decorator, ast.Call) else decorator
                if (
                    isinstance(decorated, ast.Name) and decorated.id == "classmethod"
                ) or self.imports.origin(decorated) == "builtins.classmethod":
                    self._delete_decorator(decorator)
            self._removed_pydantic_imports.update({"validator", "field_validator"})
            self._resolve(marker.lineno, "pydantic.validator")
            calls.extend(
                f"self.{field} = self.{statement.name}(self.{field})" for field in fields
            )
        if not calls:
            return
        existing = next(
            (
                statement
                for statement in node.body
                if isinstance(statement, ast.FunctionDef)
                and statement.name == "__post_init__"
            ),
            None,
        )
        if existing is not None:
            first = existing.body[0]
            indent = self.buf.line_indent(first.lineno)
            self.buf.insert_before_line(
                first.lineno, "\n".join(f"{indent}{call}" for call in calls)
            )
            return
        indent = self.buf.line_indent(node.lineno) + "    "
        end = self.buf.end_of(node.body[-1])
        body = "\n".join(f"{indent}    {call}" for call in calls)
        self.buf._edits.append(
            (end, end, f"\n\n{indent}def __post_init__(self) -> None:\n{body}".encode())
        )

    def _strip_all_bases(self, node: ast.ClassDef) -> None:
        """Remove the whole `(...)` base list — correct only when it is the sole base."""
        b = self.buf.b
        pstart = self.buf.start_of(node.bases[0])
        pend = self.buf.end_of(node.bases[-1])
        open_i = b.rfind(b"(", 0, pstart)
        close_i = b.find(b")", pend)
        if open_i != -1 and close_i != -1:
            self.buf._edits.append((open_i, close_i + 1, b""))
            # The `BaseModel` mention went with the parentheses, so it must not
            # also keep its import alive.
            self._rewritten.add(id(node.bases[0]))

    def _rewrite_pydantic_field(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            declarative = all(
                isinstance(child, ast.Assign)
                or (
                    isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)
                )
                for child in stmt.body
            )
            settings = {
                child.targets[0].id
                for child in stmt.body
                if isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
            }
            if declarative and settings and settings <= {
                "arbitrary_types_allowed",
                "from_attributes",
                "orm_mode",
                "protected_namespaces",
                "use_enum_values",
                "validate_default",
            }:
                self._replace_all_of(stmt, "# wreath-port: redundant model config removed")
                self._resolve(stmt.lineno, "pydantic.config_class")
            else:
                self._annotate(stmt.lineno, "pydantic.config_class")
            return
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name == "model_config":
                extra = _config_extra(stmt.value)
                if extra == "forbid":
                    # `replace` spans from the node's own col_offset, so the
                    # source indentation in front of it is left alone.
                    self.buf.replace(
                        stmt, "# wreath-port: extra='forbid' is wreath's default (dropped)"
                    )
                elif extra == "ignore" and self.opinionated:
                    self._resolve(stmt.lineno, "pydantic.config_ignore")
                    self.buf.replace(
                        stmt,
                        "# wreath-port: extra='ignore' dropped -- wreath rejects unknown "
                        "fields with a 422. Clients sending extras will start failing.",
                    )
                elif extra == "ignore":
                    self._annotate(stmt.lineno, "pydantic.config_ignore")
                elif self._drop_redundant_model_config(stmt, stmt.value):
                    pass
                return
            rule_id = pydantic_field_rule(self.imports, stmt)
            if (
                rule_id == "pydantic.field_metadata_exact"
                and isinstance(stmt.value, ast.Call)
            ):
                self._rewrite_field_metadata(stmt, stmt.value)
                return
            if rule_id != "pydantic.field":
                # Metadata without an exact Wreath equivalent stays written so
                # the reviewer can see the contract they are deciding about.
                self._annotate(stmt.lineno, rule_id)
                return
            default = stmt.value
            if (
                isinstance(default, ast.Call)
                and self.imports.origin(default.func).split(".")[-1] == "Field"
            ):
                self._rewrite_field_marker(stmt, default)
                return
            # mutable literal defaults -> field(default_factory=...)
            factory = _mutable_factory(default) if default is not None else None
            if factory and default is not None:
                self.needs_field = True
                self.buf.replace(default, f"field(default_factory={factory})")
        elif isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
        ):
            extra = _config_extra(stmt.value)
            if extra == "forbid":
                self.buf.replace(
                    stmt, "# wreath-port: extra='forbid' is wreath's default (dropped)"
                )
            elif extra == "ignore" and self.opinionated:
                self._resolve(stmt.lineno, "pydantic.config_ignore")
                self.buf.replace(
                    stmt,
                    "# wreath-port: extra='ignore' dropped -- wreath rejects unknown "
                    "fields with a 422. Clients sending extras will start failing.",
                )
            elif extra == "ignore":
                self._annotate(stmt.lineno, "pydantic.config_ignore")
            else:
                self._drop_redundant_model_config(stmt, stmt.value)

    def _drop_redundant_model_config(
        self, stmt: ast.stmt, value: ast.expr | None
    ) -> bool:
        # `None` because an `AnnAssign` may carry no value at all (`x: int`);
        # it takes the same answer as any other non-call.
        if not isinstance(value, ast.Call):
            return False
        safe = {
            "arbitrary_types_allowed",
            "from_attributes",
            "protected_namespaces",
            "use_enum_values",
            "validate_default",
        }
        names = {keyword.arg for keyword in value.keywords}
        if None in names or not names or not names <= safe:
            return False
        self._removed_pydantic_imports.add("ConfigDict")
        self._replace_all_of(stmt, "# wreath-port: redundant model config removed")
        return True

    def _rewrite_field_metadata(self, stmt: ast.AnnAssign, call: ast.Call) -> None:
        """Move an exact Pydantic ``Field`` contract into ``Annotated``."""
        metadata: list[str] = []
        for keyword in call.keywords:
            if keyword.arg in {
                "alias", "description", "gt", "ge", "lt", "le",
                "min_length", "max_length", "pattern", "regex",
            }:
                name = "pattern" if keyword.arg == "regex" else keyword.arg
                metadata.append(f"{name}={self._seg(keyword.value)}")
        self.needs.add("Field")
        self.needs_annotated = True
        annotation = self._seg(stmt.annotation)
        self.buf.replace(
            stmt.annotation,
            f"Annotated[{annotation}, Field({', '.join(metadata)})]",
        )
        self._rewrite_field_marker(stmt, call)

    def _rewrite_field_marker(self, stmt: ast.AnnAssign, call: ast.Call) -> None:
        """`x: int = Field(default=3, description="…")` -> `x: int = 3`.

        Reached only for the shape `pydantic_field_rule` calls determined: the
        marker holds a default and, at most, metadata already moved into
        ``Annotated``. A dataclass has one slot per field, so the default is
        written into it.

        `Field(...)` with no default at all is how pydantic spells *required*,
        and a dataclass spells that by having no `=` — so the whole assignment
        goes. Removing a default can put a required field after a defaulted one,
        which is exactly what `_rewrite_pydantic_class` already checked for
        before it chose between `@dataclass` and `@dataclass(kw_only=True)`.
        """
        self._rewritten.add(id(call.func))
        by_name = {kw.arg: kw.value for kw in call.keywords}
        factory = by_name.get("default_factory")
        if factory is not None:
            self.needs_field = True
            self.buf.replace(call, f"field(default_factory={self._seg(factory)})")
            return
        default = by_name.get("default")
        if default is None and call.args:
            positional = call.args[0]
            if not (isinstance(positional, ast.Constant) and positional.value is Ellipsis):
                default = positional
        if default is None:
            # Required: drop " = Field(...)" and leave a bare annotation.
            self.buf._edits.append((self.buf.end_of(stmt.annotation), self.buf.end_of(call), b""))
            return
        mutable = _mutable_factory(default)
        if mutable:
            self.needs_field = True
            self.buf.replace(call, f"field(default_factory={mutable})")
        else:
            self.buf.replace(call, self._seg(default))

    def _rewrite_model_dataclass(self, node: ast.Call, func: ast.Attribute) -> None:
        """Replace a statically named ormar projection with Wreath's dataclass."""
        outer = self._parents.get(id(node))
        if isinstance(outer, ast.ClassDef):
            projected_name = f"_{outer.name}Fields"
        elif (
            isinstance(outer, ast.Assign)
            and len(outer.targets) == 1
            and isinstance(outer.targets[0], ast.Name)
        ):
            projected_name = outer.targets[0].id
        elif isinstance(outer, ast.AnnAssign) and isinstance(outer.target, ast.Name):
            projected_name = outer.target.id
        else:
            projected_name = None
        arguments = [self._seg(func.value)]
        arguments.extend(
            f"{keyword.arg}={self._seg(keyword.value)}" for keyword in node.keywords
        )
        if projected_name is not None:
            arguments.append(f"name={projected_name!r}")
        self.needs.add("model_dataclass")
        self._rewritten.add(id(func))
        self._replace_all_of(node, f"model_dataclass({', '.join(arguments)})")
