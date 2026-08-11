"""Which functions need a session and where one can be threaded to them --
the call graph climbed to a fixed point, and the sites that follow from it."""

from __future__ import annotations

import ast
import builtins
import os
from collections import Counter
from pathlib import Path

from .imports import _Imports
from .nodes import parent_map
from .queries import (
    QUERY_TRANSLATED,
    chain_tail,
    plain_filter_mappings,
    query_chain_runs,
    query_rule,
)
from .routes import HTTP_METHODS
from .sources import _SKIPPABLE, _parse_file

#: Names a call site can carry without meaning the function of that name: every
#: builtin, plus the methods `dict`, `list`, `str`, `set` and a file answer to.
#: A repository is very likely to have its own `get`, `count` or `update`, and
#: matching by name would then rewrite every `payload.get(...)` in the tree.
_AMBIGUOUS_CALL_NAMES = frozenset(dir(builtins)) | frozenset({
    "get", "keys", "values", "items", "update", "copy", "pop", "setdefault",
    "append", "extend", "insert", "remove", "discard", "clear", "add",
    "count", "index", "sort", "reverse", "join", "split", "strip", "format",
    "encode", "decode", "read", "write", "close", "flush", "send", "seek",
    "startswith", "endswith", "replace", "lower", "upper", "title",
    "first", "last", "all", "any", "one", "run", "execute", "save", "delete",
})


def _function_query_names(
    tree: ast.Module,
    imports: _Imports,
    orm_columns: dict[str, set[str]],
    orm_relations: dict[str, dict[str, str]],
    orm_tables: dict[str, str],
    orm_unique_constraints: dict[str, tuple[frozenset[str], ...]],
) -> tuple[set[str], set[str]]:
    """`(functions that run a query, every function name defined here)`.

    Only the *determined* queries count: a chain this tool would not rewrite
    anyway is no reason to change a signature.
    """
    parents = parent_map(tree)
    runs: set[str] = set()
    defined: set[str] = set()
    enclosing: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            if isinstance(node, ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    enclosing.setdefault(id(child), node.name)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "objects"):
            continue
        call = parents.get(id(node))
        rule_id = query_rule(
            node.attr, call if isinstance(call, ast.Call) else None,
            chain_tail(node, parents),
            model=ast.unparse(node.value.value),
            relations=orm_relations,
            columns=orm_columns,
            tables=orm_tables,
            unique_constraints=orm_unique_constraints,
            plain_mappings=plain_filter_mappings(
                call if isinstance(call, ast.Call) else None, parents
            ),
        )
        owner = enclosing.get(id(node))
        if owner is not None and rule_id in QUERY_TRANSLATED and query_chain_runs(node, parents):
            runs.add(owner)
    return runs, defined


