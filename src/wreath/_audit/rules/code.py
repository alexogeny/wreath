"""Source-level security rules — the tier that reads the application's own code.

The other two tiers look at what an application *emits*: rendered HTML, live
response headers. Neither can see the defect classes that do the real damage,
because those leave no trace in a correct-looking 200. A query built by string
formatting, a signing key that is a literal, a comparison that returns early on
the first wrong byte -- all of them serve a perfectly ordinary response.

So this tier parses the source instead. It is a **curated** ruleset, not a
general-purpose linter: every rule here corresponds to a defect class that was
planted in a red-team range against a Wreath application and captured, and
every one of them has a safe spelling that Wreath already ships. That framing
is what makes the suggestions worth reading -- a finding does not say "this is
dangerous", it says "you wrote X; the primitive for this is Y".

## Precision over recall, deliberately

A security linter that cries wolf gets suppressed wholesale, and then it is
worse than nothing: the suppression outlives the person who understood it.
So each rule is written to be quiet on the correct form of the same intent, and
`tests/audit/test_code_rules.py` asserts both halves -- the vulnerable shape
fires, the safe shape does not. A rule that cannot be made quiet does not ship.

Two consequences worth stating:

* **There is no general path-traversal rule.** Catching `Path(root) / name`
  requires knowing that `name` came from a route parameter, and route
  parameters are only knowable from the application object rather than from one
  file. The archive-member case *is* covered, because there the provenance is
  visible in the same expression. General traversal waits for the app-level
  tier; see `docs/reference/audit.md`.
* **Taint starts at the route boundary, and that is what makes it precise.**
  A handler is identifiable from its decorator alone -- `@router.get(...)`,
  `@app.post(...)` -- so its parameters are known to be caller-controlled
  without needing the application object. Everything else is inferred one hop
  at a time from there.

  This distinction is the whole rule set. Interpolating a *module constant*
  into SQL is how you write a schema-qualified statement, and Wreath's own
  `_locks.py` does it in seven places; interpolating a *handler parameter* is
  an injection. A first draft of this file did not separate them and reported
  103 findings against Wreath's own source and six against a correct example
  application -- which is precisely the "cries wolf, gets suppressed wholesale"
  failure this module claims to avoid. Measured, not assumed: the sweep is in
  `docs/reference/audit.md`.

## Adding a rule

A rule is a row in `CODE_RULES` plus the detection in `_Scanner`, and a pair of
tests: one shape it catches, one shape it must not. The row carries the CWE and
the Wreath primitive that replaces the defect, because a finding with no
remediation is a complaint.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass

from ..model import Finding, Severity

__all__ = ["CODE_RULES", "CodeRule", "scan_source"]


@dataclass(frozen=True)
class CodeRule:
    """One rule's identity and remediation. Detection lives in `_Scanner`."""

    rule_id: str
    severity: Severity
    #: CWE identifier, so a finding maps onto an external taxonomy.
    reference: str
    #: The Wreath primitive (or stdlib call) that replaces the defect.
    suggestion: str
    #: One line, for the reference table in the docs.
    summary: str


