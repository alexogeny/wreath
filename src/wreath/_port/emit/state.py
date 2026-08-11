"""Everything one emit run carries, and the edits every layer makes through it.

The state is here because every layer above reads it. Those layers are the
domains -- one module each -- and they are assembled into `_Emitter` by
`.emitter`; nothing here knows about any of them."""

from __future__ import annotations

import ast

from ..analyzer import DjangoImage, _Imports, status_int
from ..ir import TRANSLATED
from ..rules import RULES
from .buffer import _Buffer, _Positioned
from .targets import _RENAMED_ORIGINS, _RETAINED_ORIGINS


class _EmitterState(ast.NodeVisitor):
    def __init__(
        self,
        source: str,
        imports: _Imports,
        pk_types: dict[str, str] | None = None,
        *,
        opinionated: bool = False,
    ) -> None:
        self.buf = _Buffer(source)
        self.src = source
        self.imports = imports
        self.opinionated = opinionated
        self.session_functions: frozenset[str] = frozenset()
        self.session_definition_sites: frozenset[tuple[str, int]] = frozenset()
        self.session_call_sites: frozenset[tuple[str, int, int]] = frozenset()
        self.session_sites_resolved = False
        self._source_path = ""
        self.pk_types = pk_types or {}  # ORM model name -> PK PgType (FK inference)
        self.orm_columns: dict[str, set[str]] = {}
        self.orm_relations: dict[str, dict[str, str]] = {}
        self.orm_tables: dict[str, str] = {}
        self.orm_unique_constraints: dict[str, tuple[frozenset[str], ...]] = {}
        self.orm_mixins: frozenset[str] = frozenset()
        # The same tree-wide manager answer the report was written from, so a
        # rewrite and its finding cannot disagree about one `.objects` chain.
        self.django = DjangoImage()
        self.pydantic_models: frozenset[str] = frozenset()
        self.settings_models: frozenset[str] = frozenset()
        self._settings_custom_init: frozenset[str] = frozenset()
        self._settings_bindings: dict[str, tuple[str | None, str | None]] = {}
        self._http_clients: dict[int, tuple[str, ast.expr | None]] = {}
        self._http_client_calls: dict[int, int] = {}
        self._http_requests: dict[int, int] = {}
        self._http_dynamic_clients: dict[int, ast.expr] = {}
        self._http_url_parts: dict[int, str] = {}
        self._http_request_timeouts: dict[int, ast.expr] = {}
        self._http_retries: dict[int, ast.expr] = {}
        self._http_transport_assignments: set[int] = set()
        self._http_timeout_constants: set[str] = set()
        self._http_responses: set[tuple[int | None, str]] = set()
        self.needs_urlencode = False
        self._global_test_clients: dict[str, ast.Call] = {}
        self._fixture_test_clients: set[str] = set()
        self.needs_fixture = False
        self._pydantic_partial_family: frozenset[str] = frozenset()
        self.needs: set[str] = set()  # extra `from wreath import` names
        self._from_fastapi_wreath: set[str] = set()  # names already on the rewritten fastapi import
        self.needs_annotated = False  # `from typing import Annotated`
        self.needs_dataclass = False  # `from dataclasses import dataclass`
        # `field` is imported separately from the decorator, because it is only
        # needed for a mutable default and it is an ordinary English word: three
        # real modules use `field` as a loop variable, and importing it there
        # shadowed their own name.
        self.needs_field = False  # `field` (a default_factory)
        self.needs_uuid = False  # plain `import uuid` (a Uuid FK annotation)
        self._celery_runners: set[str] = set()
        # Task function name -> the runner its decorator named. Collected in
        # the module pre-scan because `.delay(...)` is routinely written above
        # the task it enqueues.
        self._celery_task_runners: dict[str, str] = {}
        self.needs_datetime = False  # a Date/TimestampTz column's annotation
        self.needs_decimal = False  # a Numeric column's annotation
        self.needs_temporal = False  # `from wreath import temporal`
        # Names the import rewrite must NOT drop, because a reference to them
        # survived the visit. A codemod that deletes an import whose name is
        # still used produces a module that imports nothing and runs nowhere:
        # `Field`, `HTTPException` and `BaseModel` all went missing this way, and
        # every module it happened to parsed, compiled, and then raised
        # `NameError` on import. Filled by `visit_Name`
        # and `visit_Attribute`, which is why `rewrite_imports` now runs *after*
        # the walk rather than before it.
        self._retain: set[str] = set()
        # The name a session is reachable under in the function being walked,
        # and whether a query wanted one and could not have it.
        self._session: str | None = None
        self._session_wanted = False
        # Byte spans replaced whole, so nothing inside one is edited again.
        self._replaced: list[tuple[int, int]] = []
        # `id(node)` of every name node subsumed by a rewritten span, so a
        # reference the emitter already dealt with does not also retain its
        # import. Marked at each rewrite site, read by `visit_Name`.
        self._rewritten: set[int] = set()
        self.annotated_lines: set[tuple[int, str]] = set()  # (line, rule_id) dedupe
        self._dep_targets: set[str] = set()  # function names referenced by Depends(<name>)
        self._claimed_objects: set[int] = set()  # `.objects` billed by its verb
        # Same parent map the analyzer builds, for the same reason: the verdict
        # a query gets depends on its arguments and on the verbs chained after
        # it, and neither is visible from the head node alone.
        self._parents: dict[int, ast.AST] = {}
        # Names handed to an application as `lifespan=`; filled by `visit_Module`,
        # since the `FastAPI(lifespan=...)` call sits below the `def` it names.
        self._lifespan_names: frozenset[str] = frozenset()
        self._test_clients: frozenset[str] = frozenset()
        self._removed_pydantic_imports: set[str] = set()
        self._removed_middleware_imports: set[str] = set()
        self._used_names: set[str] = set()

    # -- helpers -----------------------------------------------------------------
    def _seg(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.src, node) or ""

    def _enclosing_callable_id(self, node: ast.AST) -> int | None:
        owner = self._parents.get(id(node))
        while owner is not None:
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return id(owner)
            owner = self._parents.get(id(owner))
        return None

    def _enclosing_is_async(self, node: ast.AST) -> bool:
        """Whether the function this node sits in can hold an `await`."""
        owner = self._parents.get(id(node))
        while owner is not None and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            owner = self._parents.get(id(owner))
        return isinstance(owner, ast.AsyncFunctionDef)

    def _fresh_name(self, preferred: str) -> str:
        """Reserve a local temporary without shadowing a source identifier."""
        name = preferred
        suffix = 2
        while name in self._used_names:
            name = f"{preferred}_{suffix}"
            suffix += 1
        self._used_names.add(name)
        return name

    def _annotate(self, line: int, rule_id: str, extra: str = "") -> None:
        """Write the rule's own wording, with a detail in brackets after it."""
        _c, _cat, tag, message = RULES[rule_id]
        if extra:
            message = f"{message} ({extra})"
        self._annotate_message(line, rule_id, tag, message)

    def _resolve(self, line: int, rule_id: str) -> None:
        """Record that this line's finding was *acted on*, so no note is written.

        The shared pass writes a note for every finding the report calls
        needs-review, which is right until `--opinionated` settles one itself:
        the file would then carry both the decision and the question about it.
        """
        self.annotated_lines.add((line, rule_id))

    def _note(self, line: int, rule_id: str, text: str) -> None:
        """Write `text` *instead of* the rule's wording, under the same rule id.

        For the cases where the emitter knows something specific enough that the
        general sentence in front of it is noise — "this column stored a UUID as
        text" says everything, and prefixing it with the catalog's description of
        every ormar column does not help anyone read it.
        """
        self._annotate_message(line, rule_id, RULES[rule_id][2], text)

    def _annotate_message(self, line: int, rule_id: str, tag: str, message: str) -> None:
        key = (line, rule_id)
        if key in self.annotated_lines:
            return
        self.annotated_lines.add(key)
        indent = self.buf.line_indent(line)
        # A note is one comment line, so it cannot contain a newline. Quoting a
        # multi-line construct back at the reader put one in, and the comment
        # then swallowed the code under it — a file that would not parse.
        self.buf.insert_before_line(
            line, f"{indent}# TODO(wreath-port: [{tag}] {' '.join(message.split())} [{rule_id}])"
        )

    def annotate_findings(self, findings) -> None:
        """Write a note above every construct the report says needs a person.

        The emitter rewrites what it can and this covers the rest, straight from
        the analyzer's own verdicts — so the file a porter opens carries the same
        list the report prints, with each note sitting on the line it is about.

        Only `needs-review` and `unsupported` findings become notes. A translated
        verdict means the answer is already decided, and marking 1,078 derivable
        Alembic operations "done" would bury the 160 that are not.
        """
        for finding in findings:
            if finding.tag == TRANSLATED:
                continue
            self._annotate_message(finding.line, finding.rule_id, finding.tag, finding.message)

    # -- dependencies (Phase 3): a function referenced by Depends(<name>) gains a
    # leading `request: Request` param, exactly like a route handler.
    def collect_dep_targets(self, tree: ast.Module) -> None:
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and self.imports.origin(n.func).split(".")[-1] == "Depends":
                if n.args and isinstance(n.args[0], ast.Name):
                    self._dep_targets.add(n.args[0].id)

    def _remove_function_parameter(self, node, parameter: ast.arg) -> None:
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        index = arguments.index(parameter)
        if index + 1 < len(arguments):
            start = self.buf.start_of(parameter)
            end = self.buf.start_of(arguments[index + 1])
        elif index:
            start = self.buf.end_of(arguments[index - 1])
            end = self.buf.end_of(parameter)
        else:
            start = self.buf.start_of(parameter)
            end = self.buf.end_of(parameter)
        self.buf._edits.append((start, end, b""))
        self._rewritten.update(id(item) for item in ast.walk(parameter))

    def _delete_decorator(self, dec) -> None:
        """Remove a complete decorator, including a multiline argument list."""
        self._rewritten.update(id(item) for item in ast.walk(dec))
        start = self.buf._starts[dec.lineno - 1]
        end_line = dec.end_lineno or dec.lineno
        line_start = self.buf._starts[end_line - 1]
        nxt = self.buf.b.find(b"\n", line_start)
        end = (nxt + 1) if nxt != -1 else len(self.buf.b)
        self.buf._edits.append((start, end, b""))

    def visit_Name(self, node: ast.Name) -> None:
        """Rename or retain one bare reference to a framework name.

        Every mention counts, not only the one being called. `FastAPI` appears
        as an annotation far more often than as a constructor, and `HTTPException`
        appears in an `except` clause and an exception-handler registration where
        there is no call to rewrite at all.
        """
        self._track_reference(node, self.imports.origin(node))
        self.generic_visit(node)

    def _track_reference(self, node: _Positioned, origin: str) -> None:
        if id(node) in self._rewritten or self._inside_replaced(node):
            return
        wreath_name = _RENAMED_ORIGINS.get(origin)
        if wreath_name is not None:
            self._rewritten.update(id(item) for item in ast.walk(node))
            self.needs.add(wreath_name)
            if wreath_name != origin.split(".")[-1]:
                self.buf.replace(node, wreath_name)
        elif origin in _RETAINED_ORIGINS:
            # Keyed by the name *this module* uses. `from fastapi import status
            # as fastapistatus` is legal, and retaining "status" would have kept
            # an import nothing referred to while dropping the one that mattered.
            self._retain.add(node.id if isinstance(node, ast.Name) else origin.split(".")[-1])
        elif origin.startswith("strawberry") and isinstance(node, ast.Name):
            self._retain.add(node.id)
        elif (
            status := status_int(self.imports, node if isinstance(node, ast.expr) else None)
        ) is not None and isinstance(node, ast.Attribute):
            # `status.HTTP_404_NOT_FOUND` is an integer with a long name, and
            # wreath has no such module, and the number is what the reader
            # already has in mind anyway.
            self._rewritten.add(id(node))
            self._replace_all_of(node, str(status))

    def _inside_replaced(self, node: _Positioned) -> bool:
        """Whether this node sits inside a span already replaced wholesale.

        Edits are applied from the end of the file backwards and an overlapping
        one is dropped, so an inner edit queued after an outer one *wins* — the
        rewritten `HTTPException(...)` would be thrown away in favour of an
        integer written into the call it replaced. Nothing inside a replaced
        span is worth editing, so nothing inside one is.
        """
        start = self.buf.start_of(node)
        return any(low <= start < high for low, high in self._replaced)

    def _replace_all_of(self, node: _Positioned, text: str) -> None:
        """Replace a whole construct, and remember that its insides are gone."""
        if self._would_empty_a_block(node):
            # A comment is not a statement. Removing the last real statement out
            # of a class body leaves `class Row(BaseModel):` with nothing under
            # it, and the module stops parsing -- which aborts the whole tree,
            # because `port_tree` raises on the first invalid module. Rules that
            # are right about the statement should not have to know they were
            # the last one, so the guard lives here rather than at each site.
            indent = self.buf.line_indent(node.lineno)
            text = f"{text}\n{indent}pass" if text else "pass"
        self._replaced.append((self.buf.start_of(node), self.buf.end_of(node)))
        self.buf.replace(node, text)

    def _would_empty_a_block(self, node: _Positioned) -> bool:
        """Is `node` the last statement standing in the class body that owns it?

        Only class bodies: a function body emptied this way has not come up, and
        answering "yes" for an expression that merely *sits* inside a class would
        insert `pass` in the middle of a line. Hence the identity check against
        `owner.body` -- being descended from a ClassDef is not the question.

        Siblings already replaced do not count as body: two config statements in
        one class are removed one after the other, and the second is as alone as
        the first was.
        """
        owner = self._parents.get(id(node))
        if not isinstance(owner, ast.ClassDef):
            return False
        if not any(sibling is node for sibling in owner.body):
            return False
        for sibling in owner.body:
            if sibling is node:
                continue
            start, end = self.buf.start_of(sibling), self.buf.end_of(sibling)
            if any(lo <= start and end <= hi for lo, hi in self._replaced):
                continue
            return False
        return True
