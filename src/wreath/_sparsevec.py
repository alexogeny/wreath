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

import math
from typing import Any

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

    __slots__ = ("_pg_oid", "dim", "indices", "values")

    def __init__(self, dim: Any, elements: Any = ()) -> None:
        if dim.__class__ is not int:
            raise TypeError(
                f"SparseVector dimension must be int, not {type(dim).__name__}"
            )
        if not 1 <= dim <= MAX_SPARSEVEC_DIM:
            raise ValueError(
                f"SparseVector({dim}) is out of range; pgvector allows 1 to "
                f"{MAX_SPARSEVEC_DIM} dimensions"
            )
        mapping = elements if isinstance(elements, dict) else dict(elements)
        indices: list[int] = []
        values: list[float] = []
        for index in sorted(mapping):
            if index.__class__ is not int:
                raise TypeError(
                    f"sparsevec index {index!r} must be int, not "
                    f"{type(index).__name__}"
                )
            if not 1 <= index <= dim:
                raise ValueError(
                    f"sparsevec index {index} is outside 1..{dim}; indices are "
                    "1-based, the way pgvector writes them"
                )
            number = _as_element(mapping[index], index)
            if number == 0.0:
                continue
            indices.append(index)
            values.append(number)
        if len(indices) > MAX_SPARSEVEC_NNZ:
            raise ValueError(
                f"a sparsevec holds at most {MAX_SPARSEVEC_NNZ} non-zero elements; "
                f"this one has {len(indices)}. A value that dense wants `Vector` "
                "or `Halfvec`, which store every position and index far better"
            )
        self.dim = dim
        self.indices = tuple(indices)
        self.values = tuple(values)
        #: Set by `Sparsevec.to_wire`, read by the driver's parameter-OID
        #: inference. Zero means "not bound to a database yet"; see `WireList`
        #: in `orm/types.py`, which solves the same problem for `vector`.
        self._pg_oid = 0

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
        return dict(zip(self.indices, self.values, strict=True))

    def _with_oid(self, oid: int) -> SparseVector:
        """A copy that names its own PostgreSQL OID, for parameter inference.

        A copy rather than a mutation: the value handed to `to_wire` belongs to
        the caller, and an application that keeps one around and binds it
        against two databases must not have the first one's OID written into it.
        """
        twin = SparseVector.__new__(SparseVector)
        twin.dim = self.dim
        twin.indices = self.indices
        twin.values = self.values
        twin._pg_oid = oid
        return twin

    def __len__(self) -> int:
        """How many elements are stored -- pgvector's `nnz`, not the dimension."""
        return len(self.indices)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SparseVector):
            return NotImplemented
        return (
            self.dim == other.dim
            and self.indices == other.indices
            and self.values == other.values
        )

    def __hash__(self) -> int:
        return hash((self.dim, self.indices, self.values))

    def __repr__(self) -> str:
        return f"SparseVector({self.dim}, {self.to_dict()!r})"


def _as_element(value: Any, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"sparsevec value at index {index} must be int or float, not "
            f"{type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(
            f"sparsevec value at index {index} is {number!r}; pgvector stores "
            "neither NaN nor infinity"
        )
    return number