CODE_RULES: tuple[CodeRule, ...] = (
    CodeRule(
        "sql-interpolation", Severity.ERROR, "CWE-89",
        "pass values as $1, $2 parameters to Session.raw; Wreath never rewrites raw SQL",
        "SQL built by string interpolation reaches the database unmodified",
    ),
    CodeRule(
        "timing-unsafe-compare", Severity.ERROR, "CWE-208",
        "compare secrets with hmac.compare_digest, which does not return early",
        "a secret compared with == leaks its prefix through response timing",
    ),
    CodeRule(
        "weak-randomness", Severity.ERROR, "CWE-338",
        "use secrets.token_urlsafe / secrets.choice for anything an attacker must not guess",
        "a security value drawn from random, which is a predictable Mersenne Twister",
    ),
    CodeRule(
        "hardcoded-secret", Severity.ERROR, "CWE-798",
        "read the key from the environment or a secret store; never a literal in source",
        "a signing key or password written as a string literal",
    ),
    CodeRule(
        "ssrf-policy-widened", Severity.ERROR, "CWE-918",
        "leave DestinationPolicy at its defaults and name the hosts you mean with hosts=",
        "an outbound client permitted to reach private, loopback or link-local addresses",
    ),
    CodeRule(
        "unsafe-xml-parser", Severity.ERROR, "CWE-611",
        "parse with wreath.xml.parse, which refuses DOCTYPE and every non-predefined entity",
        "XML read with a parser that resolves external entities",
    ),
    CodeRule(
        "template-from-request", Severity.ERROR, "CWE-1336",
        "compile templates from a TemplateDirectory at startup, never from a request",
        "a template compiled from a value that is not a literal",
    ),
    CodeRule(
        "dynamic-import", Severity.ERROR, "CWE-470",
        "map a closed vocabulary of names to functions; never resolve a caller's string",
        "a module, attribute or expression resolved from data",
    ),
    CodeRule(
        "unsafe-archive-extract", Severity.ERROR, "CWE-22",
        "extract with wreath.objects.unzip_stream and ZipExtractionLimits",
        "an archive extracted without member, size, ratio or symlink limits",
    ),
    CodeRule(
        "mass-assignment", Severity.ERROR, "CWE-915",
        "declare the body as a dataclass or ORM model; Wreath rejects unknown fields",
        "a request body walked onto an object with setattr",
    ),
    CodeRule(
        "case-mapped-authz", Severity.WARN, "CWE-178",
        "compare the stored, normalised value; upper()/lower()/casefold() are not injective",
        "an authorization decision made after a Unicode case mapping",
    ),
    CodeRule(
        "cors-reflect-origin", Severity.ERROR, "CWE-942",
        "use CORSMiddleware with an explicit allow_origins list",
        "the request Origin reflected into Access-Control-Allow-Origin",
    ),
    CodeRule(
        "debug-enabled", Severity.WARN, "CWE-489",
        "drive debug from configuration so production cannot inherit a developer's value",
        "the application constructed with debug=True as a literal",
    ),
    CodeRule(
        "untrusted-forwarded-header", Severity.WARN, "CWE-348",
        "establish the header with ProxyHeadersMiddleware(trusted=...) before reading it",
        "a forwarded client address read without a configured proxy trust boundary",
    ),
    CodeRule(
        "unparseable", Severity.WARN, "wreath:audit",
        "check the file parses under the interpreter this audit runs on",
        "the file could not be parsed, so no rule could be applied to it",
    ),
)

_BY_ID = {rule.rule_id: rule for rule in CODE_RULES}

#: Ruff's `flake8-bandit` codes that mean the same thing as one of these rules.
#:
#: **A finding the project has already declared and justified is not reported
#: again.** Wreath's own ORM compiler carries `# noqa: S102` on three `exec`
#: calls with a written reason, and re-raising those under a second name is how
#: the second tool gets switched off. `AGENTS.md` asks for suppressions to be
#: declared, scoped and reasoned; honouring the declaration is the other half
#: of that bargain.
_EQUIVALENT_NOQA = {
    "dynamic-import": ("S102", "S307"),
    "sql-interpolation": ("S608",),
    "hardcoded-secret": ("S105", "S106", "S107"),
    "weak-randomness": ("S311",),
    "unsafe-xml-parser": ("S314", "S405", "S320"),
    "unsafe-archive-extract": ("S202",),
}

#: The audit's own marker, for a finding ruff has no code for:
#:
#:     # wreath-audit: allow case-mapped-authz -- the list is ASCII by construction
#:
#: A bare marker with no reason is itself a finding, exactly as for the native
#: lints -- so the reason is required and is echoed in `--json`.
_WAIVER = "wreath-audit: allow"


# --- vocabularies ------------------------------------------------------------
#
# Kept as data rather than inline so the docs table and the tests can quote
# them, and so widening one is a reviewable one-line change.

#: Names that are a secret whatever else is in the expression.
_SECRET_WORDS = (
    "secret", "hmac", "password", "passwd", "apikey", "api_key",
    "otp", "totp", "salt", "credential",
)

#: Names that are *sometimes* a secret. A route signature, a lexer token, a
#: plan digest and a protocol nonce all live in Wreath under these words, and
#: flagging them accounted for six false positives. They fire only opposite a
#: name that gives the comparison an authentication role.
_WEAK_SECRET_WORDS = ("token", "signature", "digest", "mac")

