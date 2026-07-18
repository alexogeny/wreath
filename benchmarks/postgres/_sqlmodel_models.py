"""SQLModel models for `bench_orm_competitors`, at module scope.

SQLModel is SQLAlchemy underneath, so it resolves annotations against the
defining module's globals for the same reason `_sqlalchemy_models` does, and
likewise must not use `from __future__ import annotations`.

Its table lives in `SQLModel.metadata`, which is a different registry from
`_sqlalchemy_models.Base.metadata`, so the two can map the same physical table
in one process without colliding.
"""

from sqlmodel import Field, Relationship, SQLModel

TABLE = "orm_competitor_items"
AUTHORS = "orm_competitor_authors"
BOOKS = "orm_competitor_books"


class Item(SQLModel, table=True):
    __tablename__ = TABLE

    id: int = Field(primary_key=True)
    number: int
    enabled: bool
    label: str


class Author(SQLModel, table=True):
    __tablename__ = AUTHORS

    id: int = Field(primary_key=True)
    name: str
    # Quoted forward reference: Book is defined below and this module has no
    # `from __future__ import annotations` on purpose (see the docstring).
    books: list["Book"] = Relationship(back_populates="author")  # noqa: UP037


class Book(SQLModel, table=True):
    __tablename__ = BOOKS

    id: int = Field(primary_key=True)
    author_id: int = Field(foreign_key=f"{AUTHORS}.id")
    title: str
    year: int
    author: Author | None = Relationship(back_populates="books")
