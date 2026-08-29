"""Which rewrite a class gets -- the dispatcher over the three model dialects
and the Pydantic DTOs."""

from __future__ import annotations

import ast

from ..analyzer import _base_kind, _plain_graphql_dataclass, pydantic_projection_rule
from .django import _DjangoModels
from .models import _ModelRewrite
from .ormar import _OrmarModels


class _ClassRewrite(_ModelRewrite, _DjangoModels, _OrmarModels):
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.bases and any(
            self.imports.origin(base).endswith("models.Model") for base in node.bases
        ):
            self._rewrite_django_model(node)
            self.generic_visit(node)
            return
        if node.name in self._pydantic_partial_family:
            self._annotate(node.lineno, "pydantic.partial")
            self.generic_visit(node)
            return
        for dec in node.decorator_list:
            decorated = dec.func if isinstance(dec, ast.Call) else dec
            origin = self.imports.origin(decorated)
            if origin.split(".")[-1] == "as_form":
                # translated: whole-model Annotated[Model, Form()] replaces it
                self._delete_decorator(dec)
            elif origin == "strawberry.type" and _plain_graphql_dataclass(self.imports, node):
                self._delete_decorator(dec)
                self.needs_dataclass = True
                indent = self.buf.line_indent(node.lineno)
                self.buf.insert_before_line(node.lineno, f"{indent}@dataclass(kw_only=True)")
        projection_base = any(
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Attribute)
            and base.func.attr == "get_pydantic"
            and pydantic_projection_rule(base.func, self._parents) == "pydantic.get_pydantic_exact"
            for base in node.bases
        )
        if projection_base:
            self.needs_dataclass = True
            indent = self.buf.line_indent(node.lineno)
            self.buf.insert_before_line(node.lineno, f"{indent}@dataclass(kw_only=True)")
        kind = _base_kind(self.imports, node)
        if kind is None and node.name in self.pydantic_models:
            kind = "pydantic"
        elif kind is None and node.name in self.settings_models:
            kind = "settings"
        if kind == "pydantic":
            self._rewrite_pydantic_class(node)
        elif kind == "settings":
            self._rewrite_settings_class(node)
        elif kind == "ormar":
            self._rewrite_ormar_class(node)
        elif node.name in self.orm_mixins:
            self._rewrite_ormar_mixin(node)
        elif kind == "sqlmodel":
            self._note(
                node.lineno,
                "orm.model",
                "this is a SQLModel class and only ormar models are rewritten "
                "automatically. The shape is the same: Mapped[...] annotations "
                "with column(...) for each field",
            )
        self.generic_visit(node)