#: The second signal: one side names what the other is being checked against.
_COMPARISON_ROLES = (
    "expected", "provided", "given", "supplied", "received", "candidate",
    "computed", "presented", "claimed",
)

#: Suffixes that make a name an identifier or a tag rather than a secret.
#: `credential_id` is a primary key; `_TOKEN_VERSION` is a format marker.
_NOT_SECRET_SUFFIXES = ("_id", "_ids", "_name", "_version", "_type", "_kind", "_key_id")
#: Deliberately excludes a bare "key" (`for key, value in ...` is everywhere),
#: and "auth"/"sig", which matched `oauth2` helpers and anything with "signal"
#: in the name across 25 false positives in Wreath's own source.
_SECRET_EXACT = ("pin",)

#: Keyword arguments whose literal value is a credential.
_SECRET_KEYWORDS = frozenset(
    {"secret", "secret_key", "url_secret", "password", "api_key", "apikey",
     "token", "private_key", "signing_key"}
)

#: Constructors whose *first positional* argument is a signing key.
_POSITIONAL_SECRET_CALLEES = frozenset({"SessionMiddleware", "CSRFMiddleware"})

#: Modules that will resolve an external entity if asked.
_UNSAFE_XML_MODULES = ("xml.sax", "xml.dom", "xml.etree", "xml.parsers", "lxml")

#: Statement-level sinks that execute SQL exactly as given.
_SQL_SINKS = frozenset({"raw", "declared", "execute", "fetch", "fetchrow", "fetchval"})

#: Headers that carry a client address only when a proxy is trusted.
_FORWARDED_HEADERS = frozenset(
    {"x-forwarded-for", "x-real-ip", "x-client-ip", "forwarded", "true-client-ip"}
)

#: Names that identify a principal. The case-mapping rule needs one of these
#: on the mapped side: a CORS preflight uppercases an HTTP *method*, and a
#: method is not a principal.
_IDENTITY_WORDS = (
    "email", "user", "login", "principal", "account", "name", "subject",
    "actor", "identity", "address",
)

#: Containers whose membership decides authorization.
_AUTHZ_WORDS = (
    "allow", "permit", "admin", "ops", "staff", "role", "whitelist", "allowlist",
    "member", "privileged", "superuser",
)

_CASE_MAPPINGS = frozenset({"upper", "lower", "casefold", "title", "swapcase"})

#: Decorator attributes that mark a function as reachable from the network.
_ROUTE_DECORATORS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "route", "websocket"}
)

#: Handler parameters that are framework-injected rather than caller-supplied.
_INJECTED_PARAMS = frozenset({"self", "cls", "request", "websocket"})

#: Annotations that mark a parameter as framework-injected.
_INJECTED_ANNOTATIONS = ("Session", "FromORM", "Depends", "Request", "AppScope")


def _is_route_decorator(node: ast.AST) -> bool:
    """Whether this decorator publishes the function on the network."""
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATORS


def _mentions(name: str, words: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in words)


def _is_secret_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(_NOT_SECRET_SUFFIXES):
        return False
    if _mentions(lowered, _SECRET_WORDS):
        return True
    # Exact-match vocabulary: short words that would over-match as substrings.
    # "pin" inside "pinned".
    return any(part in _SECRET_EXACT for part in lowered.replace("-", "_").split("_"))


def _is_weak_secret_name(name: str) -> bool:
    lowered = name.lower()
    return not lowered.endswith(_NOT_SECRET_SUFFIXES) and _mentions(
        lowered, _WEAK_SECRET_WORDS
    )


def _bound_names(target: ast.AST) -> list[str]:
    """The names an assignment target binds.

    Only plain names and the elements of a tuple or list unpacking. **Not the
    base of an attribute or subscript**: `self.table = value` binds an
    attribute, not `self`, and collecting it with `ast.walk` marked every
    object whose field was ever assigned from request data as caller-controlled
    -- nine of the twenty-nine false positives against Wreath's own source.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return []


def _name_of(node: ast.AST) -> str:
    """A readable name for an expression, for the vocabulary tests."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    if isinstance(node, ast.Subscript):
        return _name_of(node.value)
    return ""


