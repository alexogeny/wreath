"""Django models: `models.Model` classes and their fields, into `wreath.orm`
columns.

The `models.*` field tables live in `.._port.foreign`, which owns Django
detection, so that this module and the foreign-framework report cannot
disagree about what a field is -- and so neither has to import the other."""

from __future__ import annotations

import ast

from ..analyzer import _is_true
from ..foreign import _DJANGO_DOC_KWARGS, _DJANGO_TYPE
from .state import _EmitterState
from .targets import _DJANGO_PYANN, _PG_PYANN


class _DjangoModels(_EmitterState):
    # -- Django models ---------------------------------------------------------

    def _django_pyann(self, pgtype: str, nullable: bool) -> str:
        base = _DJANGO_PYANN.get(pgtype, "str")
        if base.startswith("uuid."):
            self.needs_uuid = True
        if base.startswith("datetime."):
            self.needs_datetime = True
        if base.startswith("decimal."):
            self.needs_decimal = True
        return f"{base} | None" if nullable else base

    def _django_kwargs(self, stmt: ast.stmt, call: ast.Call) -> tuple[list[str], bool]:
        """`column(...)` arguments one Django field's keywords become.

        Same three-way split the ormar path uses: carried, dropped in silence,
        or one note naming what was left over. `null=` is returned separately
        because it decides the Python annotation as well as the column.
        """
        out: list[str] = []
        dropped: list[str] = []
        nullable = False
        max_length = None
        for kw in call.keywords:
            if kw.arg == "null":
                nullable = _is_true(kw.value)
                if nullable:
                    out.append("nullable=True")
            elif kw.arg in ("primary_key", "unique"):
                if _is_true(kw.value):
                    out.append(f"{kw.arg}=True")
            elif kw.arg == "db_index":
                if _is_true(kw.value):
                    out.append("index=True")
            elif kw.arg == "default":
                out.append(f"default={self._seg(kw.value)}")
            elif kw.arg == "max_length":
                max_length = self._seg(kw.value)
            elif kw.arg == "auto_now_add":
                # A database-side default is the same guarantee, and it survives
                # a write that did not come through the ORM.
                if _is_true(kw.value):
                    out.append('server_default="now()"')
            elif kw.arg == "on_delete":
                action = self._seg(kw.value).split(".")[-1].upper()
                out.append(f'on_delete="{action}"')
            elif kw.arg in _DJANGO_DOC_KWARGS:
                continue
            elif kw.arg in (
                "max_digits",
                "decimal_places",
                "auto_now",
                "db_column",
                "unique_for_date",
            ):
                dropped.append(kw.arg)
            else:
                dropped.append(kw.arg or "**kwargs")
        if max_length is not None:
            self.needs.add("Length")
            out.append(f"check=Length(maximum={max_length})")
        if dropped:
            self._note(
                stmt.lineno,
                "orm.django.column",
                "this field set "
                + ", ".join(f"{name}=" for name in dropped)
                + ", which wreath's column() has no setting for. Decide what each one "
                "should become before relying on this model",
            )
        return out, nullable

    def _rewrite_django_column(self, stmt: ast.Assign, call: ast.Call) -> None:
        tail = self.imports.origin(call.func).split(".")[-1]
        pgtype = _DJANGO_TYPE.get(tail)
        if pgtype is None or not isinstance(stmt.targets[0], ast.Name):
            self._annotate(stmt.lineno, "orm.django.column_unmapped")
            return
        kwargs, nullable = self._django_kwargs(stmt, call)
        self.needs.update({"Mapped", "column", pgtype})
        args = "" if not kwargs else ", " + ", ".join(kwargs)
        # Django writes `name = models.CharField(...)` with no annotation, where
        # ormar writes an annotated one -- so this inserts the annotation rather
        # than replacing it.
        target = stmt.targets[0]
        end = self.buf.end_of(target)
        ann = self._django_pyann(pgtype, nullable)
        self.buf._edits.append((end, end, f": Mapped[{ann}]".encode()))
        self._replace_all_of(call, f"column({pgtype}{args})")
        self._resolve(stmt.lineno, "orm.django.column")

    def _rewrite_django_fk(self, stmt: ast.Assign, call: ast.Call) -> None:
        if not isinstance(stmt.targets[0], ast.Name) or not call.args:
            self._annotate(stmt.lineno, "orm.django.fk")
            return
        name = stmt.targets[0].id
        arg0 = call.args[0]
        target_name = arg0.id if isinstance(arg0, ast.Name) else (
            arg0.attr if isinstance(arg0, ast.Attribute) else None
        )
        if target_name is None or isinstance(arg0, ast.Constant):
            # A string reference ("app.Model") names a class this pass cannot
            # resolve, so the referenced primary key type is unknown.
            self._annotate(stmt.lineno, "orm.django.fk")
            return
        pg = self.pk_types.get(target_name, "Int64")
        pyann = _PG_PYANN.get(pg, "int")
        if pyann.startswith("uuid."):
            self.needs_uuid = True
        kwargs, nullable = self._django_kwargs(stmt, call)
        self.needs.update({"Mapped", "column", pg})
        extra = "".join(f", {kw}" for kw in kwargs)
        ann = f"{pyann} | None" if nullable else pyann
        unique = ", unique=True" if self.imports.origin(call.func).endswith("OneToOneField") else ""
        self._replace_all_of(
            stmt,
            f"{name}_id: Mapped[{ann}] = column({pg}, references={target_name}.id{unique}{extra})",
        )
        self._resolve(stmt.lineno, "orm.django.fk")

    def _rewrite_django_model(self, node: ast.ClassDef) -> None:
        """`class X(models.Model)` -> `class X(Model, table="...")`."""
        table = None
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
                for child in stmt.body:
                    if (
                        isinstance(child, ast.Assign)
                        and any(
                            isinstance(target, ast.Name) and target.id == "db_table"
                            for target in child.targets
                        )
                        and isinstance(child.value, ast.Constant)
                    ):
                        table = child.value.value
        self.needs.add("Model")
        header = "Model" if table is None else f'Model, table="{table}"'
        self.buf.replace_span(node.bases[0], node.bases[-1], header)
        for base in node.bases:
            # The whole base expression is gone, `models.` included -- marking
            # only the outer node left the `models` inside `models.Model` looking
            # like a live reference, which is enough to keep the django import.
            self._rewritten.update(id(item) for item in ast.walk(base))
        if table is None:
            self._note(
                node.lineno,
                "orm.django.model",
                'add `table="<name>"` to the class header; Django derived it from '
                "the app label and the class name, and wreath does not",
            )
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta" and table is not None:
                self._replace_all_of(
                    stmt,
                    "# wreath-port: Meta.db_table is now table= on the class",
                )
                continue
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                continue
            tail = self.imports.origin(stmt.value.func).split(".")[-1]
            if tail in ("ForeignKey", "OneToOneField"):
                self._rewrite_django_fk(stmt, stmt.value)
            elif tail == "ManyToManyField":
                self._annotate(stmt.lineno, "orm.django.m2m")
            elif tail in _DJANGO_TYPE:
                self._rewrite_django_column(stmt, stmt.value)
                self._rewritten.add(id(stmt.value.func))
            elif tail.endswith("Field"):
                self._annotate(stmt.lineno, "orm.django.column_unmapped")
        self._resolve(node.lineno, "orm.django.model")
