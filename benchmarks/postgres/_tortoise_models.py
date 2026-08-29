"""Tortoise models for `bench_orm_competitors`, at module scope.

Tortoise discovers models by scanning a module's namespace, so they cannot be
declared inside the setup function the way the other ORMs' are. This module is
imported only when the competitor benchmark runs, so Tortoise stays an optional
benchmark dependency.
"""

from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

TABLE = "orm_competitor_items"
AUTHORS = "orm_competitor_authors"
BOOKS = "orm_competitor_books"


class Item(Model):
    id = fields.BigIntField(pk=True)
    number = fields.IntField()
    enabled = fields.BooleanField()
    label = fields.TextField()

    class Meta:
        table = TABLE


class Author(Model):
    id = fields.BigIntField(pk=True)
    name = fields.TextField()

    class Meta:
        table = AUTHORS


class Book(Model):
    id = fields.BigIntField(pk=True)
    author = fields.ForeignKeyField("models.Author", related_name="books", source_field="author_id")
    title = fields.TextField()
    year = fields.IntField()

    class Meta:
        table = BOOKS
