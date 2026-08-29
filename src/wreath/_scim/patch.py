"""`PATCH` and `PUT` as one operation: produce the resource's next representation.

RFC 7644 section 3.5.2 defines `PATCH` as a list of `add`/`remove`/`replace`
operations over attribute paths, and section 3.5.1 defines `PUT` as a whole
representation. Both are modelled here as *pure functions on the SCIM document*:
they take the resource as it currently reads, and return how it should read
next. Nothing in this module touches a store.

That split is what keeps the two verbs from drifting. The endpoint applies
whichever function the verb calls for, then hands the resulting document to one
writer that reconciles it against `wreath.organizations` and the user store --
so `PUT` and `PATCH` cannot disagree about what setting `active` to `false`
means, because neither of them decides it.

Two semantics chosen here rather than left to the protocol, because reversing
either later loses data:

* **Removing a member that is not there is a no-op**, not an error. Section
  3.5.2.2 permits either reading for a value-selection filter that matches
  nothing; a directory that retries a de-provisioning it already completed is
  the common case, and answering 400 to it turns a settled state into a
  recurring alarm. A `replace` whose filter matches nothing *is* refused --
  there the specification says `SHALL` -- because it asks to modify something
  that is not there rather than to reach a state that already holds.
* **`remove` is refused for a single-valued attribute this provider writes.**
  Those are `userName`, `active` and `password`, and none of them has a coherent
  absent state: a user with no `userName` cannot be addressed and a user with no
  `active` is not thereby anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .filters import Filter, FilterError, parse, select
from .resources import Shape

__all__ = [
    "MAX_OPERATIONS",
    "PATCH_OP_URN",
    "Path",
    "PatchError",
    "apply",
    "parse_path",
    "replace",
]

#: The `schemas` value section 3.5.2 requires of a `PATCH` body.
PATCH_OP_URN = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

#: Most operations accepted in one `PATCH`. A directory sends a handful; an
#: unbounded list is a request whose cost the client chooses.
MAX_OPERATIONS = 100


class PatchError(ValueError):
    """A refusal that maps onto a SCIM error document.

    `scim_type` is the `scimType` value of section 3.12 -- `invalidPath`,
    `invalidValue`, `invalidSyntax`, `mutability`, or `noTarget` -- and `detail`
    is the sentence the client reads. Every site writes its own `detail`; the
    tests assert those sentences, because a refusal test that asserts only the
    status passes on whichever branch fired.
    """

    __slots__ = ("detail", "scim_type")

    def __init__(self, scim_type: str, detail: str) -> None:
        super().__init__(detail)
        self.scim_type = scim_type
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Path:
    """A parsed `PATCH` path: `attribute`, an optional filter, an optional sub.

    `members[value eq "u1"].display` is `Path("members", <filter>, "display")`.
    Both parts are lowercased, since SCIM attribute names are case-insensitive.
    """

    attribute: str
    predicate: Filter | None = None
    sub_attribute: str | None = None


def parse_path(source: str, *, shape: Shape) -> Path:
    """`source` as a `Path`, refusing an attribute this provider does not hold.

    Raises:
        PatchError: `invalidPath` for a malformed path or an unknown attribute.
    """
    text = source.strip()
    if not text:
        raise PatchError("invalidPath", "a patch operation has an empty path")
    head, bracket, rest = text.partition("[")
    if not bracket:
        # The schema URN comes off *before* the sub-attribute split, because it
        # contains dots of its own -- `...:core:2.0:User:userName` would
        # otherwise parse as the attribute `2` with the sub-attribute
        # `0:User:userName`, and refuse a perfectly ordinary qualified path.
        attribute, _, sub = text.rpartition(":")[2].partition(".")
        return _path(attribute, None, sub, shape)
    body, closer, tail = rest.rpartition("]")
    if not closer:
        raise PatchError("invalidPath", f"patch path {source!r} has no closing ']'")
    try:
        predicate = parse(body, attributes=None)
    except FilterError as error:
        raise PatchError(
            "invalidPath", f"patch path {source!r} has an invalid filter: {error.detail}"
        ) from None
    if tail and not tail.startswith("."):
        raise PatchError(
            "invalidPath",
            f"patch path {source!r} has unexpected text after ']': {tail!r}",
        )
    # `tail` is either empty or begins with the dot checked above, so slicing it
    # unconditionally is the same answer as branching on it.
    return _path(head, predicate, tail[1:], shape)


def _path(attribute: str, predicate: Filter | None, sub: str, shape: Shape) -> Path:
    name = attribute.strip().rpartition(":")[2].lower()
    if not name:
        raise PatchError("invalidPath", "a patch operation has an empty attribute name")
    if name not in shape.attributes:
        raise PatchError(
            "invalidPath",
            f"a {shape.name} has no attribute named {name!r} on this provider; it "
            f"has {', '.join(sorted(shape.canonical.values()))}",
        )
    return Path(name, predicate, sub.lower() or None)


def _operations(payload: Any) -> Sequence[Any]:
    """The `Operations` list of a `PATCH` body, validated as a list of objects."""
    if not isinstance(payload, Mapping):
        raise PatchError("invalidSyntax", "a patch body must be a JSON object")
    schemas = payload.get("schemas")
    if schemas is not None and (not isinstance(schemas, list) or PATCH_OP_URN not in schemas):
        raise PatchError(
            "invalidSyntax",
            f"a patch body's schemas must contain {PATCH_OP_URN!r}",
        )
    operations = payload.get("Operations", payload.get("operations"))
    if not isinstance(operations, list) or not operations:
        raise PatchError("invalidSyntax", "a patch body must carry a non-empty Operations list")
    if len(operations) > MAX_OPERATIONS:
        raise PatchError(
            "invalidValue",
            f"a patch body may carry at most {MAX_OPERATIONS} operations "
            f"({len(operations)} were sent)",
        )
    return operations


def apply(document: Mapping[str, Any], payload: Any, *, shape: Shape) -> dict[str, Any]:
    """`document` after every operation in `payload`, or nothing at all.

    The operations are applied to a copy and the copy is returned, so a refusal
    part-way through leaves the caller holding the original -- which is the
    "apply all or none" rule of section 3.5.2 without a transaction.

    Args:
        document: the resource as it currently reads.
        payload: the parsed `PATCH` body.
        shape: which attributes the resource type has, and which of them a
            client may write. Anything outside `shape.writable` is `readOnly`
            and refused with `mutability`.

    Raises:
        PatchError: any refusal, carrying its `scimType`.
    """
    draft = deepcopy(dict(document))
    for operation in _operations(payload):
        if not isinstance(operation, Mapping):
            raise PatchError("invalidSyntax", "each patch operation must be an object")
        raw_op = operation.get("op")
        if not isinstance(raw_op, str):
            raise PatchError("invalidSyntax", "each patch operation needs an 'op'")
        op = raw_op.lower()
        if op not in ("add", "remove", "replace"):
            raise PatchError(
                "invalidSyntax",
                f"unknown patch operation {raw_op!r}; expected add, remove or replace",
            )
        raw_path = operation.get("path")
        has_value = "value" in operation
        value = operation.get("value")
        if raw_path is None:
            if op == "remove":
                raise PatchError("noTarget", "a remove operation must name a path to remove")
            if not isinstance(value, Mapping):
                raise PatchError(
                    "invalidValue",
                    f"a pathless {op} operation's value must be an object of attributes to set",
                )
            for name, sub_value in value.items():
                _one(draft, op, parse_path(str(name), shape=shape), sub_value, True, shape)
            continue
        if not isinstance(raw_path, str):
            raise PatchError("invalidPath", "a patch operation's path must be a string")
        _one(draft, op, parse_path(raw_path, shape=shape), value, has_value, shape)
    return draft


def _one(
    draft: dict[str, Any],
    op: str,
    path: Path,
    value: Any,
    has_value: bool,
    shape: Shape,
) -> None:
    """Apply one operation to `draft` in place."""
    if path.attribute not in shape.writable:
        raise PatchError(
            "mutability",
            f"{path.attribute!r} is read-only on this provider and cannot be "
            f"changed by a patch operation",
        )
    if op != "remove" and not has_value:
        raise PatchError("invalidValue", f"a patch {op} operation needs a value")
    if path.attribute in shape.multi_valued:
        _multi_valued(draft, op, path, value, shape)
        return
    if path.predicate is not None or path.sub_attribute is not None:
        raise PatchError(
            "invalidPath",
            f"{path.attribute!r} is a single-valued attribute and cannot carry a "
            "value filter or a sub-attribute",
        )
    if op == "remove":
        raise PatchError(
            "invalidValue",
            f"{path.attribute!r} cannot be removed; it has no meaningful absent "
            "state, so send a replace with the value you want",
        )
    draft[shape.key(path.attribute)] = value


def _elements(value: Any) -> list[dict[str, Any]]:
    """A patch value for a multi-valued attribute as a list of member objects.

    A bare string is accepted as `{"value": ...}` because directories send both
    spellings for group membership, and the two mean the same thing.
    """
    items = value if isinstance(value, list) else [value]
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            out.append(dict(item))
        elif isinstance(item, str):
            out.append({"value": item})
        else:
            raise PatchError(
                "invalidValue",
                f"a multi-valued attribute's element must be an object or a "
                f"string, not {type(item).__name__}",
            )
    return out


def _multi_valued(draft: dict[str, Any], op: str, path: Path, value: Any, shape: Shape) -> None:
    key = shape.key(path.attribute)
    existing: list[Any] = list(draft.get(key) or [])
    if op == "add":
        known = {_identity(item) for item in existing}
        for element in _elements(value):
            if _identity(element) not in known:
                existing.append(element)
                known.add(_identity(element))
        draft[key] = existing
        return
    if op == "remove":
        if path.predicate is None:
            draft[key] = []
            return
        # A filter that matches nothing removes nothing, deliberately: see the
        # module docstring. The state the client asked for already holds.
        draft[key] = select(path.predicate, existing, invert=True)
        return
    if path.predicate is None:
        draft[key] = _elements(value)
        return
    selected = select(path.predicate, existing)
    if not selected:
        raise PatchError(
            "noTarget",
            f"no element of {path.attribute!r} matches the value filter, so there "
            "is nothing to replace",
        )
    for item in selected:
        if path.sub_attribute is None:
            item.clear()
            item.update(_elements(value)[0])
        else:
            item[path.sub_attribute] = value
    draft[key] = existing


def _identity(element: Mapping[str, Any]) -> tuple[str, str]:
    """What makes two elements of a multi-valued attribute the same one.

    `value` is the primary key of every multi-valued attribute this provider
    publishes -- a group member's user id, an email address -- so an `add` of a
    member already present is a no-op rather than a duplicate row.
    """
    # Every element reaching here came through `_elements`, which turns a bare
    # string into `{"value": ...}`, or off a document this module produced --
    # so it is a mapping, and a second branch for the case that cannot arise
    # would be a claim nothing can check.
    marker = element.get("value")
    # Tagged, and `repr` for the untagged half: a member id is a string and is
    # matched case-insensitively, while anything else -- which `commit_group`
    # refuses before it can reach a store -- is compared by its written form, so
    # `7` and `"7"` stay two members and an unhashable value cannot make this
    # raise on a path a client controls.
    if isinstance(marker, str):
        return "text", marker.lower()
    return "other", repr(marker)


def replace(document: Mapping[str, Any], body: Any, *, shape: Shape) -> dict[str, Any]:
    """`document` with `body`'s writable attributes applied -- the `PUT` shape.

    Read-only and unknown attributes are **ignored rather than refused**, which
    is what section 3.5.1 requires: a directory sends the whole resource back,
    including the `meta` and `groups` it was given, and refusing a body for
    echoing what we served would fail every real client.

    A writable attribute the body omits is left as it reads today rather than
    cleared. `PUT` nominally replaces the whole resource, but the attributes
    this provider writes are exactly the ones with no coherent absent state, so
    "omitted" can only mean "unchanged" here.
    """
    if not isinstance(body, Mapping):
        raise PatchError("invalidSyntax", "a request body must be a JSON object")
    draft = deepcopy(dict(document))
    for name, value in body.items():
        if not isinstance(name, str):
            continue
        attribute = name.rpartition(":")[2].lower()
        if attribute not in shape.writable:
            continue
        draft[shape.key(attribute)] = _elements(value) if attribute in shape.multi_valued else value
    return draft
