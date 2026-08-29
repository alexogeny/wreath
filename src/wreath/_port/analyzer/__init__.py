"""Static (never-import-the-target) FastAPI/Pydantic/ormar/SQLModel analyzer.

Design 07's load-bearing constraint: the source cannot be imported (private deps,
import-time side effects), so this walks `ast` only. Two passes: (1) index every
module's classes by framework base across the whole tree so body-params and query
targets resolve cross-module; (2) classify constructs into findings.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from ..detect import Detection, ModuleSignals, scan_module
from ..foreign import foreign_findings
from ..ir import Finding, Report, SkippedFile
from .background import celery_enqueue_rule, celery_runner_names, celery_task_rule
from .django import DjangoImage, django_image
from .imports import _Imports
from .models import (
    _config_extra,
    _plain_graphql_dataclass,
    dataclass_needs_kw_only,
    pydantic_field_rule,
    pydantic_projection_rule,
    redundant_literal_validator,
)
from .nodes import _is_false, _is_true, parent_map
from .orm import _base_kind, _index_tree, module_pk_types, tree_pk_types
from .queries import (
    _NULL_METHOD,
    LOOKUP_METHOD,
    LOOKUP_OPERATOR,
    _eager_paths,
    _order_argument_is_mechanical,
    _pagination_values,
    _projection_names,
    _resolved_column_path,
    chain_tail,
    plain_filter_mappings,
    query_rule,
    split_lookup,
)
from .responses import (
    STATUS_EXCEPTION,
    _returns_in,
    http_exception_rule,
    http_exception_status,
    response_class_rule,
    status_code_rule,
    status_int,
)
from .routes import HTTP_METHODS, lifespan_names
from .scan import _Analyzer
from .sessions import session_functions, session_sites
from .settings import settings_class_rule
from .sources import _SKIPPABLE, _iter_py, _parse_file, _relative_to, _skip_detail, _skip_reason


@dataclass(frozen=True)
class TreeContext:
    """What one module needs to know about the rest of the tree to be ported well.

    A module on its own cannot see that `Llama`'s primary key is a UUID, that
    `NewLlama` is a body model rather than a query parameter, or that a GraphQL
    type lists exactly the columns of the model it shadows. Every one of those
    changes the verdict, so `port_tree` reads the tree once and hands the answers
    to each file.
    """

    pk_types: dict[str, str] = dataclass_field(default_factory=dict)
    index: dict[str, set[str]] = dataclass_field(
        default_factory=lambda: {
            "pydantic": set(),
            "settings": set(),
            "orm": set(),
            "orm_mixin": set(),
        }
    )
    orm_columns: dict[str, set[str]] = dataclass_field(default_factory=dict)
    orm_relations: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)
    orm_tables: dict[str, str] = dataclass_field(default_factory=dict)
    orm_unique_constraints: dict[str, tuple[frozenset[str], ...]] = dataclass_field(
        default_factory=dict
    )
    positional_model_calls: frozenset[str] = frozenset()
    #: Function names that have to take a session once the requirement has
    #: climbed the call graph. Only `--opinionated` acts on it, because acting on
    #: it changes signatures and call sites across the whole tree.
    session_functions: frozenset[str] = frozenset()
    session_definition_sites: frozenset[tuple[str, int]] = frozenset()
    session_call_sites: frozenset[tuple[str, int, int]] = frozenset()
    session_sites_resolved: bool = False
    #: Every class name in the tree -> the names of its bases, unresolved.
    #: Foreign-framework handler hierarchies cross module boundaries, so
    #: deciding "is this a RequestHandler" needs the whole tree, not one file.
    class_bases: dict[str, list[str]] = dataclass_field(default_factory=dict)
    #: Class name -> the names it declares itself, for telling an inherited
    #: framework method from one this codebase wrote.
    class_members: dict[str, set[str]] = dataclass_field(default_factory=dict)
    #: Which Django models this tree declares and which of them are plain. A
    #: `.objects` chain is classified against this rather than against the
    #: querying module's own imports -- the manager belongs to the model.
    django: DjangoImage = dataclass_field(default_factory=DjangoImage)

    @classmethod
    def of(
        cls,
        files: list[Path],
        on_skip=None,
        *,
        opinionated: bool = False,
        trees: Mapping[Path, ast.Module] | None = None,
    ) -> TreeContext:
        (
            index,
            orm_columns,
            orm_relations,
            orm_tables,
            orm_unique_constraints,
            positional_calls,
            class_bases,
            class_members,
        ) = _index_tree(files, on_skip=on_skip, trees=trees)
        # A Django model is an ORM model: its columns and relations answer the
        # same questions ormar's do, and the query rules read them by the same
        # names. Merged rather than kept apart so one rewrite path serves both.
        image = django_image(files, on_skip=on_skip, trees=trees)
        orm_columns = {**image.columns, **orm_columns}
        orm_relations = {**image.relations, **orm_relations}
        orm_tables = {**image.tables, **orm_tables}
        selected_names = (
            session_functions(
                files,
                on_skip=on_skip,
                orm_columns=orm_columns,
                orm_relations=orm_relations,
                orm_tables=orm_tables,
                orm_unique_constraints=orm_unique_constraints,
            )
            if opinionated
            else frozenset()
        )
        definition_sites, call_sites = (
            session_sites(
                files,
                on_skip=on_skip,
                orm_columns=orm_columns,
                orm_relations=orm_relations,
                orm_tables=orm_tables,
                orm_unique_constraints=orm_unique_constraints,
            )
            if opinionated
            else (frozenset(), frozenset())
        )
        return cls(
            tree_pk_types(files, on_skip=on_skip, trees=trees),
            index,
            orm_columns,
            orm_relations,
            orm_tables,
            orm_unique_constraints,
            frozenset(positional_calls),
            selected_names,
            definition_sites,
            call_sites,
            opinionated,
            class_bases,
            class_members,
            image,
        )


def module_findings(
    path: Path, root: Path, tree: ast.Module, imports: _Imports, context: TreeContext
) -> list[Finding]:
    """Every finding for one already-parsed module.

    Shared with the emitter. The emitter used to carry its own copy of each
    detector, and the two drifted apart exactly as you would expect: 23 rules
    and 794 findings appeared in the report and nowhere in the ported files, so
    a porter reading their own code saw no sign of 160 hand-written SQL
    migrations or 87 pandas modules. Deriving both from this makes that class of
    gap impossible rather than merely fixed.
    """
    analyzer = _Analyzer(
        path,
        root,
        imports,
        context.index,
        {**context.pk_types, **module_pk_types(tree, imports)},
        context.orm_columns,
        context.orm_relations,
        context.orm_tables,
        context.orm_unique_constraints,
        context.positional_model_calls,
        context.django,
    )
    if imports.has_star:
        analyzer._emit("resolve.star_import", 1)
    analyzer.visit(tree)
    # Foreign-framework constructs come from the same call as everything else,
    # because the emitter builds its notes from this list. Kept outside it, a
    # Flask module reported forty-four findings and the ported file carried
    # none -- the exact drift `module_findings` exists to make impossible, in
    # the one family where the reader has no other clue what to do next.
    return analyzer.findings + foreign_findings(
        _relative_to(Path(path), Path(root)),
        tree,
        imports.roots,
        context.class_bases,
        imports,
        context.class_members,
    )


def analyze(root) -> Report:
    """Analyze a single app root (directory or file) and return its Report.

    **One bad file is recorded and skipped, never fatal.** A 3000-file tree
    reliably contains a broken symlink, a file whose permission bit says no, a
    file deleted between the walk and the read, a null byte in a "`.py`" that
    is really a fixture, and an expression nested past the parser's limit. Each
    of those takes its own file out of the run and leaves the rest in, and each
    lands in `Report.skipped` with a reason — a silently dropped file is
    indistinguishable from a file with nothing in it, and the coverage number is
    computed from exactly this population.

    `KeyboardInterrupt` and `SystemExit` derive from `BaseException` and
    are deliberately *not* caught: a run the user asked to stop must stop.
    """
    root = Path(root)
    skipped: dict[str, SkippedFile] = {}

    def record(target, exc: BaseException) -> None:
        key = _relative_to(Path(target), root)  # same spelling Findings use
        # First reason wins: the same file is read twice (index pass, then
        # analysis pass) and would otherwise be reported twice.
        skipped.setdefault(key, SkippedFile(key, _skip_reason(exc), _skip_detail(exc)))

    files = list(_iter_py(root, on_error=lambda exc: record(exc.filename or root, exc)))
    trees: dict[Path, ast.Module] = {}
    for path in files:
        try:
            trees[path] = _parse_file(path)
        except _SKIPPABLE as exc:
            record(path, exc)
    context = TreeContext.of(files, on_skip=record, trees=trees)
    findings: list[Finding] = []
    signals: dict[str, ModuleSignals] = {}
    analyzed = 0
    for path in files:
        tree = trees.get(path)
        if tree is None:
            continue
        try:
            imports = _Imports().visit(tree)
            found = module_findings(path, root, tree, imports, context)
        except _SKIPPABLE as exc:
            # Partial findings from a half-visited file are discarded with it:
            # half a module's constructs is a worse denominator than none.
            record(path, exc)
            continue
        analyzed += 1
        findings.extend(found)
        rel = _relative_to(Path(path), root)
        # Detection reads the same parse, so naming the stack costs no extra I/O.
        signals[rel] = scan_module(tree)
    return Report(
        findings,
        roots=[str(root)],
        skipped=list(skipped.values()),
        files_analyzed=analyzed,
        detection=Detection.of(signals),
    )


def analyze_all(roots) -> Report:
    """Analyze several app roots (a glob of apps, design 07 §5) into one Report."""
    return Report.merge([analyze(r) for r in roots])


def detect_roots(roots) -> Detection | None:
    """Name the stack without running any rule — emit mode's pre-flight.

    Emit does not otherwise analyze, so it used to write a full ported tree for
    an application in a framework this tool does not translate without ever
    saying so. Reading imports is the cheapest question that catches it.
    """
    parts = []
    for raw_root in roots:
        root = Path(raw_root)
        signals: dict[str, ModuleSignals] = {}
        for path in _iter_py(root, on_error=lambda exc: None):
            try:
                tree = _parse_file(path)
            except _SKIPPABLE:
                # A file emit cannot read is emit's problem to report, not
                # detection's; here it simply does not vote on the framework.
                continue
            signals[_relative_to(Path(path), root)] = scan_module(tree)
        parts.append(Detection.of(signals))
    return Detection.merge(parts)


#: What the rest of `_port` imports from this package.
__all__ = [
    "HTTP_METHODS",
    "DjangoImage",
    "LOOKUP_METHOD",
    "LOOKUP_OPERATOR",
    "STATUS_EXCEPTION",
    "TreeContext",
    "_Imports",
    "_NULL_METHOD",
    "_SKIPPABLE",
    "_base_kind",
    "_config_extra",
    "_eager_paths",
    "_is_false",
    "_is_true",
    "_iter_py",
    "_order_argument_is_mechanical",
    "_pagination_values",
    "_parse_file",
    "_plain_graphql_dataclass",
    "_projection_names",
    "_relative_to",
    "_resolved_column_path",
    "_returns_in",
    "_skip_detail",
    "_skip_reason",
    "analyze",
    "analyze_all",
    "celery_enqueue_rule",
    "celery_runner_names",
    "celery_task_rule",
    "chain_tail",
    "dataclass_needs_kw_only",
    "detect_roots",
    "django_image",
    "http_exception_rule",
    "http_exception_status",
    "lifespan_names",
    "module_findings",
    "module_pk_types",
    "parent_map",
    "plain_filter_mappings",
    "pydantic_field_rule",
    "pydantic_projection_rule",
    "query_rule",
    "redundant_literal_validator",
    "response_class_rule",
    "settings_class_rule",
    "split_lookup",
    "status_code_rule",
    "status_int",
]
