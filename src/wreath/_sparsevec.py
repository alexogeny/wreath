"""The Python value a pgvector `sparsevec` column holds.

A `vector` is a `list[float]` and needs no type of its own: the dimension is the
length, and every position carries a number. A `sparsevec` is not that. It is a
dimension -- which may be a billion -- plus the handful of positions that are
not zero, and there is no Python builtin that says both halves at once. A bare
`dict` loses the dimension, a bare `list` defeats the point of the type, and a
`(dim, dict)` tuple is a shape every caller has to remember the order of.

So: one small immutable value, in a module that imports nothing, because both
halves of the driver need it. `wreath._pgdriver` imports it directly and
`_native/postgres/codec.c` imports it at module init the same way it imports
`uuid.UUID` and `decimal.Decimal`. Anything under `wreath/` importing anything
else under `wreath/` from here would be a cycle, which is why this file holds
one class and no framework imports.

**Indices are 1-based**, matching how pgvector writes them --
`'{1:1.5,3:3.5}/5'` is the first and third of five positions. The *binary* wire
format is 0-based, and the conversion happens at that boundary and nowhere else.
The alternative -- exposing the wire's numbering -- would mean a value printed by
`psql` and the same value in Python disagree by one, which is the kind of
difference that is only ever found in production.
"""

from __future__ import annotations

from typing import Any

from ._native import _core

#: pgvector's `SPARSEVEC_MAX_DIM`. Far beyond `vector`'s 16,000, which is the
#: reason the type exists: a bag-of-words over a real vocabulary is mostly zero.
MAX_SPARSEVEC_DIM = 1_000_000_000

#: pgvector's `SPARSEVEC_MAX_NNZ` -- how many non-zero elements one value may
#: hold, whatever its dimension. A sparse vector denser than this is a dense
#: vector wearing the wrong type, and pgvector refuses it at the server.
MAX_SPARSEVEC_NNZ = 16_000

__all__ = ["MAX_SPARSEVEC_DIM", "MAX_SPARSEVEC_NNZ", "SparseVector"]


class SparseVector:
    """A pgvector `sparsevec` value: a dimension, and the positions that are not zero.

    Construct one from a mapping of **1-based** index to value, or from anything
    `dict()` accepts (an iterable of pairs, another `SparseVector`'s
    `to_dict()`):

    ```python
    SparseVector(5, {1: 1.5, 3: 3.5})       # '{1:1.5,3:3.5}/5' to pgvector
    SparseVector.from_dense([1.5, 0, 3.5, 0, 0])   # the same value
    ```

    Explicit zeros are dropped rather than stored. That is what the server does
    to them, so keeping them would mean a value did not survive its own round
    trip -- write `{2: 0.0}`, read back `{}` -- and a difference that appears
    only after a write is the worst kind to debug.

    Immutable in practice: `indices` and `values` are tuples and there is no
    setter. Two values are equal when their dimension and their non-zero
    elements are, so `SparseVector(5, {1: 1.0})` equals itself across a round
    trip through the database.

    Attributes:
        dim: The declared dimension -- how many positions exist, not how many
            are stored.
        indices: The stored positions, 1-based and ascending.
        values: The stored values, positionally paired with `indices`.
    """

    __slots__ = ("_data", "_pg_oid")

    def __init__(self, dim: Any, elements: Any = ()) -> None:
        self._data = _core.sparsevector_data(dim, elements, MAX_SPARSEVEC_NNZ)
        #: Set by `Sparsevec.to_wire`, read by the driver's parameter-OID
        #: inference. Zero means "not bound to a database yet"; see `WireList`
        #: in `orm/types.py`, which solves the same problem for `vector`.
        self._pg_oid = 0

    @property
    def dim(self) -> int:
        """The declared dimension."""
        return _core.sparsevector_dim(self._data)

    @property
    def indices(self) -> tuple[int, ...]:
        """The stored 1-based positions, materialized at the Python boundary."""
        return _core.sparsevector_indices(self._data)

    @property
    def values(self) -> tuple[float, ...]:
        """The stored values, materialized at the Python boundary."""
        return _core.sparsevector_values(self._data)

    @classmethod
    def from_dense(cls, values: Any) -> SparseVector:
        """The sparse form of a dense sequence, dropping its zeros.

        The dimension is the sequence's length, so a 30,000-word bag whose
        vocabulary is fixed keeps its dimension even when a document uses nine
        of the words.
        """
        items = list(values)
        return cls(len(items), {i + 1: v for i, v in enumerate(items) if v != 0})

    def to_dict(self) -> dict[int, float]:
        """The non-zero elements, 1-based index to value."""
        return _core.sparsevector_dict(self._data)

    def _with_oid(self, oid: int) -> SparseVector:
        """A copy that names its own PostgreSQL OID, for parameter inference.

        A copy rather than a mutation: the value handed to `to_wire` belongs to
        the caller, and an application that keeps one around and binds it
        against two databases must not have the first one's OID written into it.
        """
        twin = SparseVector.__new__(SparseVector)
        twin._data = self._data
        twin._pg_oid = oid
        return twin

    def __len__(self) -> int:
        """How many elements are stored -- pgvector's `nnz`, not the dimension."""
        return _core.sparsevector_len(self._data)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SparseVector):
            return NotImplemented
        return _core.sparsevector_equal(self._data, other._data)

    def __hash__(self) -> int:
        return _core.sparsevector_hash(self._data)

    def __repr__(self) -> str:
        return f"SparseVector({self.dim}, {self.to_dict()!r})"
