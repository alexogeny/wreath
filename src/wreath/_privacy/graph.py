"""The foreign-key graph an erasure walks, read from the ORM's own registry.

This is the part the vendor category sells. A DSAR product connects to a
database it did not design and rebuilds the relationships by hand, in a
spreadsheet somebody maintains, and the spreadsheet is stale the day after the
next migration. Wreath does not have to: `wreath.orm.schema.ColumnRef` already
carries every foreign key with its referential action and its deferrability,
because `wreath.migrations` needs exactly that to diff a schema. The same fact,
read again for a different question.

Nothing here opens a connection. The graph is derived from a *compiled
registry*, which is what the application already built at startup, so
`wreath privacy plan` is safe to run anywhere -- including pointed at a
production application whose database is not reachable from where you are
standing. `wreath.infra` makes the same promise for the same reason.

**What that costs, stated plainly.** A declared graph is the graph the ORM
believes in. A foreign key added by hand in a migration the ORM does not model
is invisible here, and an erasure that does not know about it will miss rows.
`verify_against_catalog` is the answer: it is the only function in this package
that wants a live database, and it reports edges the catalog has and the
registry does not, rather than silently proceeding.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .model import Edge, Reach

__all__ = [
    "CATALOG_EDGES",
    "Graph",
    "Node",
    "build_graph",
    "catalog_edge_rows",
    "missing_edges",
    "order_children_first",
]


@dataclass(frozen=True, slots=True)
class Node:
    """One mapped table in the graph."""

    model: type
    label: str
    schema: str
    table: str
    primary_key: tuple[str, ...]
    #: Database column name -> whether it is nullable, for the orphan analysis.
    nullable: dict[str, bool]

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True, slots=True)
class Graph:
    """Every mapped table and every foreign key between them."""

    nodes: dict[type, Node]
    #: Child model -> the edges it declares, one per referencing column.
    outbound: dict[type, tuple[tuple[Edge, type], ...]]
    #: Parent model -> the edges pointing at it.
    inbound: dict[type, tuple[tuple[Edge, type], ...]]

    def reachable_from(self, root: type) -> dict[type, Reach]:
        """Every table reachable from `root` by following foreign keys inward.

        Breadth-first, so each table records the *shortest* path to the
        subject. That matters for more than tidiness: the shortest path is the
        one an execution predicate nests least deeply, and a reviewer reading
        "via three joins" instead of "via seven" is a reviewer who can still
        check the answer.

        The walk follows inbound edges only -- children referencing parents.
        Walking outbound as well would sweep in every row the subject's rows
        merely *point at*, which for a `Photo.camera_id` is somebody else's
        equipment record, not the subject's personal data.
        """
        found: dict[type, Reach] = {root: Reach(())}
        queue: deque[type] = deque([root])
        while queue:
            parent = queue.popleft()
            for edge, child in self.inbound.get(parent, ()):
                if child in found:
                    continue
                found[child] = Reach((*found[parent].path, edge))
                queue.append(child)
        return found


def build_graph(registry: Any) -> Graph:
    """Read a compiled `wreath.orm.Registry` into a graph.

    Args:
        registry: anything carrying `.specs`, which is every compiled ORM
            registry. Typed loosely so a test can hand in a stub without
            standing up a database.
    """
    specs = tuple(getattr(registry, "specs", ()) or ())
    if not specs:
        raise ValueError(
            "the registry has no compiled models, so there is no foreign-key graph "
            "to walk; pass the registry the application compiled at startup"
        )
    nodes: dict[type, Node] = {}
    for spec in specs:
        nodes[spec.model_type] = Node(
            model=spec.model_type,
            label=getattr(spec.model_type, "__name__", str(spec.model_type)),
            schema=str(spec.schema),
            table=str(spec.table),
            primary_key=tuple(column.database_name for column in spec.primary_key),
            nullable={
                column.database_name: bool(column.nullable) for column in spec.columns
            },
        )
    outbound: dict[type, list[tuple[Edge, type]]] = {}
    inbound: dict[type, list[tuple[Edge, type]]] = {}
    for spec in specs:
        child = spec.model_type
        for column in spec.columns:
            reference = column.reference
            if reference is None:
                continue
            target = reference.model_type
            if target not in nodes:
                # A reference to a model this registry does not compile. The
                # planner cannot walk it, and saying so is better than dropping
                # it: `missing_edges` reports the same class of hole from the
                # catalog side.
                continue
            edge = Edge(
                from_table=nodes[child].qualified,
                from_column=column.database_name,
                to_table=nodes[target].qualified,
                to_column=reference.column,
                on_delete=reference.on_delete,
                deferrable=bool(reference.deferrable),
            )
            outbound.setdefault(child, []).append((edge, target))
            inbound.setdefault(target, []).append((edge, child))
    return Graph(
        nodes=nodes,
        outbound={key: tuple(value) for key, value in outbound.items()},
        inbound={key: tuple(value) for key, value in inbound.items()},
    )


def order_children_first(
    graph: Graph, members: set[type]
) -> tuple[list[type], list[tuple[type, ...]]]:
    """Order `members` so every child precedes the parent it references.

    Deleting a parent before its children fails against a `RESTRICT` or
    `NO ACTION` foreign key and orphans data behind a `SET NULL` one, so the
    order is a correctness property rather than a preference.

    Returns:
        `(ordered, cycles)`. A cycle cannot be ordered -- that is what a cycle
        is -- so its members are left out of `ordered` and returned separately
        for the plan to name. Guessing an order inside a cycle is how an
        erasure half-runs and reports success.
    """
    after: dict[type, set[type]] = {model: set() for model in members}
    for child in members:
        for _edge, parent in graph.outbound.get(child, ()):
            if parent in members and parent is not child:
                after[parent].add(child)
    # Kahn's walk, indexed in the direction a removal unlocks.  The former
    # implementation rescanned every remaining dependency set after each
    # layer; a chain has one ready model per layer and therefore took Θ(V²).
    # Here each model is settled once and each distinct edge is decremented
    # once.  Sorting is confined to ready layers to retain stable plan output.
    parents_by_child: dict[type, set[type]] = {model: set() for model in members}
    waiting = {model: len(children) for model, children in after.items()}
    for parent, children in after.items():
        for child in children:
            parents_by_child[child].add(parent)

    def key(model: type) -> str:
        return graph.nodes[model].qualified

    ready = sorted((model for model in members if waiting[model] == 0), key=key)
    ordered: list[type] = []
    while ready:
        following: list[type] = []
        for model in ready:
            ordered.append(model)
            for parent in parents_by_child[model]:
                waiting[parent] -= 1
                if waiting[parent] == 0:
                    following.append(parent)
        ready = sorted(following, key=key)
    cycles = _cycles(graph, {model for model, count in waiting.items() if count != 0})
    return ordered, cycles


def _cycles(graph: Graph, members: set[type]) -> list[tuple[type, ...]]:
    """The strongly connected components of what the ordering could not place.

    Tarjan would be tidier; this is an iterative colour-marking walk because
    the input is the *residue* of a topological sort, which in a real schema is
    a handful of tables rather than the whole graph. Priced before written: the
    loop's length is the number of tables in a cycle, and its body is a set
    lookup, so there is nothing here worth making faster.
    """
    components: list[tuple[type, ...]] = []
    seen: set[type] = set()
    for start in sorted(members, key=lambda model: graph.nodes[model].qualified):
        if start in seen:
            continue
        component: list[type] = []
        stack = [start]
        local: set[type] = set()
        while stack:
            model = stack.pop()
            if model in local:
                continue
            local.add(model)
            component.append(model)
            # Membership is the only test on the way in. "Have we been here?"
            # is answered once, at the top of the loop, where a duplicate is
            # skipped anyway -- a second copy of that question here would only
            # keep the stack shorter, and two spellings of one condition is how
            # they drift apart later.
            #
            # Both directions are walked, and only one of them is ever taken by
            # a test. A residue member is stuck because of a *child* it depends
            # on, so following outbound edges from the alphabetically-first
            # member reaches every loop this planner can build -- the inbound
            # arm is completeness for a component whose first member has no
            # outbound edge inside it, which the ordering does not produce.
            # Left in rather than deleted, because "the walk visits the whole
            # component" is the property the finding rests on, and a component
            # split in two would name the wrong tables to break.
            for _edge, child in graph.inbound.get(model, ()):
                if child in members:
                    stack.append(child)
            for _edge, parent in graph.outbound.get(model, ()):
                if parent in members:
                    stack.append(parent)
        seen |= local
        # No size test, and no self-reference test beside it. This is called
        # with the *residue* of the topological sort, and a model reaches the
        # residue only by depending on another member -- `order_children_first`
        # never records a table as its own dependency, so a table that merely
        # references itself is ordered like any other and never arrives here.
        # Every component is therefore a genuine loop of two or more. A
        # mutation run found both clauses unreachable, and an unreachable
        # clause in a finding is a claim nobody can check.
        components.append(
            tuple(sorted(component, key=lambda model: graph.nodes[model].qualified))
        )
    return components


#: Every foreign key the live catalog holds for one schema, as
#: `(child_table, child_column, parent_table, parent_column)`. Read only by
#: `verify` -- the planner itself never opens a socket.
CATALOG_EDGES = """
    SELECT
        c.relname::text        AS child_table,
        a.attname::text        AS child_column,
        fc.relname::text       AS parent_table,
        fa.attname::text       AS parent_column
    FROM pg_catalog.pg_constraint con
    JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
    JOIN pg_catalog.pg_attribute a
      ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
    JOIN pg_catalog.pg_attribute fa
      ON fa.attrelid = con.confrelid AND fa.attnum = con.confkey[1]
    WHERE n.nspname = $1::text
      AND con.contype = 'f'
"""


def catalog_edge_rows(rows: Any, schema: str) -> set[tuple[str, str, str, str]]:
    """Normalise `CATALOG_EDGES` rows into comparable tuples."""
    found: set[tuple[str, str, str, str]] = set()
    for row in rows:
        child, child_column, parent, parent_column = (str(item) for item in tuple(row)[:4])
        found.add(
            (f"{schema}.{child}", child_column, f"{schema}.{parent}", parent_column)
        )
    return found


def missing_edges(
    graph: Graph, catalog: set[tuple[str, str, str, str]]
) -> list[tuple[str, str, str, str]]:
    """Foreign keys the database has that the registry does not model.

    Each one is a path an erasure would not walk, which makes it exactly the
    silent omission this module exists to surface. Reported, never repaired:
    the fix is a model declaration or a written note that the table holds no
    personal data, and neither is a decision a planner should make.
    """
    declared = {
        (edge.from_table, edge.from_column, edge.to_table, edge.to_column)
        for edges in graph.outbound.values()
        for edge, _target in edges
    }
    return sorted(catalog - declared)