def _dotted(node: ast.AST) -> str:
    """`a.b.c` for an attribute chain, else ''."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _is_dynamic_string(node: ast.AST) -> bool:
    """Whether this expression builds a string from something at run time."""
    if isinstance(node, ast.JoinedStr):
        # A constant f-string is a constant. Flagging it teaches nothing.
        return any(isinstance(part, ast.FormattedValue) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return any(
            isinstance(side, ast.Constant) and isinstance(side.value, str)
            for side in (node.left, node.right)
        )
    if isinstance(node, ast.Call):
        callee = node.func
        if isinstance(callee, ast.Attribute) and callee.attr in ("format", "join"):
            return True
    return False


class _Scanner(ast.NodeVisitor):
    """One walk, every rule.

    Two passes in practice: `prepare` collects the module-wide facts a rule
    needs (which names hold request data, whether a proxy trust boundary is
    configured), then the walk applies the rules. Doing it the other way round
    made a rule's answer depend on the order statements happened to appear in.
    """

    def __init__(self, tree: ast.Module, surface: str, source: str = "") -> None:
        self.tree = tree
        self.surface = surface
        self.lines = source.splitlines()
        self.findings: list[Finding] = []
        self.request_bound: set[str] = set()
        self.random_bound: set[str] = set()
        self.dynamic_strings: set[str] = set()
        self.origin_bound: set[str] = set()
        self.archive_members: set[str] = set()
        self.proxy_trusted = False
        #: Names whose value a caller chose: handler parameters, request data,
        #: and anything derived from them. This is what separates an injection
        #: from a schema-qualified statement.
        self.caller_controlled: set[str] = set()
        #: Names bound once at module scope to a literal. An application's
        #: shell template and its schema name both live here, and neither is
        #: something a caller chose.
        self.module_constants: set[str] = set()

    # -- emit ----------------------------------------------------------------

    def _waived(self, rule_id: str, line: int) -> bool:
        """Whether a reviewed directive already covers this line.

        The statement's own line and the two above it, because a directive on a
        multi-line call conventionally sits on the opening line and the node
        this scanner flags is sometimes an inner expression.
        """
        codes = _EQUIVALENT_NOQA.get(rule_id, ())
        for offset in (0, -1, -2, 1):
            index = line - 1 + offset
            if not (0 <= index < len(self.lines)):
                continue
            text = self.lines[index]
            if f"{_WAIVER} {rule_id}" in text:
                return True
            if "noqa" in text and any(code in text for code in codes):
                return True
        return False

    def _flag(self, rule_id: str, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        if self._waived(rule_id, line):
            return
        rule = _BY_ID[rule_id]
        self.findings.append(
            Finding(
                rule_id=rule.rule_id,
                severity=rule.severity,
                surface=self.surface,
                message=message,
                reference=rule.reference,
                location=f"{getattr(node, 'lineno', 0)}:{getattr(node, 'col_offset', 0)}",
                suggestion=rule.suggestion,
            )
        )

    # -- pass one ------------------------------------------------------------

    def prepare(self) -> None:
        for statement in self.tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                targets = (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    self.module_constants.update(_bound_names(target))
        self._collect_handler_parameters()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                dotted = _dotted(node.func)
                name = _name_of(node.func)
                if name == "ProxyHeadersMiddleware":
                    self.proxy_trusted = True
                if dotted.endswith("infolist") or dotted.endswith("namelist") \
                        or dotted.endswith("getmembers"):
                    self.archive_members.add(dotted.split(".")[0])
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                self._bind(node)
            if isinstance(node, ast.For):
                # `for member in archive.infolist():` binds the member name.
                iterated = node.iter
                if isinstance(iterated, ast.Call):
                    dotted = _dotted(iterated.func)
                    if dotted.split(".")[-1] in ("infolist", "namelist", "getmembers"):
                        for target in ast.walk(node.target):
                            if isinstance(target, ast.Name):
                                self.archive_members.add(target.id)
        self._propagate()

    def _collect_handler_parameters(self) -> None:
        """Every parameter of every route-decorated function.

        A handler is recognisable from its decorator alone, which is why this
        needs no application object: `@router.get("/x")` and `@app.post("/y")`
        both end in an attribute from `_ROUTE_DECORATORS`. Framework-injected
        parameters are excluded -- a `Session` is not caller-controlled, and
        treating it as such put a finding on every handler that runs a query.
        """
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_route_decorator(d) for d in node.decorator_list):
                continue
            arguments = node.args
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
                if argument.arg in _INJECTED_PARAMS:
                    continue
                annotation = ast.unparse(argument.annotation) if argument.annotation else ""
                if any(marker in annotation for marker in _INJECTED_ANNOTATIONS):
                    continue
                self.caller_controlled.add(argument.arg)

    def _propagate(self) -> None:
        """One hop at a time until nothing new is caller-controlled.

        A fixed point rather than a single pass, because `needle = q.strip()`
        followed by `sql = f"...{needle}..."` needs two, and the statements are
        not guaranteed to be walked in that order.
        """
        for _ in range(8):                    # bounded; real chains are short
            before = len(self.caller_controlled) + len(self.dynamic_strings)
            for node in ast.walk(self.tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [name for target in targets for name in _bound_names(target)]
                referenced = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
                if referenced & (self.caller_controlled | self.request_bound):
                    self.caller_controlled.update(names)
                    if _is_dynamic_string(value.value if isinstance(value, ast.Await) else value):
                        self.dynamic_strings.update(names)
            if len(self.caller_controlled) + len(self.dynamic_strings) == before:
                return

    def _bind(self, node: ast.Assign | ast.AnnAssign) -> None:
        value = node.value
        if value is None:
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [name for target in targets for name in _bound_names(target)]
        if not names:
            return
        source = value.value if isinstance(value, ast.Await) else value

        if self._is_request_data(source):
            self.request_bound.update(names)
        if self._is_random(source):
            self.random_bound.update(names)
        if _is_dynamic_string(source):
            self.dynamic_strings.update(names)
        if self._is_origin_read(source):
            self.origin_bound.update(names)

    def _is_request_data(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        dotted = _dotted(node.func)
        return dotted.endswith(("request.json", "request.form", "request.body"))

    def _is_random(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if dotted.startswith("random.") or dotted == "random":
                return True
            root = dotted.split(".")[0] if dotted else ""
            return bool(root) and root in self.random_bound
        return False

    def _is_origin_read(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if not _dotted(node.func).endswith(("header", "get")):
            return False
        return any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value.lower() == "origin"
            for arg in node.args
        )

    # -- pass two ------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        self._sql(node)
        self._secrets(node)
        self._ssrf(node)
        self._xml(node)
        self._template(node)
        self._dynamic_import(node)
        self._archive(node)
        self._cors(node)
        self._debug(node)
        self._forwarded(node)
        self._random_use(node)
        self.generic_visit(node)

    def _sql(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _SQL_SINKS:
            return
        if not node.args:
            return
        first = node.args[0]
        if isinstance(first, ast.Name):
            interpolated = first.id in self.dynamic_strings
            tainted = first.id in self.caller_controlled
        else:
            interpolated = _is_dynamic_string(first)
            referenced = {n.id for n in ast.walk(first) if isinstance(n, ast.Name)}
            tainted = bool(referenced & (self.caller_controlled | self.request_bound))
        # Interpolating a module constant is how a schema-qualified statement is
        # written. Interpolating something a caller chose is an injection. Only
        # the second is a finding, and conflating them is what made the first
        # draft of this rule unusable.
        if interpolated and tainted:
            self._flag(
                "sql-interpolation", node,
                f"SQL passed to .{node.func.attr}() is built by string interpolation",
            )

    def _secrets(self, node: ast.Call) -> None:
        callee = _name_of(node.func)
        if callee in _POSITIONAL_SECRET_CALLEES and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, (str, bytes)):
                self._flag(
                    "hardcoded-secret", node,
                    f"{callee} is constructed with a literal signing key",
                )
        for keyword in node.keywords:
            if keyword.arg in _SECRET_KEYWORDS and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, (str, bytes)) and keyword.value.value:
                    self._flag(
                        "hardcoded-secret", node,
                        f"{keyword.arg}= is a literal",
                    )

    def _ssrf(self, node: ast.Call) -> None:
        if _name_of(node.func) != "DestinationPolicy":
            return
        widened = [
            keyword.arg
            for keyword in node.keywords
            if keyword.arg in ("allow_private", "allow_loopback", "allow_link_local")
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
        ]
        if widened:
            self._flag(
                "ssrf-policy-widened", node,
                "DestinationPolicy permits " + ", ".join(sorted(widened)),
            )

    def _xml(self, node: ast.Call) -> None:
        if _name_of(node.func) != "setFeature":
            return
        for arg in node.args:
            name = _name_of(arg)
            if name.startswith("feature_external"):
                self._flag(
                    "unsafe-xml-parser", node,
                    f"{name} is enabled, which lets the document name files and URLs",
                )

    def _template(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "from_string":
            return
        first = node.args[0] if node.args else None
        constant = isinstance(first, ast.Constant) or (
            isinstance(first, ast.Name) and first.id in self.module_constants
        ) or (
            # `_SHELL % {...}` -- a constant formatted with constants.
            isinstance(first, ast.BinOp)
            and isinstance(first.left, ast.Name)
            and first.left.id in self.module_constants
        )
        if first is not None and not constant:
            self._flag(
                "template-from-request", node,
                "a template is compiled from a value that is not a literal",
            )

    def _dynamic_import(self, node: ast.Call) -> None:
        callee = _name_of(node.func)
        dotted = _dotted(node.func)
        if callee in ("eval", "exec") and not isinstance(node.func, ast.Attribute):
            self._flag("dynamic-import", node, f"{callee}() executes data as code")
            return
        if dotted.endswith("import_module") or callee == "__import__":
            referenced = {n.id for n in ast.walk(node.args[0])
                          if isinstance(n, ast.Name)} if node.args else set()
            caller_chose = bool(referenced & (self.caller_controlled | self.request_bound))
            if node.args and not isinstance(node.args[0], ast.Constant) and caller_chose:
                self._flag(
                    "dynamic-import", node,
                    "a module name is resolved from a value that is not a literal",
                )

    def _archive(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "extractall":
            self._flag(
                "unsafe-archive-extract", node,
                "extractall() honours member paths and symlinks as given",
            )

    def _cors(self, node: ast.Call) -> None:
        literals = [
            arg for arg in ast.walk(node)
            if isinstance(arg, ast.Constant)
            and isinstance(arg.value, (str, bytes))
            and _as_text(arg.value) == "access-control-allow-origin"
        ]
        if not literals:
            return
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if names & self.origin_bound or any(_mentions(n, ("origin",)) for n in names):
            self._flag(
                "cors-reflect-origin", node,
                "Access-Control-Allow-Origin is set from the request's own Origin",
            )

    def _debug(self, node: ast.Call) -> None:
        if _name_of(node.func) != "Wreath":
            return
        for keyword in node.keywords:
            if keyword.arg == "debug" and isinstance(keyword.value, ast.Constant) \
                    and keyword.value.value is True:
                self._flag("debug-enabled", node, "the application is constructed with debug=True")

    def _forwarded(self, node: ast.Call) -> None:
        if self.proxy_trusted:
            return
        if not _dotted(node.func).endswith(("header", "get")):
            return
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.lower() in _FORWARDED_HEADERS:
                self._flag(
                    "untrusted-forwarded-header", node,
                    f"{arg.value} is read but no ProxyHeadersMiddleware establishes it",
                )

    def _random_use(self, node: ast.Call) -> None:
        """`random.Random(x)` seeded from a value is a defect on its own."""
        if _dotted(node.func) == "random.Random" and node.args:
            argument = node.args[0]
            # A module constant is a *reproducibility* seed -- a data seeder, a
            # property test. Both of the example application's findings were
            # this, and neither was a security draw.
            fixed = isinstance(argument, ast.Constant) or (
                isinstance(argument, ast.Name) and argument.id in self.module_constants
            )
            if not fixed:
                self._flag(
                    "weak-randomness", node,
                    "random.Random is seeded from a value, so every draw is reproducible",
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        names = [name for target in node.targets for name in _bound_names(target)]
        if any(_is_secret_name(name) for name in names) and self._draws_on_random(node.value):
            self._flag(
                "weak-randomness", node,
                f"{names[0]} is drawn from random rather than secrets",
            )
        self.generic_visit(node)

    def _draws_on_random(self, node: ast.AST) -> bool:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                dotted = _dotted(inner.func)
                if dotted.startswith("random."):
                    return True
                root = dotted.split(".")[0] if dotted else ""
                if root and root in self.random_bound:
                    return True
        return False

    def visit_Compare(self, node: ast.Compare) -> None:
        self._timing(node)
        self._case_mapped(node)
        self.generic_visit(node)

    def _timing(self, node: ast.Compare) -> None:
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            return
        operands = [node.left, *node.comparators]
        if any(isinstance(operand, ast.Constant) for operand in operands):
            # `alg == "RS256"`, `token_type == "bearer"`. Comparing against a
            # known literal is a dispatch, not a secret check, and flagging it
            # accounted for most of this rule's false positives.
            return
        names = [_name_of(operand) for operand in operands]
        strong = any(_is_secret_name(name) for name in names)
        weak = any(_is_weak_secret_name(name) for name in names) and any(
            _mentions(name, _COMPARISON_ROLES) for name in names
        )
        if strong or weak:
            self._flag(
                "timing-unsafe-compare", node,
                "a secret is compared with == , which returns on the first wrong byte",
            )

    def _case_mapped(self, node: ast.Compare) -> None:
        left = node.left
        mapped = (
            isinstance(left, ast.Call)
            and isinstance(left.func, ast.Attribute)
            and left.func.attr in _CASE_MAPPINGS
        )
        if not mapped:
            return
        if not _mentions(_name_of(left.func.value), _IDENTITY_WORDS):  # type: ignore[union-attr]
            return
        if any(_mentions(_name_of(operand), _AUTHZ_WORDS) for operand in node.comparators):
            self._flag(
                "case-mapped-authz", node,
                f".{left.func.attr}() is applied before an authorization comparison",  # type: ignore[union-attr]
            )

    def visit_For(self, node: ast.For) -> None:
        self._mass_assignment(node)
        self._archive_member_path(node)
        self.generic_visit(node)

    def _mass_assignment(self, node: ast.For) -> None:
        iterated = {n.id for n in ast.walk(node.iter) if isinstance(n, ast.Name)}
        if not (iterated & self.request_bound):
            return
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _name_of(inner.func) == "setattr":
                self._flag(
                    "mass-assignment", inner,
                    "every key of the request body is written onto the object",
                )
                return

    def _archive_member_path(self, node: ast.For) -> None:
        """A member's own name used to build a destination path.

        This is the hand-rolled extractor -- the loop `extractall` was avoided
        in favour of -- and its provenance is visible in one expression, which
        is why it can be matched precisely where general traversal cannot.
        """
        members = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        members |= self.archive_members
        for inner in ast.walk(node):
            if not isinstance(inner, ast.BinOp) or not isinstance(inner.op, ast.Div):
                continue
            for side in (inner.left, inner.right):
                if isinstance(side, ast.Attribute) and side.attr in ("filename", "name") \
                        and _name_of(side.value) in members:
                    self._flag(
                        "unsafe-archive-extract", inner,
                        "a destination path is built from an archive member's own name",
                    )
                    return

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.startswith(_UNSAFE_XML_MODULES):
                self._flag(
                    "unsafe-xml-parser", node,
                    f"{alias.name} resolves external entities unless explicitly disabled",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.startswith(_UNSAFE_XML_MODULES):
            self._flag(
                "unsafe-xml-parser", node,
                f"{node.module} resolves external entities unless explicitly disabled",
            )
        self.generic_visit(node)


def _as_text(value: str | bytes) -> str:
    return (value.decode("latin-1") if isinstance(value, bytes) else value).lower()


def scan_source(source: str, *, surface: str) -> list[Finding]:
    """Apply every code rule to one module's source.

    `source` is dedented first so a snippet lifted out of a docstring or a test
    parses the same way it would as a file.

    A file that does not parse yields a single `unparseable` finding rather than
    raising: one bad file in a tree must not take the scan of the other two
    hundred with it.
    """
    text = textwrap.dedent(source)
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        rule = _BY_ID["unparseable"]
        return [
            Finding(
                rule_id=rule.rule_id,
                severity=rule.severity,
                surface=surface,
                message=f"could not parse: {error.msg}",
                reference=rule.reference,
                location=f"{error.lineno or 0}:{error.offset or 0}",
                suggestion=rule.suggestion,
            )
        ]
    scanner = _Scanner(tree, surface, text)
    scanner.prepare()
    scanner.visit(tree)
    return scanner.findings
