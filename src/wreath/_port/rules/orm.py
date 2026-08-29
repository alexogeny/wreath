"""The ORM: models and columns (ormar, SQLModel, Django), and the `.objects.`
query chains that read them.
"""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED

ORM_MODELS: dict[str, tuple[str, str, str, str]] = {
    "orm.model": (
        "orm_model",
        "orm_models",
        TRANSLATED,
        'This model becomes wreath.orm.Model with table="<name>" on the class header, and each field an annotated column().',
    ),
    "orm.column": (
        "column",
        "orm_models",
        TRANSLATED,
        "Column types map onto wreath.orm.types. The one thing to check is emptiness: ormar allowed a column to be empty unless told otherwise, wreath requires a value unless told otherwise.",
    ),
    "orm.fk": (
        "column",
        "orm_models",
        NEEDS_REVIEW,
        "This foreign key points at a model this tool could not find, so the column type is a guess (Uuid). Open the model it references and set the column to the same type as its primary key.",
    ),
    "orm.fk_typed": (
        "column",
        "orm_models",
        TRANSLATED,
        'The foreign key becomes two lines: a column() holding the id, typed to match the primary key it points at, and a relationship() for the object. load="raise" means wreath will not fetch it behind your back -- include it in the query when you need it.',
    ),
}

