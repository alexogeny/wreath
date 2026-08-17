"""Cedar schema declarations compiled at application construction time.

The request evaluator deliberately knows nothing about schema text.  This
module turns the human Cedar schema syntax into the two facts the evaluator
needs: policy validation and the `Action` parent entities used by Cedar's
ordinary hierarchy walk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _Record:
    fields: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Set:
    item: Any


@dataclass(frozen=True, slots=True)
class _Action:
    parents: tuple[str, ...]
    context: Any | None


_MISSING = object()
# The discovery scanner deliberately cannot prove two parser facts: statement
# and field slices are disjoint (their total copied length is <= the input),
# and `_type`/`_resolve` recurse over a declaration tree rather than over the
# same text. Cap both axes explicitly so malformed startup configuration cannot
# turn either into unbounded work. These shapes are recorded in the discovery
# baseline for that reason; they are startup-only and never enter evaluation.
_MAX_SCHEMA_BYTES = 1 << 20
_MAX_TYPE_DEPTH = 64


def _without_comments(source: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", source, flags=re.S)


def _statements(source: str) -> tuple[str, ...]:
    statements: list[str] = []
    start = 0
    depth = 0
    quoted = False
    escaped = False
    for index, character in enumerate(source):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in "{[<":
            depth = depth + 1
        elif character in "}]>":
            depth = depth - 1
        elif character == ";" and depth == 0:
            statement = source[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
    tail = source[start:].strip()
    if tail:
        raise ValueError(f"Cedar schema declaration is missing ';': {tail[:60]!r}")
    if depth or quoted:
        raise ValueError("Cedar schema has an unterminated record, set, or string")
    return tuple(statements)


def _parts(source: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quoted = False
    for index, character in enumerate(source):
        if character == '"':
            quoted = not quoted
        elif not quoted and character in "{[<":
            depth = depth + 1
        elif not quoted and character in "}]>":
            depth = depth - 1
        elif not quoted and character == "," and depth == 0:
            parts.append(source[start:index].strip())
            start = index + 1
    part = source[start:].strip()
    if part:
        parts.append(part)
    return tuple(parts)


def _type(source: str, aliases: dict[str, Any], depth: int = 0) -> Any:
    if depth > _MAX_TYPE_DEPTH:
        raise ValueError(f"Cedar schema type nesting exceeds {_MAX_TYPE_DEPTH}")
    source = source.strip()
    if source.startswith("{") and source.endswith("}"):
        fields: dict[str, Any] = {}
        for declaration in _parts(source[1:-1]):
            match = re.fullmatch(
                r'(?P<name>"(?:\\.|[^"])+"|[A-Za-z_][\w]*)\s*(?P<optional>\?)?\s*:\s*(?P<type>.+)',
                declaration,
                re.S,
            )
            if match is None:
                raise ValueError(f"invalid Cedar schema field declaration: {declaration!r}")
            name = match.group("name")
            if name.startswith('"'):
                name = bytes(name[1:-1], "utf-8").decode("unicode_escape")
            fields[name] = _type(match.group("type"), aliases, depth + 1)
        return _Record(fields)
    if source.startswith("Set<") and source.endswith(">"):
        return _Set(_type(source[4:-1], aliases, depth + 1))
    return aliases.get(source, source)


def _resolve(
    value: Any,
    aliases: dict[str, Any],
    entities: dict[str, _Record],
    seen: frozenset[str] = frozenset(),
    depth: int = 0,
) -> Any:
    if depth > _MAX_TYPE_DEPTH:
        raise ValueError(f"Cedar schema reference nesting exceeds {_MAX_TYPE_DEPTH}")
    if isinstance(value, str) and value not in seen:
        target = aliases.get(value, entities.get(value))
        if target is not None:
            return _resolve(target, aliases, entities, seen | {value}, depth + 1)
    if isinstance(value, _Record):
        return _Record(
            {
                name: _resolve(item, aliases, entities, seen, depth + 1)
                for name, item in value.fields.items()
            }
        )
    if isinstance(value, _Set):
        return _Set(_resolve(value.item, aliases, entities, seen, depth + 1))
    return value


class CedarSchema:
    """A parsed Cedar schema used to reject bad policies before startup.

    The supported input is Cedar's human-readable schema syntax.  Action
    hierarchy declarations are also compiled into ordinary `Action` entity
    parents, so `action in Action::\"group\"` runs in the native evaluator
    without request-time schema work.
    """

    __slots__ = ("_actions", "_aliases", "_children", "_descendants", "_entities", "source")

    def __init__(self, source: str) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Cedar schema source must be non-empty text")
        if len(source.encode("utf-8")) > _MAX_SCHEMA_BYTES:
            raise ValueError(f"Cedar schema exceeds {_MAX_SCHEMA_BYTES} UTF-8 bytes")
        aliases: dict[str, Any] = {}
        entities: dict[str, _Record] = {}
        pending_actions: list[tuple[str, str]] = []
        for statement in _statements(_without_comments(source)):
            if statement.startswith("namespace "):
                raise ValueError(
                    "Cedar schema namespace blocks are not supported; "
                    "declare top-level types with their qualified names"
                )
            match = re.fullmatch(r"type\s+([A-Za-z_][\w:]*)\s*=\s*(.+)", statement, re.S)
            if match is not None:
                aliases[match.group(1)] = _type(match.group(2), aliases)
                continue
            match = re.fullmatch(
                r"entity\s+([A-Za-z_][\w:]*)\s*(?:in\s*\[[^]]*\])?\s*(?:=\s*(\{.*\}))?",
                statement,
                re.S,
            )
            if match is not None:
                value = _type(match.group(2) or "{}", aliases)
                if not isinstance(value, _Record):
                    raise ValueError(f"entity {match.group(1)!r} must declare a record")
                entities[match.group(1)] = value
                continue
            match = re.fullmatch(r'action\s+"((?:\\.|[^"])*)"(?P<body>.*)', statement, re.S)
            if match is not None:
                pending_actions.append((match.group(1), match.group("body")))
                continue
            raise ValueError(f"unsupported Cedar schema declaration: {statement[:80]!r}")

        aliases = {name: _resolve(value, aliases, entities) for name, value in aliases.items()}
        entities = {name: _resolve(value, aliases, entities) for name, value in entities.items()}
        actions: dict[str, _Action] = {}
        for name, body in pending_actions:
            parent_match = re.match(r"\s*in\s*\[(?P<parents>[^]]*)\]", body, re.S)
            parents = ()
            if parent_match is not None:
                parents = tuple(re.findall(r'"((?:\\.|[^"])*)"', parent_match.group("parents")))
            context: Any | None = None
            applies = re.search(r"appliesTo\s*\{(?P<body>.*)\}\s*$", body, re.S)
            if applies is not None:
                entries = _parts(applies.group("body"))
                for entry in entries:
                    key, separator, value = entry.partition(":")
                    if separator and key.strip() == "context":
                        context = _resolve(_type(value, aliases), aliases, entities)
            actions[name] = _Action(parents, context)
        for name, action in actions.items():
            for parent in action.parents:
                if parent not in actions:
                    raise ValueError(
                        f"action {name!r} names unknown parent {parent!r}; "
                        "declare the parent action first"
                    )
        self.source = source
        self._aliases = aliases
        self._entities = entities
        self._actions = actions
        children: dict[str, list[str]] = {name: [] for name in actions}
        for child, action in actions.items():
            for parent in action.parents:
                children[parent].append(child)
        self._children = {name: tuple(values) for name, values in children.items()}
        self._descendants: dict[str, frozenset[str]] = {}

    @property
    def actions(self) -> tuple[str, ...]:
        """Action ids declared by this schema, in declaration order."""
        return tuple(self._actions)

    def action_parents(self, name: str) -> tuple[str, ...]:
        return self._actions[name].parents

    def descendants(self, name: str) -> frozenset[str]:
        cached = self._descendants.get(name)
        if cached is not None:
            return cached
        found: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            for child in self._children[current]:
                stack.append(child)
        result = frozenset(found)
        self._descendants[name] = result
        return result

    def contexts(self, actions: set[str]) -> tuple[Any, ...]:
        contexts: list[Any] = []
        for name, action in self._actions.items():
            if name not in actions:
                continue
            if action.context is None and self._children[name]:
                # A grouping action contributes its descendants' contracts. It
                # is not itself a request action unless a policy names it with
                # equality, which the caller passes as a one-element set.
                continue
            contexts.append(action.context)
        return tuple(contexts)

    def require_action(self, name: str) -> None:
        if name not in self._actions:
            known = ", ".join(repr(item) for item in self._actions)
            raise ValueError(f"policy names unknown action {name!r}; declared actions are {known}")


def _field_type(types: tuple[Any, ...], name: str) -> tuple[Any, ...]:
    if not types:
        return ()
    values: list[Any] = []
    for value in types:
        if not isinstance(value, _Record):
            return ()
        field = value.fields.get(name, _MISSING)
        if field is _MISSING:
            return ()
        values.append(field)
    return tuple(values)


def validate_context_expression(
    node: object, contexts: tuple[Any, ...], *, path: str = "context"
) -> None:
    """Reject attribute reads absent from every applicable context record."""
    if not isinstance(node, tuple | list):
        return
    if isinstance(node, tuple) and len(node) == 3 and node[0] == 10:
        name = node[2]
        if not isinstance(name, str):
            raise ValueError("compiled Cedar attribute name must be text")
        base_types, base_path = _expression_types(node[1], contexts, path)
        if node[1] == (1, 3):
            base_types, base_path = contexts, path
            if not base_types:
                raise ValueError(
                    f"policy tests {path}.{name}, but its applicable actions "
                    "declare no context; add context: { ... } to appliesTo"
                )
        if base_types and not _field_type(base_types, name):
            raise ValueError(
                f"policy tests unknown Cedar schema attribute {base_path}.{name}; "
                f"declare {name!r} in every applicable context record"
            )
    # Opcodes are deliberately numeric at this seam to keep this startup-only
    # parser independent of the evaluator module's private names.
    if isinstance(node, tuple) and len(node) == 3 and node[0] == 16:
        base = node[1]
        name = node[2]
        if not isinstance(name, str):
            raise ValueError("compiled Cedar attribute name must be text")
        if base == (1, 3):
            base_types = contexts
            base_path = path
            if not base_types:
                raise ValueError(
                    f"policy reads {path}.{name}, but its applicable actions "
                    "declare no context; add context: {{ ... }} to appliesTo"
                )
        else:
            base_types, base_path = _expression_types(base, contexts, path)
        if base_types:
            resolved = _field_type(base_types, name)
            if not resolved:
                raise ValueError(
                    f"policy reads unknown Cedar schema attribute {base_path}.{name}; "
                    f"declare {name!r} in the applicable context record"
                )
    for part in node:
        validate_context_expression(part, contexts, path=path)


def _expression_types(
    node: object, contexts: tuple[Any, ...], path: str
) -> tuple[tuple[Any, ...], str]:
    if node == (1, 3):
        return contexts, path
    if isinstance(node, tuple) and len(node) == 3 and node[0] == 16:
        name = node[2]
        if not isinstance(name, str):
            return (), path
        parents, parent_path = _expression_types(node[1], contexts, path)
        return _field_type(parents, name), f"{parent_path}.{name}"
    return (), path
