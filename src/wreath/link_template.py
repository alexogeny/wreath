"""RFC 9652 Link-Template response field values."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ._structured_fields import DisplayString, Item, serialize_item

__all__ = ["LinkTemplate", "serialize_link_templates"]

_PARAMETER_KEY = re.compile(r"[a-z*][a-z0-9_.*-]*\Z")
_HEX = frozenset("0123456789abcdefABCDEF")
_OPERATORS = frozenset("+#./;?&")
_RESERVED_PARAMETERS = frozenset({"rel", "anchor", "var-base"})


def _validate_percent_encoded(value: str, index: int, context: str) -> int:
    if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
        raise ValueError(f"{context} has an invalid percent-encoded sequence at offset {index}")
    return index + 3


def _validate_varname(name: str) -> None:
    if not name:
        raise ValueError("URI template expression needs a variable name")
    segment_has_value = False
    index = 0
    while index < len(name):
        character = name[index]
        if character == ".":
            if not segment_has_value:
                raise ValueError(f"URI template variable {name!r} has an empty name segment")
            segment_has_value = False
            index += 1
            continue
        if character == "%":
            index = _validate_percent_encoded(name, index, f"URI template variable {name!r}")
            segment_has_value = True
            continue
        if character.isascii() and (character.isalnum() or character == "_"):
            segment_has_value = True
            index += 1
            continue
        raise ValueError(f"URI template variable {name!r} contains invalid character {character!r}")
    if not segment_has_value:
        raise ValueError(f"URI template variable {name!r} has an empty name segment")


def _validate_varspec(varspec: str) -> None:
    if not varspec:
        raise ValueError("URI template expression needs a variable name")
    if "*" in varspec:
        if not varspec.endswith("*") or varspec.count("*") != 1:
            raise ValueError(f"URI template variable {varspec!r} has an invalid explode modifier")
        name = varspec[:-1]
    elif ":" in varspec:
        name, separator, prefix = varspec.partition(":")
        if not separator or not prefix.isascii() or not prefix.isdigit():
            raise ValueError(f"URI template variable {varspec!r} has an invalid prefix modifier")
        if len(prefix) > 4 or prefix[0] == "0":
            raise ValueError(
                f"URI template variable {varspec!r} prefix must be one to four digits "
                "starting from 1"
            )
    else:
        name = varspec
    _validate_varname(name)


def _validate_expression(expression: str) -> None:
    if expression and expression[0] in _OPERATORS:
        expression = expression[1:]
    if not expression:
        raise ValueError("URI template expression needs at least one variable")
    for varspec in expression.split(","):
        _validate_varspec(varspec)


def _validate_template(template: str) -> str:
    if not isinstance(template, str):
        raise TypeError(f"template must be str, not {type(template).__name__}")
    try:
        template.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("template must be ASCII; percent-encode non-ASCII characters") from error
    index = 0
    while index < len(template):
        character = template[index]
        if character == "%":
            index = _validate_percent_encoded(template, index, "URI template")
            continue
        if character == "}":
            raise ValueError(f"URI template has an unmatched closing brace at offset {index}")
        if character != "{":
            if ord(character) < 0x21 or character in '"<>\\^`{|}':
                raise ValueError(
                    f"URI template contains invalid literal {character!r} at offset {index}"
                )
            index += 1
            continue
        closing = template.find("}", index + 1)
        if closing < 0:
            raise ValueError(f"URI template opening brace at offset {index} needs a closing brace")
        expression = template[index + 1 : closing]
        if "{" in expression:
            raise ValueError(f"URI template expression at offset {index} contains a nested brace")
        _validate_expression(expression)
        index = closing + 1
    return template


def _ascii_parameter(name: str, value: str | None, *, non_empty: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str or None, not {type(value).__name__}")
    if non_empty and not value:
        raise ValueError(f"{name} must not be empty")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be ASCII; percent-encode non-ASCII characters") from error
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise ValueError(f"{name} must contain only printable ASCII")
    return value


@dataclass(frozen=True, slots=True)
class LinkTemplate:
    """One validated RFC 9652 templated link, serialized once at construction."""

    template: str
    rel: str | None = None
    anchor: str | None = None
    var_base: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)
    _header: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        template = _validate_template(self.template)
        rel = _ascii_parameter("rel", self.rel, non_empty=True)
        anchor = _ascii_parameter("anchor", self.anchor)
        if anchor is not None:
            _validate_template(anchor)
        var_base = _ascii_parameter("var_base", self.var_base)
        if not isinstance(self.attributes, Mapping):
            raise TypeError(
                f"attributes must be a mapping of parameter names to strings, not "
                f"{type(self.attributes).__name__}"
            )

        copied: dict[str, str] = {}
        attribute_parameters: dict[str, str | DisplayString] = {}
        for name, value in self.attributes.items():
            if not isinstance(name, str) or _PARAMETER_KEY.fullmatch(name) is None:
                raise ValueError(f"invalid Link-Template parameter name {name!r}")
            if name in _RESERVED_PARAMETERS:
                raise ValueError(
                    f"attribute {name!r} is reserved; pass it through its named argument"
                )
            if not isinstance(value, str):
                raise TypeError(
                    f"Link-Template attribute {name!r} must be str, not {type(value).__name__}"
                )
            copied[name] = value
            try:
                value.encode("ascii")
            except UnicodeEncodeError:
                attribute_parameters[name] = DisplayString(value)
            else:
                attribute_parameters[name] = value

        parameters: dict[str, str | DisplayString] = {}
        if rel is not None:
            parameters["rel"] = rel
        if anchor is not None:
            parameters["anchor"] = anchor
        if var_base is not None:
            parameters["var-base"] = var_base
        parameters.update(attribute_parameters)

        object.__setattr__(self, "attributes", MappingProxyType(copied))
        object.__setattr__(self, "_header", serialize_item(Item(template, parameters)))

    def to_header(self) -> bytes:
        """Return this link as one Structured Fields list member."""
        return self._header


def serialize_link_templates(templates: Iterable[LinkTemplate]) -> bytes:
    """Serialize a non-empty sequence as one Link-Template field value."""
    members: list[bytes] = []
    for index, template in enumerate(templates):
        if not isinstance(template, LinkTemplate):
            raise TypeError(
                f"Link-Template member {index} must be LinkTemplate, not {type(template).__name__}"
            )
        members.append(template.to_header())
    if not members:
        raise ValueError("Link-Template needs at least one LinkTemplate")
    return b", ".join(members)


def _set_link_templates(
    headers: list[tuple[bytes, bytes]], templates: Iterable[LinkTemplate]
) -> None:
    value = serialize_link_templates(templates)
    headers[:] = [
        (name, existing) for name, existing in headers if name.lower() != b"link-template"
    ]
    headers.append((b"link-template", value))