QUERIES: dict[str, tuple[str, str, str, str]] = {
    # `.objects.` is the largest single construct in a real ormar codebase — of
    # the order of a third of every framework token in one.
    # One generic verdict for all of it reports the *size* of the job
    # and nothing about its *shape*, so each verb names the call it becomes.
    # The split within a verb is by *argument*, not by verb alone. `filter(id=x)`
    # is a mechanical rewrite — every keyword maps to a wreath predicate with the
    # value carried across untouched. `filter(name__icontains=x)` is not: the
    # value has to be wrapped in wildcards. `filter(ranch__slug=x)` is not
    # either — but *not* because the join is a decision, which it is not:
    # `Model.ranch.slug` is a `RelatedColumnExpr` and `plan_filter_joins` emits
    # the INNER JOIN itself, choosing INNER because a parent with no matching
    # child cannot satisfy a predicate on the child's column. The real blocker
    # is resolution: turning `ranch__slug` into `Model.ranch.slug` means knowing
    # `ranch` is a relation and `slug` a column on its target, and a model is
    # usually declared in a different module from the query. `analyze` has a
    # tree-wide index and could; `emit_module` is per-module and takes raw source
    # text, so it could not — and `query_rule` is shared precisely so the report
    # and the emitted TODO cannot disagree. Promoting this needs the emitter to
    # gain a tree-wide index first. Same verb, three verdicts, and the argument
    # list is what tells them apart — so the analyzer reads it rather than
    # guessing from the name.
    "orm.query": (
        "orm_query",
        "queries",
        UNSUPPORTED,
        "This is an ormar query and it was left as written. Queries become Model.select() with .where(...) on it, run through a session: await session.fetch(...) for a list, fetch_one(...) for one row, count(...) for a number.",
    ),
    "orm.manager_value": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This stores Model.objects as a repository dependency. Replace the manager parameter and fallback with one Session parameter; queries then use Model.select() through that session. Do not recreate a manager compatibility object.",
    ),
    "orm.manager_patch": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This test patches Model.objects.__class__. Construct the repository with an AsyncMock(spec=Session), configure get/fetch/fetch_one on that session, and assert the session call instead.",
    ),
    # The emitter writes the determined queries out in full, and can only do that
    # where a session is in scope. Inside a route handler wreath supplies one;
    # anywhere else the function has to take one, and that is a change to every
    # caller — so it is one note on the function rather than one per query.
    # What `--opinionated` does instead of leaving `orm.query.needs_session`.
    # Still needs-review, and for a reason that has nothing to do with the query:
    # the signature changed, so the callers have to catch up.
    "orm.query.session_added": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "this function now takes a session, because it runs queries or calls something that does. Every call to it inside the ported tree was updated to pass one; anything calling it from outside has to be updated by hand.",
    ),
    "orm.query.needs_session": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "Queries in this function were left alone because wreath runs them through a session and there is none here. Add a session: Session parameter to this function and pass one in from each caller -- a route handler gets one for free by declaring session: Annotated[Session, FromORM()]. Once it is in scope, each query below becomes the Model.select() form its own note describes.",
    ),
    # A transaction is a session block, not a query, so it bills as its own
    # construct -- but it lands in `queries` because the thing it needs is the
    # thing every running chain needs, and splitting the category would report
    # one missing session as two unrelated shortfalls.
    "orm.transaction.atomic": (
        "transaction",
        "queries",
        TRANSLATED,
        "with transaction.atomic() becomes async with session.begin(). Nesting is a savepoint on both sides -- wreath names them wreath_sp_<depth> -- so a nested atomic() block is a nested session.begin().",
    ),
    "orm.query.filter": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This filter was left as written because one of its lookups needs a decision. A lookup across a relation (owner__name) becomes Model.owner.name -- wreath adds the join itself, but it has to be told which model owner points at, and that model is usually in another file. A JSON lookup needs you to pick the containment operator. Everything else about the query is mechanical: Model.select().where(...), run with session.fetch().",
    ),
    "orm.query.filter_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "Every lookup here carries straight across: filter(...) becomes Model.select().where(Model.col == value), with __gte as >= and __in as .in_(...). Run it with await session.fetch(...) for a list or session.count(...) for a number. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.get_or_none": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "Same as the filter note: one of the lookups here does not carry across on its own. The call itself becomes await session.fetch_one(Model.select().where(...)), which returns None on no match exactly as get_or_none did.",
    ),
    "orm.query.get_or_none_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.fetch_one(Model.select().where(...)). It behaves the same as get_or_none: None when nothing matches, an error when more than one row does. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.get": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This get() has a dynamic predicate or positional query object, so the required-row rewrite cannot be proved. Static keyword lookups become session.require(...) or session.require_one(...), both of which preserve the exception-on-miss contract.",
    ),
    "orm.query.get_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "get(id=value) becomes await session.require(Model, value); other static lookups become await session.require_one(Model.select().where(...)). Both preserve the exception-on-miss and multiple-row contracts. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.create": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This create() uses positional input, so it cannot become a keyword-only Wreath model construction without a decision.",
    ),
    "orm.query.create_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.create(Model, **values) preserves immediate insertion while keeping construction and validation on the native Wreath model. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.all": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.fetch(Model.select()). Pass --opinionated and this is written out for you.",
    ),
    "orm.query.page_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "limit(n)/offset(n) becomes Model.select().limit(n)/offset(n); paginate(page, page_size) becomes the same limit plus offset=(page - 1) * page_size. The query is run with session.fetch(...). Pass --opinionated and this is written out for you.",
    ),
    "orm.query.eager": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "This call does not name the relations to load as plain strings -- select_all() means every relation, and wreath has no such switch. Write out the ones this code actually reads, one .include(Model.rel.selectin()) each. It matters more than it did: wreath never loads a relation behind your back, so one you forget raises instead of quietly running an extra query per row.",
    ),
    "orm.query.select_all": (
        "orm_query",
        "queries",
        UNSUPPORTED,
        "select_all() is deliberately not portable. Unbounded graph expansion hides query count and response size. Name the few relationships the use case reads, or write one explicit SQL projection/JSON aggregate for a genuinely wide response; Wreath will not recreate the switch.",
    ),
    "orm.query.select_all_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "This model declares no relationships, so select_all() expands nothing. It becomes Model.select() and is run through the session like all().",
    ),
    "orm.query.eager_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "One .include(Model.rel.selectin()) per relation named here, on a Model.select() run with session.fetch(). The include is not optional in wreath the way select_related was an optimisation: a relation you do not include raises when touched, instead of quietly running an extra query per row. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.values": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "values([...]) returned dictionaries. The wreath equivalent, Model.select(Model.a, Model.b), returns model objects with only those columns filled in -- so the code reading these rows has to use attributes instead of keys.",
    ),
    "orm.query.values_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "A literal values([...]) projection becomes Model.select(Model.a, Model.b) plus a dictionary comprehension; values_list(...) uses a tuple comprehension. Both work at the head or after filter()/fields() and add no compatibility function.",
    ),
    "orm.query.bulk": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "bulk_create/bulk_update becomes session.add() for each row followed by a single await session.flush(). The flush batches the inserts by model, so this is still one round trip per model rather than one per row.",
    ),
    "orm.query.count": (
        "orm_query",
        "queries",
        TRANSLATED,
        "await session.count(Model.select().where(...)). Pass --opinionated and this is written out for you.",
    ),
    "orm.query.exists": (
        "orm_query",
        "queries",
        TRANSLATED,
        "wreath has no exists(); count the rows instead -- await session.count(Model.select().where(...)) > 0. It is the same single round trip. Pass --opinionated and this is written out for you.",
    ),
    "orm.query.delete": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "For a row already loaded, session.delete(row) then await session.flush(). A statically filtered bulk delete becomes session.delete_where(Model.select().where(...)); predicate-free bulk writes are refused.",
    ),
    "orm.query.first": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "first() becomes await session.fetch_one(Model.select().order_by(...).limit(1)) -- and you have to supply the order_by. Without one, 'the first row' is whatever the database happens to return, which is why wreath makes you say it.",
    ),
    "orm.query.get_or_create": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "get_or_create is a check-then-create convenience. Static field values can be expanded onto Session.fetch_one and Session.create; dynamic defaults or lookup arguments stay here so the port does not guess which values select and which values create.",
    ),
    "orm.query.get_or_create_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "Static field values become a Session.fetch_one check followed by Session.create when absent, preserving the (row, created) result with existing Wreath primitives. This deliberately exposes the legacy check-then-create race instead of inventing an atomic compatibility helper.",
    ),
    "orm.query.order": (
        "orm_query",
        "queries",
        NEEDS_REVIEW,
        "The columns to order by are not plain strings here, so this tool cannot tell which columns they are. Written out, order_by('name') is .order_by(Model.name) and order_by('-created') is .order_by(Model.created.desc()).",
    ),
    "orm.query.order_exact": (
        "orm_query",
        "queries",
        TRANSLATED,
        "order_by('name') becomes .order_by(Model.name), order_by('-created') becomes .order_by(Model.created.desc()), and an already-explicit Model.created.desc() carries across unchanged. The Model.select() is run with the session. Pass --opinionated and this is written out for you.",
    ),
}

