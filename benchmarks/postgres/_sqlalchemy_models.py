"""SQLAlchemy models for `bench_orm_competitors`, at module scope.

SQLAlchemy resolves `Mapped[...]` annotations against the *defining module's*
globals, so a class declared inside a setup function cannot be mapped. Note the
deliberate absence of `from __future__ import annotations`: it would turn these
annotations into strings and defeat the resolution entirely.

Imported only when the competitor benchmark runs, so SQLAlchemy stays an
optional benchmark dependency. It is a benchmark competitor and nothing more --
AGENTS.md rules out SQLAlchemy integration or compatibility layers in Wreath
itself.
"""

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TABLE = "orm_competitor_items"
AUTHORS = "orm_competitor_authors"
BOOKS = "orm_competitor_books"


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = TABLE

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    number: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    label: Mapped[str] = mapped_column(Text)


class Author(Base):
    __tablename__ = AUTHORS

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    # Quoted forward reference: Book is defined below, and this module has no
    # `from __future__ import annotations` on purpose (see the docstring), so
    # the name must not be evaluated now.
    books: Mapped[list["Book"]] = relationship(back_populates="author")  # noqa: UP037


class Book(Base):
    __tablename__ = BOOKS

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(f"{AUTHORS}.id"))
    title: Mapped[str] = mapped_column(Text)
    year: Mapped[int] = mapped_column(Integer)
    author: Mapped["Author"] = relationship(back_populates="books")  # noqa: UP037
