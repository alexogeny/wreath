"""The translation rule catalog (design 07 §2), assembled from one table per domain.

Each rule maps a recognized source construct to (construct-name, coverage-category,
verdict-tag, message). The message names the wreath target idiom or the reason a
site needs review. Rule ids are stable and appear in the report so a reviewer can
`grep` a worklist. Seeded from docs/from-fastapi/{index,pydantic,sqlmodel,alembic}.md.
"""

from __future__ import annotations

from .auth import AUTH, AUTH_SCHEMES
from .background import BACKGROUND
from .confidence import CONFIDENCE
from .exceptions import EXCEPTIONS, RESPONSES
from .foreign_frameworks import FOREIGN, FOREIGN_TRANSLATED
from .graphql import GRAPHQL
from .libraries import CACHING, EXTERNAL, INTEGRATIONS, LOCKS, TIME
from .middleware import LIFESPAN, MIDDLEWARE
from .migrations import MIGRATIONS
from .models import PYDANTIC
from .orm import DJANGO_MODELS, ORM_MODELS, QUERIES
from .params import FORMS, PARAMS
from .routing import DEPENDENCIES, ROUTING
from .settings import SETTINGS
from .testing import TESTS

# rule_id -> (construct, category, tag, message)
RULES: dict[str, tuple[str, str, str, str]] = {
    **ROUTING,
    **PARAMS,
    **PYDANTIC,
    **DEPENDENCIES,
    **ORM_MODELS,
    **EXCEPTIONS,
    **SETTINGS,
    **QUERIES,
    **MIDDLEWARE,
    **LIFESPAN,
    **BACKGROUND,
    **GRAPHQL,
    **INTEGRATIONS,
    **FORMS,
    **LOCKS,
    **AUTH,
    **MIGRATIONS,
    **CACHING,
    **TIME,
    **RESPONSES,
    **AUTH_SCHEMES,
    **TESTS,
    **EXTERNAL,
    **CONFIDENCE,
    **DJANGO_MODELS,
    **FOREIGN,
    **FOREIGN_TRANSLATED,
}


def rule(rule_id: str) -> tuple[str, str, str, str]:
    return RULES[rule_id]