DJANGO_MODELS: dict[str, tuple[str, str, str, str]] = {
    # `translated` here means the emitter rewrites the site, and it does:
    # `models.CharField(max_length=32, null=True)` comes out as
    # `Mapped[str | None] = column(Varchar, nullable=True, check=Length(maximum=32))`,
    # the class header becomes `Model, table="..."`, Meta.db_table is consumed
    # into it, and the django import goes with the last field that used it.
    # These were needs-review for exactly as long as that was untrue.
    # Django is a supported source, not a foreign one, for the part of it that
    # has an exact wreath spelling. A field whose storage wreath matches is a
    # `column(PgType, ...)` and nothing is being invented; a field whose storage
    # it does not is refused by name, the way an unmapped ormar type is.
    "orm.django.model": (
        "orm_model",
        "orm_models",
        TRANSLATED,
        "class X(models.Model) becomes class X(Model) from wreath.orm, with Meta.db_table becoming __tablename__.",
    ),
    "orm.django.column": (
        "column",
        "orm_models",
        TRANSLATED,
        "This field maps to a wreath column type exactly: models.CharField(max_length=n) becomes Mapped[str] = column(Varchar, check=Length(maximum=n)), null= becomes nullable=, db_index= becomes index=.",
    ),
    "orm.django.column_unmapped": (
        "column",
        "orm_models",
        NEEDS_REVIEW,
        "wreath has no column type that stores exactly what this Django field stores. Pick the closest one in wreath.orm.types and check the values still fit -- this is not translated rather than translated wrongly.",
    ),
    "orm.django.fk": (
        "column",
        "orm_models",
        TRANSLATED,
        "ForeignKey becomes column(<referenced pk type>, references=Other.id, on_delete=...). The reverse accessor related_name= has no column form; wreath reaches the other side through a relationship().",
    ),
    "orm.django.m2m": (
        "column",
        "orm_models",
        UNSUPPORTED,
        "ManyToManyField is an association table, not a column, and Django creates it implicitly. Wreath declares that table, so this needs a model of its own before the two sides can be related.",
    ),
}