def _called_names(tree: ast.Module) -> dict[str, set[str]]:
    """`{async function name -> the plain function/method names it calls}`."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        called: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        out.setdefault(node.name, set()).update(called)
    return out


def _stub_definition_counts(tree: ast.Module) -> Counter[str]:
    """Count protocol/ABC declarations that intentionally have no body."""
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if len(body) != 1:
            continue
        statement = body[0]
        if isinstance(statement, ast.Pass):
            counts[node.name] += 1
        elif (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis
        ):
            counts[node.name] += 1
        elif (
            isinstance(statement, ast.Raise)
            and isinstance(statement.exc, ast.Call)
            and isinstance(statement.exc.func, ast.Name)
            and statement.exc.func.id == "NotImplementedError"
        ):
            counts[node.name] += 1
    return counts


def _annotation_names(annotation: ast.expr | None) -> frozenset[str]:
    """Class-name tails mentioned by one annotation, including unions/generics."""
    if annotation is None:
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return frozenset(names)


def session_functions(
    files: list[Path],
    on_skip=None,
    *,
    orm_columns: dict[str, set[str]] | None = None,
    orm_relations: dict[str, dict[str, str]] | None = None,
    orm_tables: dict[str, str] | None = None,
    orm_unique_constraints: dict[str, tuple[frozenset[str], ...]] | None = None,
) -> frozenset[str]:
    """Every function name that has to take a session, once it has spread.

    A function that runs a query needs one. So does anything that calls such a
    function, and anything that calls *that* — the requirement climbs the call
    graph until it reaches a route handler, where wreath supplies it. This is
    what `--opinionated` needs in order to finish the job: adding the parameter
    without updating the callers leaves a tree that imports and then fails on
    the first call.

    **Names, not resolved targets.** Working out that `repo.by_herd(...)` is
    `LlamaRepository.by_herd` needs type inference this tool does not have, so a
    method name is matched by name across the tree. The over-approximation is
    deliberate and it is one-directional: a name that matches gains a keyword
    argument, and since *every* definition of that name gains the parameter too,
    the pair stays consistent. What it cannot know is a same-named method on a
    third-party object, which is why this is opt-in and why the report says which
    functions were changed.
    """
    runs: set[str] = set()
    calls: dict[str, set[str]] = {}
    definitions: dict[str, int] = {}
    stubs: Counter[str] = Counter()
    for path in files:
        try:
            tree = _parse_file(path)
        except _SKIPPABLE as exc:
            if on_skip is not None:
                on_skip(path, exc)
            continue
        imports = _Imports().visit(tree)
        stubs.update(_stub_definition_counts(tree))
        module_runs, _module_defined = _function_query_names(
            tree,
            imports,
            orm_columns or {},
            orm_relations or {},
            orm_tables or {},
            orm_unique_constraints or {},
        )
        runs |= module_runs
        for candidate in ast.walk(tree):
            if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions[candidate.name] = definitions.get(candidate.name, 0) + 1
        for name, called in _called_names(tree).items():
            calls.setdefault(name, set()).update(called)
    # A unique definition is resolvable by name. The narrow duplicate case is a
    # protocol/ABC declaration plus its one implementation: both signatures
    # deliberately move together. Treating *every* repeated name as the same
    # target infected unrelated repositories across a large monorepo (including
    # abstract methods and lifecycle hooks) with session parameters.
    def usable(name: str) -> bool:
        count = definitions.get(name, 0)
        resolvable = count == 1 or (count == 2 and stubs[name] == 1 and name in runs)
        return resolvable and name not in _AMBIGUOUS_CALL_NAMES
    needs = {name for name in runs if usable(name)}
    changed = True
    while changed:                            # climb the call graph to a fixed point
        changed = False
        for name, called in calls.items():
            if name not in needs and usable(name) and called & needs:
                needs.add(name)
                changed = True
    return frozenset(needs)


def session_sites(
    files: list[Path],
    on_skip=None,
    *,
    orm_columns: dict[str, set[str]] | None = None,
    orm_relations: dict[str, dict[str, str]] | None = None,
    orm_tables: dict[str, str] | None = None,
    orm_unique_constraints: dict[str, tuple[frozenset[str], ...]] | None = None,
) -> tuple[frozenset[tuple[str, int]], frozenset[tuple[str, int, int]]]:
    """Exact definition and call sites whose resolved target needs a session.

    Method names are not identities. A large application can have dozens of
    unrelated ``get`` or ``create`` methods, so propagating by the final name
    mutates signatures that have no database call at all. This graph resolves
    the static cases the source actually states: module functions, ``self`` and
    class calls, annotated parameters, constructor-bound locals, and method
    overrides on a uniquely named base class. Anything it cannot resolve stays
    out of the graph and remains a visible ``needs_session`` finding.
    """
    FunctionKey = tuple[str, str | None, str]
    ClassKey = tuple[str, str]
    common_root = Path(
        os.path.commonpath([str(path.resolve()) for path in files])
        if files
        else os.curdir
    )
    if common_root.suffix == ".py":
        common_root = common_root.parent

    trees: dict[str, tuple[ast.Module, _Imports, dict[int, ast.AST]]] = {}
    definitions: dict[FunctionKey, ast.AsyncFunctionDef] = {}
    function_keys: dict[tuple[str, int], FunctionKey] = {}
    classes: dict[ClassKey, ast.ClassDef] = {}
    class_bases: dict[ClassKey, frozenset[str]] = {}

    def enclosing_class(path: str, node: ast.AST, parents: dict[int, ast.AST]) -> str | None:
        current = parents.get(id(node))
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
            if isinstance(current, ast.ClassDef):
                return current.name
            current = parents.get(id(current))
        return None

    for raw_path in files:
        path = str(raw_path.resolve())
        try:
            tree = _parse_file(raw_path)
        except _SKIPPABLE as exc:
            if on_skip is not None:
                on_skip(raw_path, exc)
            continue
        imports = _Imports().visit(tree)
        parents = parent_map(tree)
        trees[path] = (tree, imports, parents)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                key = (path, node.name)
                classes[key] = node
                class_bases[key] = frozenset(
                    name
                    for base in node.bases
                    for name in _annotation_names(base)
                )
            elif isinstance(node, ast.AsyncFunctionDef):
                owner = enclosing_class(path, node, parents)
                key = (path, owner, node.name)
                definitions[key] = node
                function_keys[(path, id(node))] = key

    classes_by_name: dict[str, list[ClassKey]] = {}
    for key in classes:
        classes_by_name.setdefault(key[1], []).append(key)
    definitions_by_name: dict[str, list[FunctionKey]] = {}
    for key in definitions:
        definitions_by_name.setdefault(key[2], []).append(key)

    direct: set[FunctionKey] = set()
    edges: dict[FunctionKey, list[tuple[ast.Call, frozenset[FunctionKey]]]] = {}
    overrides: dict[FunctionKey, set[FunctionKey]] = {}
    injectable: set[FunctionKey] = set()

    for key, function in definitions.items():
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in HTTP_METHODS
            for decorator in function.decorator_list
        ) or any(
            "Session" in _annotation_names(argument.annotation)
            for argument in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
        ):
            injectable.add(key)

    for class_key, base_names in class_bases.items():
        path, class_name = class_key
        own = {
            key[2]: key
            for key in definitions
            if key[0] == path and key[1] == class_name
        }
        for base_name in base_names:
            candidates = classes_by_name.get(base_name, ())
            if len(candidates) != 1:
                continue
            base_path, resolved_base = candidates[0]
            inherited = {
                key[2]: key
                for key in definitions
                if key[0] == base_path and key[1] == resolved_base
            }
            for name in own.keys() & inherited.keys():
                overrides.setdefault(own[name], set()).add(inherited[name])
                overrides.setdefault(inherited[name], set()).add(own[name])

    for path, (tree, _imports, parents) in trees.items():
        module_functions = {
            key[2]: key
            for key in definitions
            if key[0] == path and key[1] is None
        }
        local_classes = {key[1]: key for key in classes if key[0] == path}
        class_attributes: dict[str, dict[str, frozenset[str]]] = {}
        receiver_types: dict[FunctionKey, dict[str, frozenset[str]]] = {}

        for key, function in definitions.items():
            if key[0] != path:
                continue
            types = {
                argument.arg: _annotation_names(argument.annotation)
                for argument in (
                    *function.args.posonlyargs,
                    *function.args.args,
                    *function.args.kwonlyargs,
                )
                if argument.annotation is not None
            }
            for statement in ast.walk(function):
                if not (
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and statement.value is not None
                    and isinstance(statement.value, ast.Call)
                ):
                    continue
                target = (
                    statement.target
                    if isinstance(statement, ast.AnnAssign)
                    else statement.targets[0] if len(statement.targets) == 1 else None
                )
                if isinstance(target, ast.Name):
                    types[target.id] = _annotation_names(statement.value.func)
            receiver_types[key] = types

            if key[1] is None or function.name != "__init__":
                continue
            attrs = class_attributes.setdefault(key[1], {})
            for statement in ast.walk(function):
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                target = (
                    statement.target
                    if isinstance(statement, ast.AnnAssign)
                    else statement.targets[0] if len(statement.targets) == 1 else None
                )
                value = statement.value
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and isinstance(value, ast.Name)
                    and value.id in types
                ):
                    continue
                attrs[target.attr] = types[value.id]

        def owner_function(
            node: ast.AST,
            *,
            _parents: dict[int, ast.AST] = parents,
            _path: str = path,
        ) -> FunctionKey | None:
            current = _parents.get(id(node))
            while current is not None:
                if isinstance(current, ast.AsyncFunctionDef):
                    return function_keys.get((_path, id(current)))
                if isinstance(current, ast.FunctionDef):
                    return None
                current = _parents.get(id(current))
            return None

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "objects"
            ):
                continue
            owner = owner_function(node)
            if owner is None:
                continue
            call = parents.get(id(node))
            rule_id = query_rule(
                node.attr,
                call if isinstance(call, ast.Call) else None,
                chain_tail(node, parents),
                model=ast.unparse(node.value.value),
                relations=orm_relations or {},
                columns=orm_columns or {},
                tables=orm_tables or {},
                unique_constraints=orm_unique_constraints or {},
                plain_mappings=plain_filter_mappings(
                    call if isinstance(call, ast.Call) else None, parents
                ),
            )
            if rule_id in QUERY_TRANSLATED and query_chain_runs(node, parents):
                direct.add(owner)

        def methods_for(class_names: frozenset[str], method: str) -> set[FunctionKey]:
            resolved: set[FunctionKey] = set()
            for class_name in class_names:
                candidates = classes_by_name.get(class_name, ())
                if len(candidates) != 1:
                    continue
                class_path, owner_name = candidates[0]
                key = (class_path, owner_name, method)
                if key in definitions:
                    resolved.add(key)
            return resolved

        for call in (candidate for candidate in ast.walk(tree) if isinstance(candidate, ast.Call)):
            caller = owner_function(call)
            if caller is None:
                continue
            targets: set[FunctionKey] = set()
            func = call.func
            if isinstance(func, ast.Name):
                local = module_functions.get(func.id)
                if local is not None:
                    targets.add(local)
                elif len(definitions_by_name.get(func.id, ())) == 1:
                    targets.add(definitions_by_name[func.id][0])
            elif isinstance(func, ast.Attribute):
                receiver = func.value
                if isinstance(receiver, ast.Name):
                    if receiver.id in {"self", "cls"} and caller[1] is not None:
                        target = (path, caller[1], func.attr)
                        if target in definitions:
                            targets.add(target)
                    elif receiver.id in local_classes:
                        target = (path, receiver.id, func.attr)
                        if target in definitions:
                            targets.add(target)
                    else:
                        targets.update(
                            methods_for(
                                receiver_types.get(caller, {}).get(
                                    receiver.id, frozenset()
                                ),
                                func.attr,
                            )
                        )
                elif (
                    isinstance(receiver, ast.Attribute)
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id in {"self", "cls"}
                    and caller[1] is not None
                ):
                    targets.update(
                        methods_for(
                            class_attributes.get(caller[1], {}).get(
                                receiver.attr, frozenset()
                            ),
                            func.attr,
                        )
                    )
                elif isinstance(receiver, ast.Call):
                    targets.update(
                        methods_for(_annotation_names(receiver.func), func.attr)
                    )
                if not targets and len(definitions_by_name.get(func.attr, ())) == 1:
                    targets.add(definitions_by_name[func.attr][0])
            if targets:
                edges.setdefault(caller, []).append((call, frozenset(targets)))

    needs = set(direct)
    changed = True
    while changed:
        changed = False
        for key in tuple(needs):
            for peer in overrides.get(key, ()):
                if peer not in needs:
                    needs.add(peer)
                    changed = True
        for caller, calls in edges.items():
            if caller not in needs and any(targets & needs for _call, targets in calls):
                needs.add(caller)
                changed = True

    # A signature change is only complete when the requirement reaches a place
    # Wreath already supplies a session: a route (or a function that already
    # declares one). A standalone task, test, startup hook, or library method
    # has no such boundary. Propagating into it merely replaces one TODO with a
    # missing argument, so keep that whole branch visible instead.
    def is_test_path(path: str) -> bool:
        candidate = Path(path)
        try:
            relative = candidate.relative_to(common_root)
        except ValueError:
            relative = candidate
        return (
            candidate.name.startswith("test_")
            or candidate.name == "conftest.py"
            or any(part in {"test", "tests"} for part in relative.parts)
        )

    blocked_by_test = {
        target
        for caller, calls in edges.items()
        if is_test_path(caller[0])
        for _call, targets in calls
        for target in targets
    }
    supported = {
        key
        for key in needs & injectable
        if not is_test_path(key[0]) and key not in blocked_by_test
    }
    inbound = {
        target
        for calls in edges.values()
        for _call, targets in calls
        for target in targets
    }
    supported.update(
        key
        for key in direct
        if key not in inbound
        and not is_test_path(key[0])
        and key not in blocked_by_test
    )
    changed = True
    while changed:
        changed = False
        for caller in tuple(supported):
            # `called` rather than `targets`: the name is already bound to a
            # `set` at function scope above, and rebinding it here to the
            # `frozenset` the edge carries is a type error rather than a shadow.
            for _call, called in edges.get(caller, ()):
                for target in called & needs:
                    if (
                        target not in supported
                        and not is_test_path(target[0])
                        and target not in blocked_by_test
                    ):
                        supported.add(target)
                        changed = True
            for peer in overrides.get(caller, ()):
                if (
                    peer in needs
                    and peer not in supported
                    and not is_test_path(peer[0])
                    and peer not in blocked_by_test
                ):
                    supported.add(peer)
                    changed = True
    needs = supported

    definition_sites = frozenset(
        (key[0], definitions[key].lineno) for key in needs
    )
    call_sites = frozenset(
        (caller[0], call.lineno, call.col_offset)
        for caller, calls in edges.items()
        if caller in needs
        for call, targets in calls
        if targets & needs
    )
    return definition_sites, call_sites
