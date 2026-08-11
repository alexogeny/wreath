"""Where a name came from. One pass over a module's imports, so every later
question about a node is asked of its *origin* rather than its spelling."""

from __future__ import annotations

import ast


class _Imports:
    """Resolves local names to their dotted framework origin (honors `as`)."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.has_star = False

    def visit(self, tree: ast.AST) -> _Imports:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.names[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        self.has_star = True
                        continue
                    qualified = f"{mod}.{alias.name}" if mod else alias.name
                    self.names[alias.asname or alias.name] = qualified
        return self

    def origin(self, node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return self.names.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value)
            return f"{base}.{node.attr}"
        return ""

    @property
    def roots(self) -> frozenset[str]:
        """The top-level packages this module imports."""
        return frozenset(dotted.split(".")[0] for dotted in self.names.values())

    @property
    def serves_asgi(self) -> bool:
        """Does this module import the framework whose spellings we translate?

        `@app.get("/x")` is written identically in FastAPI, Bottle and Sanic, and
        `origin()` cannot tell them apart: `app` is a local name bound to a call,
        not an import. Asking whether the *module* imports fastapi or starlette
        is the cheap discriminator — and deliberately a loose one, because the
        strict version ("this name resolves to an APIRouter here") loses the
        common case of a router built in one module and decorated in another.
        """
        return bool(self.roots & {"fastapi", "starlette"})

    @property
    def reads_django(self) -> bool:
        """Does this module import Django, whose `.objects` is spelled like ormar's?

        **The fallback, not the gate.** Whether `Model.objects` is every row is a
        property of the *model* -- ormar's manager is every row, Django's is
        whatever `get_queryset()` left -- and the model is declared somewhere
        else entirely, so `analyzer/django.py` resolves it over the whole tree
        and `DjangoImage.objects_is_every_row` is what decides. Asking the
        *querying* module instead measured a different thing and said so: two
        files carrying identical chains against identical manager-free models
        got opposite verdicts because one of them also imported
        `django.db.transaction`.

        What is left for this to answer is the model the tree never declares --
        a `django.contrib.auth` model, or a name that is not a model at all.
        There is no manager to read, and a module that imports Django is the
        only evidence there is; guessing "plain" would translate every query
        against every model in every third-party app.
        """
        return "django" in self.roots
