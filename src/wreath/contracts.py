"""Transport-neutral, startup-compiled dataclass contracts."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from . import _json
from .binding import ValidationError, _body_validator, _compile_jsonable

DEFAULT_MAX_BYTES = 1 << 20


class Contract[T]:
    """A reusable validator and JSON codec for one dataclass declaration.

    Construction compiles the same native validation plan used by request
    binding. Instances are immutable declarations in practice: all policy is
    fixed in `__init__` and only compiled callables are retained.
    """

    __slots__ = ("_jsonable", "_validator", "max_bytes", "model", "name", "version")

    def __init__(
        self,
        model: type[T],
        *,
        name: str | None = None,
        version: int = 1,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if not isinstance(model, type) or not dataclasses.is_dataclass(model):
            raise TypeError(
                f"Contract model must be a dataclass type, got {model!r}; "
                "decorate the declaration with @dataclass"
            )
        resolved_name = name or f"{model.__module__}.{model.__qualname__}"
        if not resolved_name or len(resolved_name.encode("utf-8")) > 255:
            raise ValueError("Contract name must be between 1 and 255 UTF-8 bytes")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("Contract version must be an integer >= 1")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("Contract max_bytes must be a positive integer")
        self.model = model
        self.name = resolved_name
        self.version = version
        self.max_bytes = max_bytes
        self._validator = _body_validator(model)
        self._jsonable = _compile_jsonable(model)

    def validate(self, value: T | Mapping[str, Any]) -> T:
        """Validate and construct the declared dataclass."""
        if isinstance(value, self.model):
            return value
        return self._validator(value, ("contract", self.name))

    def to_data(self, value: T | Mapping[str, Any]) -> dict[str, Any]:
        """Return the validated value as a JSON-ready mapping."""
        result = self._jsonable(self.validate(value))
        if not isinstance(result, dict):
            raise TypeError(f"Contract {self.name!r} did not serialize to an object")
        return result

    def encode_json(self, value: T | Mapping[str, Any]) -> bytes:
        """Validate and encode with Wreath's native JSON kernel."""
        encoded = _json.dumps(self.to_data(value))
        if len(encoded) > self.max_bytes:
            raise ValueError(
                f"Contract {self.name!r} JSON is {len(encoded)} bytes; "
                f"max_bytes is {self.max_bytes}"
            )
        return encoded

    def decode_json(self, data: str | bytes | bytearray) -> T:
        """Decode and validate JSON, refusing oversized input before parsing."""
        size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
        if size > self.max_bytes:
            raise ValueError(
                f"Contract {self.name!r} JSON is {size} bytes; max_bytes is {self.max_bytes}"
            )
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return self._validator.decode_json_validation_tape(
            raw, ("contract", self.name)
        )


def compile_contract[T](
    model: type[T],
    *,
    name: str | None = None,
    version: int = 1,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Contract[T]:
    """Compile `model` into a reusable `Contract`."""
    return Contract(model, name=name, version=version, max_bytes=max_bytes)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "Contract",
    "ValidationError",
    "compile_contract",
]
