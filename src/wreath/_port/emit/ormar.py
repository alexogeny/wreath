"""ormar and SQLModel models: the class header, its `Meta`/`ormar_config`, its
columns and its foreign keys."""

from __future__ import annotations

import ast

from ..analyzer import _is_true
from .state import _EmitterState
from .targets import _ORMAR_DOC_KWARGS, _ORMAR_TYPE, _PG_PYANN, _SA_ELEM_TYPE


def _config_constraints(value: ast.AST | None) -> list[ast.Call]:
    """The `constraints=[...]` entries of an `ormar_config = base.copy(...)`."""
    if not isinstance(value, ast.Call):
        return []
    for kw in value.keywords:
        if kw.arg == "constraints" and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [entry for entry in kw.value.elts if isinstance(entry, ast.Call)]
    return []


def _copy_tablename(value: ast.AST | None) -> str | None:
    """Pull the tablename out of `base_ormar_config.copy(tablename="x")`."""
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg == "tablename" and isinstance(kw.value, ast.Constant):
                # Only a string literal is a usable table name; anything else
                # leaves the caller to emit its "add table=<name> by hand" note.
                table = kw.value.value
                return table if isinstance(table, str) else None
    return None


class _OrmarModels(_EmitterState):
    def _rewrite_ormar_class(self, node: ast.ClassDef) -> None:
        self.needs.add("Model")
        table, config_stmt = None, None
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ormar_config" for t in stmt.targets
            ):
                config_stmt, table = stmt, _copy_tablename(stmt.value)
        meta = next(
            (
                statement
                for statement in node.body
                if isinstance(statement, ast.ClassDef) and statement.name == "Meta"
            ),
            None,
        )
        if table is None and meta is not None:
            table = next(
                (
                    statement.value.value
                    for statement in meta.body
                    if isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "tablename"
                        for target in statement.targets
                    )
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ),
                None,
            )
        inherited_mixins = [
            base
            for base in node.bases
            if self.imports.origin(base).split(".")[-1] in self.orm_mixins
        ]
        bases = [
            self._seg(base) for base in node.bases if self.imports.origin(base) != "ormar.Model"
        ]
        if not inherited_mixins:
            bases.insert(0, "Model")
        if node.bases:
            header = ", ".join(bases)
            if table is not None:
                header += f', table="{table}"'
            self.buf.replace_span(node.bases[0], node.bases[-1], header)
            for base in node.bases:
                if self.imports.origin(base) == "ormar.Model":
                    self._rewritten.add(id(base))
        if table is None:
            self._note(
                node.lineno,
                "orm.model",
                'add `table="<name>"` to the class header; the table name '
                "was not written here as plain text",
            )
        if config_stmt is not None:
            self._rewrite_ormar_config(node, config_stmt)
        if meta is not None:
            self._rewrite_legacy_ormar_meta(meta)
        self._rewrite_ormar_fields(node, config_stmt)

    def _rewrite_legacy_ormar_meta(self, meta: ast.ClassDef) -> None:
        """Move legacy ``Meta`` indexes and unique constraints into the model."""
        declarations: list[str] = []
        for statement in meta.body:
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                continue
            name = statement.targets[0].id
            if name == "indexes" and isinstance(statement.value, (ast.List, ast.Tuple)):
                for value in statement.value.elts:
                    columns = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
                    if not all(
                        isinstance(column, ast.Constant) and isinstance(column.value, str)
                        for column in columns
                    ):
                        continue
                    self.needs.add("index")
                    args = ", ".join(self._seg(column) for column in columns)
                    declarations.append(f"_index_{len(declarations)} = index({args})")
            elif name == "constraints" and isinstance(statement.value, (ast.List, ast.Tuple)):
                for value in statement.value.elts:
                    if not isinstance(value, ast.Call):
                        continue
                    kind = self.imports.origin(value.func).split(".")[-1]
                    if kind not in ("UniqueColumns", "IndexColumns") or not all(
                        isinstance(column, ast.Constant) and isinstance(column.value, str)
                        for column in value.args
                    ):
                        continue
                    target = "unique" if kind == "UniqueColumns" else "index"
                    self.needs.add(target)
                    args = ", ".join(self._seg(column) for column in value.args)
                    declarations.append(f"_{target}_{len(declarations)} = {target}({args})")
        indent = self.buf.line_indent(meta.lineno)
        replacement = (
            f"\n{indent}".join(declarations)
            if declarations
            else "# wreath-port: legacy Ormar Meta moved to the class header"
        )
        self._replace_all_of(meta, replacement)

    def _rewrite_ormar_mixin(self, node: ast.ClassDef) -> None:
        """Turn a plain Ormar column mixin into a table-less Wreath model."""
        self.needs.add("Model")
        if node.bases:
            end = self.buf.end_of(node.bases[-1])
            self.buf._edits.append((end, end, b", Model"))
        else:
            name_end = self.buf.start_of(node) + len(f"class {node.name}".encode())
            self.buf._edits.append((name_end, name_end, b"(Model)"))
        self._rewrite_ormar_fields(node, None)

    def _rewrite_ormar_fields(self, node: ast.ClassDef, config_stmt: ast.Assign | None) -> None:
        """Rewrite the column declarations shared by tables and mixins."""
        # Paired rather than filtered: `AnnAssign.value` is Optional, and a list
        # of statements throws away the narrowing every reader below relies on.
        columns: list[tuple[ast.AnnAssign, ast.Call]] = [
            (stmt, stmt.value)
            for stmt in node.body
            if stmt is not config_stmt
            and isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.value, ast.Call)
        ]
        # The nullability reminder used to go on every model, whether or not the
        # model left anything unsaid. It now goes only on models with a column
        # that states neither `nullable=` nor `primary_key=` — the ones where the
        # answer really did change, because ormar defaults a column to nullable
        # and wreath defaults it to NOT NULL.
        unstated = [
            stmt.target.id
            for stmt, call in columns
            if isinstance(stmt.target, ast.Name)
            and not any(kw.arg in ("nullable", "primary_key") for kw in call.keywords)
            and self.imports.origin(call.func).split(".")[-1] != "ForeignKey"
        ]
        if unstated:
            self._note(
                node.lineno,
                "orm.column",
                "check whether these columns should allow NULL. ormar let a column be "
                "empty unless told otherwise; wreath requires a value unless told "
                "otherwise, so the ones that said nothing have just changed meaning: "
                + ", ".join(unstated),
            )
        for stmt, call in columns:
            self._rewrite_ormar_column(stmt, call)
        plain_columns: list[tuple[ast.Assign, ast.Call]] = [
            (stmt, stmt.value)
            for stmt in node.body
            if stmt is not config_stmt
            and isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
            and self.imports.origin(stmt.value.func).startswith("ormar.")
        ]
        for stmt, call in plain_columns:
            self._rewrite_unannotated_ormar_column(stmt, call)

    def _rewrite_ormar_config(self, node: ast.ClassDef, config_stmt: ast.Assign) -> None:
        """Delete `ormar_config = …`, but not the constraints hanging off it.

        `constraints=[ormar.UniqueColumns("name", "ranch")]` is a real UNIQUE
        index in the database, and the whole statement used to be deleted with
        no note at all — the port came out looking complete and quietly dropped
        31 constraints. Each one becomes a wreath declaration of the same shape.
        """
        indent = self.buf.line_indent(config_stmt.lineno)
        lines: list[str] = []
        unread: list[str] = []
        for entry in _config_constraints(config_stmt.value):
            kind = self.imports.origin(entry.func).split(".")[-1]
            columns = [
                arg
                for arg in entry.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if kind not in ("UniqueColumns", "IndexColumns") or len(columns) != len(entry.args):
                unread.append(f"{kind} on line {entry.lineno}")
                continue
            call = "unique" if kind == "UniqueColumns" else "index"
            self.needs.add(call)
            names = ", ".join(self._seg(column) for column in columns)
            lines.append(f"_{call}_{len(lines)} = {call}({names})")
        # `replace` starts at the statement's own column, so the first line keeps
        # the indentation already in the source and the rest supply their own.
        self._replace_all_of(config_stmt, f"\n{indent}".join(lines))
        if unread:
            self._note(
                config_stmt.lineno,
                "orm.model",
                "this table declared constraints that were not carried over ("
                + "; ".join(unread)
                + "). They exist in the database today, so "
                "add the matching `unique(...)` or `index(...)` to the model",
            )

    def _rewrite_ormar_column(self, stmt: ast.AnnAssign, call: ast.Call) -> None:
        tail = self.imports.origin(call.func).split(".")[-1]
        ann_src = self._seg(stmt.annotation)
        if tail == "ForeignKey":
            self._rewrite_ormar_fk(stmt, call)
            return
        pgtype = self._ormar_pgtype(tail, call)
        if pgtype is None:
            self._note(
                stmt.lineno,
                "orm.column",
                f"wreath has no column type matching ormar.{tail}; pick the "
                "closest one in wreath.orm.types and check the values still fit",
            )
            return
        self.needs.update({"Mapped", "column"})
        kwargs = self._ormar_kwargs(stmt, call)
        self.buf.replace(stmt.annotation, f"Mapped[{ann_src}]")
        args = "" if not kwargs else ", " + ", ".join(kwargs)
        self._replace_all_of(call, f"column({pgtype}{args})")

    def _rewrite_unannotated_ormar_column(self, stmt: ast.Assign, call: ast.Call) -> None:
        """Move an old-style mixin column whose Python type was implicit."""
        tail = self.imports.origin(call.func).split(".")[-1]
        if tail == "ForeignKey":
            # The same key as `ranch: Ranch = ormar.ForeignKey(Ranch)`: the
            # annotation is where the *referenced* model would be named, and the
            # rewrite reads it off the call instead. Without this the key fell
            # through to the column path, which asked the type table for
            # "ForeignKey", got nothing, and wrote a `[translated]` note saying
            # wreath has no type for it -- above a line left exactly as it was.
            self._rewrite_ormar_fk(stmt, call)
            return
        pgtype = self._ormar_pgtype(tail, call)
        if pgtype is None:
            self._note(
                stmt.lineno,
                "orm.column",
                f"wreath has no column type matching ormar.{tail}; pick the "
                "closest one in wreath.orm.types and check the values still fit",
            )
            return
        self.needs.add("column")
        kwargs = self._ormar_kwargs(stmt, call)
        args = "" if not kwargs else ", " + ", ".join(kwargs)
        self._replace_all_of(call, f"column({pgtype}{args})")

    def _rewrite_ormar_fk(self, stmt: ast.AnnAssign | ast.Assign, call: ast.Call) -> None:
        target = stmt.target if isinstance(stmt, ast.AnnAssign) else stmt.targets[0]
        if not isinstance(target, ast.Name) or not call.args:
            self._annotate(stmt.lineno, "orm.fk")
            return
        name = target.id
        arg0 = call.args[0]
        target = self._seg(arg0)
        target_name = None
        if isinstance(arg0, ast.Name):
            target_name = arg0.id
        elif isinstance(arg0, ast.Attribute):
            target_name = arg0.attr
        idx = any(kw.arg == "index" and _is_true(kw.value) for kw in call.keywords)
        indent = self.buf.line_indent(stmt.lineno)
        pg = self.pk_types.get(target_name)
        if pg is not None:  # translated: PK type resolved from the referenced model
            pyann = _PG_PYANN.get(pg, "int")
            self.needs_uuid = self.needs_uuid or pyann.startswith("uuid.")
            self.needs.update({"Mapped", "column", "relationship", pg})
            index = ", index=True" if idx else ""
            col = f"{name}_id: Mapped[{pyann}] = column({pg}, references={target}.id{index})"
            self._replace_all_of(
                stmt, f'{col}\n{indent}{name} = relationship({target}, load="raise")'
            )
        else:  # needs-review: referenced PK not resolvable in this module -> Uuid default + flag
            self.needs.update({"Mapped", "column", "relationship", "Uuid"})
            index = ", index=True" if idx else ""
            self.needs_uuid = True
            col = f"{name}_id: Mapped[uuid.UUID] = column(Uuid, references={target}.id{index})"
            self._replace_all_of(
                stmt, f'{col}\n{indent}{name} = relationship({target}, load="raise")'
            )
            self._annotate(stmt.lineno, "orm.fk")

    def _ormar_pgtype(self, tail: str, call: ast.Call) -> str | None:
        if tail == "ARRAY":
            elem = "Text"
            for kw in call.keywords:
                if kw.arg == "item_type":
                    v = kw.value.func if isinstance(kw.value, ast.Call) else kw.value
                    elem = _SA_ELEM_TYPE.get(self.imports.origin(v).split(".")[-1], "Text")
            self.needs.update({"Array", elem})
            return f"Array({elem})"
        if tail == "DateTime":
            name = (
                "TimestampTz"
                if any(kw.arg == "timezone" and _is_true(kw.value) for kw in call.keywords)
                else "Timestamp"
            )
            self.needs.add(name)
            return name
        name = _ORMAR_TYPE.get(tail)
        if name:
            self.needs.add(name)
        return name

    def _ormar_kwargs(self, stmt: ast.AnnAssign | ast.Assign, call: ast.Call) -> list[str]:
        """The `column(...)` arguments one ormar column's keywords become.

        Anything with a wreath home is carried; anything that only described the
        column for a schema is dropped without a word; and whatever is left over
        earns one note that names it. That last list used to include
        `max_length=` and `description=`, which between them accounted for 366
        of the 632 notes this emitter wrote — one on nearly every column in the
        tree, for two keywords that need no decision at all.
        """
        out: list[str] = []
        dropped: list[str] = []
        checks: list[str] = []
        minimum = maximum = min_length = max_length = None
        for kw in call.keywords:
            if kw.arg in (
                "primary_key",
                "nullable",
                "unique",
                "index",
                "server_default",
                "default",
            ):
                out.append(f"{kw.arg}={self._seg(kw.value)}")
            elif kw.arg == "default_factory":
                out.append(f"default={self._seg(kw.value)}")
            elif kw.arg == "minimum":
                minimum = self._seg(kw.value)
            elif kw.arg == "maximum":
                maximum = self._seg(kw.value)
            elif kw.arg == "min_length":
                min_length = self._seg(kw.value)
            elif kw.arg == "max_length":
                # ormar's `max_length` sizes a VARCHAR; wreath spells the same
                # limit as a check, which holds it in the database and returns a
                # 422 at the boundary rather than truncating.
                max_length = self._seg(kw.value)
            elif kw.arg in _ORMAR_DOC_KWARGS or kw.arg in ("timezone", "item_type"):
                continue
            elif kw.arg == "uuid_format":
                # ormar can store a UUID as text; wreath's `Uuid` is the native
                # postgres type. Same values, different storage — which matters
                # only if this port points at a database that already exists.
                self._note(
                    stmt.lineno,
                    "orm.column",
                    "ormar stored this UUID as text, and wreath stores it as postgres's "
                    "own uuid type. Starting from an empty database there is nothing to "
                    "do. Pointing at an existing one, this column has to be converted "
                    "before the model matches it",
                )
            else:
                dropped.append(kw.arg or "**kwargs")
        if minimum is not None and maximum is not None:
            self.needs.update({"AllOf", "Ge", "Le"})
            checks.append(f"AllOf((Ge({minimum}), Le({maximum})))")
        elif minimum is not None:
            self.needs.add("Ge")
            checks.append(f"Ge({minimum})")
        elif maximum is not None:
            self.needs.add("Le")
            checks.append(f"Le({maximum})")
        if min_length is not None or max_length is not None:
            self.needs.add("Length")
            bounds = ", ".join(
                f"{name}={value}"
                for name, value in (("minimum", min_length), ("maximum", max_length))
                if value is not None
            )
            checks.append(f"Length({bounds})")
        if len(checks) == 1:
            out.append(f"check={checks[0]}")
        elif checks:
            self.needs.add("AllOf")
            out.append(f"check=AllOf(({', '.join(checks)}))")
        if dropped:
            self._note(
                stmt.lineno,
                "orm.column",
                "this column set "
                + ", ".join(f"{name}=" for name in dropped)
                + ", which wreath's column() has no setting for. Decide what each one "
                "should become before relying on this model",
            )
        return out
