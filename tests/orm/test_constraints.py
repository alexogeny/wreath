"""Business rules layered onto the column types, in the same single pass.

The seam under test is the promise that there is *one* validator per column --
the type with its rules fused in -- and that every write path goes through it.
Several of these tests exist to prove the paths cannot drift: a value refused by
a request body must be refused by an assignment and by the constructor, for the
same reason, whichever storage backend is in use.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest

from wreath.binding import ValidationError
from wreath.orm import (
    CheckViolation,
    DeclarationError,
    FromORM,
    Ge,
    Gt,
    Le,
    Length,
    Lt,
    Mapped,
    Model,
    OneOf,
    Pattern,
    Predicate,
    Session,
    column,
    narrow,
    rule,
)
from wreath.orm.types import Bool, Int64, Text, Uuid
from wreath.orm.validation import compile_model_validator
from wreath.testing import TestClient

from .test_binding import build_app


class Employee(Model):
    """A table-less base: mapped models cannot inherit from a mapped model."""

    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text, check=Length(1, 200))
    salary: Mapped[int] = column(Int64, check=Ge(0))
    tenure_months: Mapped[int] = column(Int64, check=Ge(0))
    grade: Mapped[str] = column(Text, check=OneOf("intern", "junior", "senior"))


class Intern(Employee, table="constraint_interns"):
    """An employee, with narrower rules. Every valid Intern is a valid Employee."""

    salary_cap = narrow("salary", Le(50_000))
    tenure_cap = narrow("tenure_months", Le(8))
    grade_fixed = narrow("grade", OneOf("intern"))

    @rule("salary", "tenure_months", at="salary")
    def pay_band(salary: int, tenure_months: int) -> bool:
        """an intern past six months cannot be paid more than 40k"""
        return not (tenure_months > 6 and salary > 40_000)


class Senior(Employee, table="constraint_seniors"):
    """A second model over the same base, to prove narrowing does not leak."""

    floor = narrow("salary", Ge(80_000))


def body(**overrides: Any) -> dict[str, Any]:
    payload = {"name": "Ada", "salary": 30_000, "tenure_months": 3, "grade": "intern"}
    payload.update(overrides)
    return payload


@pytest.fixture
def validator() -> Any:
    return compile_model_validator(Intern)


# -- the three layers ----------------------------------------------------------


def test_a_sound_body_passes_every_layer(validator: Any) -> None:
    intern = validator(body(), ("body",))
    assert isinstance(intern, Intern)
    assert intern.salary == 30_000 and intern.grade == "intern"


def test_a_type_error_is_still_a_type_error(validator: Any) -> None:
    # The type layer is unchanged and still reports itself as the column's type.
    with pytest.raises(ValidationError) as caught:
        validator(body(salary="lots"), ("body",))
    assert caught.value.errors == [
        {"loc": ["body", "salary"], "msg": "expected int8, got str", "type": "int8"}
    ]


def test_a_broken_rule_reports_the_rule_not_the_type(validator: Any) -> None:
    # A client can tell "that is not an integer" from "that integer is too
    # large" without parsing the message.
    with pytest.raises(ValidationError) as caught:
        validator(body(salary=60_000), ("body",))
    assert caught.value.errors == [
        {"loc": ["body", "salary"], "msg": "value must be at most 50000", "type": "le"}
    ]


def test_a_narrowed_column_still_enforces_the_base_rule(validator: Any) -> None:
    # Intern caps the salary; Employee floors it. Narrowing added the cap, it
    # did not replace the floor.
    with pytest.raises(ValidationError) as caught:
        validator(body(salary=-1), ("body",))
    assert caught.value.errors[0]["type"] == "ge"


def test_narrowing_is_per_model_and_does_not_leak_to_a_sibling() -> None:
    # Both narrow the salary column they inherit, in opposite directions. The
    # base's column is a prototype; each model narrows its own clone.
    validate_senior = compile_model_validator(Senior)
    senior = validate_senior(
        body(salary=90_000, grade="senior", tenure_months=60), ("body",)
    )
    assert senior.salary == 90_000
    # 90k is fine for a Senior and far too much for an Intern.
    with pytest.raises(ValidationError):
        compile_model_validator(Intern)(body(salary=90_000), ("body",))
    # ...and 30k is fine for an Intern and too little for a Senior.
    with pytest.raises(ValidationError) as caught:
        validate_senior(body(salary=30_000, grade="senior"), ("body",))
    assert caught.value.errors[0]["type"] == "ge"


def test_a_cross_field_rule_runs_once_the_fields_are_in(validator: Any) -> None:
    # Neither value breaks its own column's rules; together they break the model.
    assert validator(body(salary=45_000, tenure_months=5), ("body",)).salary == 45_000
    assert validator(body(salary=39_000, tenure_months=7), ("body",)).salary == 39_000
    with pytest.raises(ValidationError) as caught:
        validator(body(salary=45_000, tenure_months=7), ("body",))
    assert caught.value.errors == [
        {
            "loc": ["body", "salary"],
            "msg": "an intern past six months cannot be paid more than 40k",
            "type": "pay_band",
        }
    ]


def test_a_rule_reports_against_the_object_when_it_names_no_field() -> None:
    class Booking(Model, table="constraint_bookings"):
        id: Mapped[int] = column(Int64, primary_key=True)
        first_day: Mapped[int] = column(Int64)
        last_day: Mapped[int] = column(Int64)

        @rule("first_day", "last_day")
        def ordered(first_day: int, last_day: int) -> bool:
            """a booking cannot end before it starts"""
            return first_day <= last_day

    with pytest.raises(ValidationError) as caught:
        compile_model_validator(Booking)({"first_day": 9, "last_day": 2}, ("body",))
    assert caught.value.errors == [
        {"loc": ["body"], "msg": "a booking cannot end before it starts", "type": "ordered"}
    ]


def test_rules_do_not_run_when_a_field_failed(validator: Any) -> None:
    # A rule has nothing useful to say about a value that was already rejected,
    # and reporting it would bury the error that matters.
    with pytest.raises(ValidationError) as caught:
        validator(body(salary="lots", tenure_months=7), ("body",))
    assert [item["type"] for item in caught.value.errors] == ["int8"]


def test_a_rule_is_skipped_when_one_of_its_columns_is_absent() -> None:
    class Optional(Model, table="constraint_optional"):
        id: Mapped[int] = column(Int64, primary_key=True)
        low: Mapped[int] = column(Int64, nullable=True)
        high: Mapped[int] = column(Int64, nullable=True)

        @rule("low", "high")
        def ordered(low: int, high: int) -> bool:
            """low must not exceed high"""
            return low <= high

    validate = compile_model_validator(Optional)
    # Nullable columns are loaded as None here, so the rule does see them; what
    # it must not see is a column that was never loaded at all.
    instance = Optional._orm_new()
    instance._orm_set_loaded(1, 5)
    from wreath.orm.constraints import check_rules

    assert check_rules(instance) == []  # 'high' is unloaded: nothing to judge
    assert validate({"low": 5, "high": 9}, ("body",)).low == 5


# -- one validator, every write path -------------------------------------------


def test_assignment_enforces_the_same_rules_as_a_body(validator: Any) -> None:
    # The seam: what a body is refused for, an assignment is refused for. This
    # is the test that fails if the native descriptor ever goes back to calling
    # PgType.coerce instead of the column's fused validator.
    intern = validator(body(), ("body",))
    with pytest.raises(CheckViolation) as caught:
        intern.salary = 60_000
    assert caught.value.kind == "le"
    assert intern.salary == 30_000  # the refused value never landed
    intern.salary = 40_000
    assert intern.salary == 40_000


def test_a_check_violation_is_a_value_error(validator: Any) -> None:
    # Every seam that already handles a rejected assignment handles a broken
    # business rule, with no new except clause anywhere.
    assert issubclass(CheckViolation, ValueError)
    intern = validator(body(), ("body",))
    with pytest.raises(ValueError):
        intern.grade = "senior"


def test_the_constructor_enforces_checks_and_rules() -> None:
    assert Intern(salary=30_000, tenure_months=3, name="Ada", grade="intern").salary
    with pytest.raises(CheckViolation, match="at most 50000"):
        Intern(salary=60_000, tenure_months=3, name="Ada", grade="intern")
    with pytest.raises(CheckViolation, match="past six months"):
        Intern(salary=45_000, tenure_months=7, name="Ada", grade="intern")


def test_hydration_does_not_re_judge_a_row_the_database_already_holds() -> None:
    # _orm_set_loaded is the seam for values that are already proven. A row that
    # predates a rule must still be readable, or a deploy that adds a constraint
    # makes existing rows unloadable.
    intern = Intern._orm_new()
    intern._orm_set_loaded(2, 999_999)
    assert intern.salary == 999_999


def test_a_column_without_checks_is_nothing_but_its_type() -> None:
    # Not a micro-optimization but the contract: this module stays entirely off
    # the path of a column that declares no rules. Nothing is generated, and
    # the callable is the type's own coercion.
    class Plain(Model, table="constraint_plain"):
        id: Mapped[int] = column(Int64, primary_key=True)
        label: Mapped[str] = column(Text)

    label = Plain.__wreath_column_map__["label"]
    assert label.checks == ()
    assert not hasattr(label.validate, "__wreath_source__")
    assert label.validate is label.pg_type._coerce
    # ...and it enforces exactly what the public wrapper enforces.
    assert label.validate("x") == label.pg_type.coerce("x") == "x"
    with pytest.raises(TypeError):
        label.validate(5)


def test_an_overridden_coerce_is_not_skipped() -> None:
    # The unwrapping above is only sound because PgType.coerce is a wrapper
    # around _coerce. A type that overrides coerce means it, so it keeps it.
    from wreath.orm.constraints import coercer
    from wreath.orm.types import PgType

    class Shouty(PgType):
        def coerce(self, value: Any) -> Any:
            return super().coerce(value).upper()

    loud = Shouty("text", 25, "text", lambda v: v)
    assert coercer(loud)("x") == "X"
    plain = PgType("text", 25, "text", lambda v: v)
    assert coercer(plain) is plain._coerce


# -- checks --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "kind", "good", "bad"),
    [
        (Ge(10), "ge", 10, 9),
        (Gt(10), "gt", 11, 10),
        (Le(10), "le", 10, 11),
        (Lt(10), "lt", 9, 10),
    ],
)
def test_each_bound(check: Any, kind: str, good: int, bad: int) -> None:
    class Bounded(Model, table=f"constraint_bound_{kind}"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[int] = column(Int64, check=check)

    validate = Bounded.__wreath_column_map__["value"].validate
    assert validate(good) == good
    with pytest.raises(CheckViolation) as caught:
        validate(bad)
    assert caught.value.kind == kind


def test_length_bounds_a_string() -> None:
    class Named(Model, table="constraint_named"):
        id: Mapped[int] = column(Int64, primary_key=True)
        short: Mapped[str] = column(Text, check=Length(2, 4))
        floor: Mapped[str] = column(Text, check=Length(minimum=2))
        ceiling: Mapped[str] = column(Text, check=Length(maximum=4))

    columns = Named.__wreath_column_map__
    assert columns["short"].validate("abc") == "abc"
    for value in ("a", "abcde"):
        with pytest.raises(CheckViolation, match="between 2 and 4"):
            columns["short"].validate(value)
    with pytest.raises(CheckViolation, match="at least 2"):
        columns["floor"].validate("a")
    assert columns["floor"].validate("a" * 500) == "a" * 500
    with pytest.raises(CheckViolation, match="at most 4"):
        columns["ceiling"].validate("abcde")


def test_pattern_searches_a_string() -> None:
    class Coded(Model, table="constraint_coded"):
        id: Mapped[int] = column(Int64, primary_key=True)
        code: Mapped[str] = column(Text, check=Pattern(r"^[A-Z]{3}-\d+$"))

    validate = Coded.__wreath_column_map__["code"].validate
    assert validate("ABC-12") == "ABC-12"
    with pytest.raises(CheckViolation) as caught:
        validate("abc-12")
    assert caught.value.kind == "pattern"


def test_one_of_keeps_an_enum_in_python() -> None:
    # The database enforces 'text'; which strings mean something is an
    # application rule, so the schema stays portable.
    validate = Intern.__wreath_column_map__["grade"].validate
    assert validate("intern") == "intern"
    with pytest.raises(CheckViolation) as caught:
        validate("senior")
    assert caught.value.kind == "one_of"


def test_predicate_is_the_escape_hatch() -> None:
    class Even(Model, table="constraint_even"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[int] = column(
            Int64, check=Predicate(lambda v: v % 2 == 0, "value must be even", kind="even")
        )

    validate = Even.__wreath_column_map__["value"].validate
    assert validate(4) == 4
    with pytest.raises(CheckViolation) as caught:
        validate(5)
    assert caught.value.kind == "even"


def test_checks_run_in_declaration_order_and_the_first_failure_wins() -> None:
    # One violation per field, matching assignment, which can only raise once.
    class Ordered(Model, table="constraint_ordered"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[str] = column(Text, check=[Length(3), Pattern("^a")])

    validate = Ordered.__wreath_column_map__["value"].validate
    with pytest.raises(CheckViolation) as caught:
        validate("b")  # breaks both
    assert caught.value.kind == "length"


def test_a_check_sees_the_coerced_value_never_a_raw_one() -> None:
    # Coercion runs first, which is why Le(10) can assume two ints and compile
    # down to a comparison.
    seen: list[Any] = []

    class Watched(Model, table="constraint_watched"):
        id: Mapped[int] = column(Int64, primary_key=True)
        flag: Mapped[bool] = column(
            Bool, check=Predicate(lambda v: seen.append(type(v)) is None, "never fails")
        )

    validate = Watched.__wreath_column_map__["flag"].validate
    validate(True)
    assert seen == [bool]
    with pytest.raises(TypeError):
        validate("yes")  # refused by the type; the check never ran
    assert seen == [bool]


def test_null_skips_the_checks_entirely() -> None:
    # A nullable column with a bound: NULL is the absence of a value, not a
    # value that must clear the bound.
    class Sparse(Model, table="constraint_sparse"):
        id: Mapped[int] = column(Int64, primary_key=True)
        score: Mapped[int] = column(Int64, nullable=True, check=Ge(0))

    validated = compile_model_validator(Sparse)({"score": None}, ("body",))
    assert validated.score is None
    instance = Sparse._orm_new()
    instance.score = None
    assert instance.score is None


# -- declaration errors --------------------------------------------------------


def test_a_check_that_cannot_mean_anything_for_the_type_is_refused() -> None:
    with pytest.raises(DeclarationError, match="Length cannot apply to a int8 column"):

        class Bad(Model, table="constraint_bad_length"):
            id: Mapped[int] = column(Int64, primary_key=True)
            value: Mapped[int] = column(Int64, check=Length(2))

    with pytest.raises(DeclarationError, match="Ge cannot apply to a uuid column"):

        class AlsoBad(Model, table="constraint_bad_ge"):
            id: Mapped[int] = column(Int64, primary_key=True)
            key: Mapped[object] = column(Uuid, check=Ge(1))


def test_a_one_of_value_the_type_would_refuse_is_a_declaration_error() -> None:
    with pytest.raises(DeclarationError, match="is not a valid int8"):

        class Bad(Model, table="constraint_bad_one_of"):
            id: Mapped[int] = column(Int64, primary_key=True)
            value: Mapped[int] = column(Int64, check=OneOf(1, "two"))


def test_narrowing_a_column_the_model_does_not_have_is_a_declaration_error() -> None:
    with pytest.raises(DeclarationError, match="narrow\\('bonus'\\)"):

        class Bad(Employee, table="constraint_bad_narrow"):
            oops = narrow("bonus", Le(1))


def test_a_rule_over_a_column_the_model_does_not_have_is_a_declaration_error() -> None:
    with pytest.raises(DeclarationError, match="does not declare"):

        class Bad(Employee, table="constraint_bad_rule"):
            @rule("salary", "bonus")
            def impossible(salary: int, bonus: int) -> bool:
                """a rule over a column that is not there"""
                return True


def test_a_rule_without_a_message_is_a_declaration_error() -> None:
    with pytest.raises(DeclarationError, match="needs a message"):

        @rule("salary")
        def undocumented(salary: int) -> bool:
            return True


def test_junk_is_refused_at_declaration_time() -> None:
    with pytest.raises(DeclarationError, match="check= takes"):
        column(Int64, check="positive")
    with pytest.raises(DeclarationError, match="check= takes"):
        column(Int64, check=[Ge(1), "and positive"])
    with pytest.raises(DeclarationError, match="needs a minimum, a maximum"):
        Length()
    with pytest.raises(DeclarationError, match="can never hold"):
        Length(9, 2)
    with pytest.raises(DeclarationError, match="at least one allowed value"):
        OneOf()
    with pytest.raises(DeclarationError, match="not a regex"):
        Pattern("(unclosed")
    with pytest.raises(DeclarationError, match="declares no checks"):
        narrow("salary")
    with pytest.raises(DeclarationError, match="must name one of"):
        rule("salary", at="bonus")


# -- code generation -----------------------------------------------------------


def test_the_generated_validator_inlines_its_bounds() -> None:
    # The reason a check costs a comparison rather than a call: the whole chain
    # is one function, and a literal bound is a LOAD_CONST inside it.
    source = Intern.__wreath_column_map__["salary"].validate.__wreath_source__
    assert "value >= 0" in source
    assert "value <= 50000" in source
    assert source.count("def ") == 1


def test_a_failure_inside_a_generated_validator_shows_its_source() -> None:
    # Generated code is registered with linecache, so a traceback through it is
    # readable rather than a bare <string>.
    import traceback

    try:
        Intern.__wreath_column_map__["salary"].validate(60_000)
    except CheckViolation:
        text = traceback.format_exc()
    assert "wreath.orm.constraints:Intern.salary" in text
    assert "raise _CheckViolation" in text


def test_a_non_finite_float_bound_still_compiles() -> None:
    # repr(inf) is 'inf', which is a name rather than a literal and would
    # compile to a NameError if it were inlined.
    from wreath.orm.types import Float64

    class Bounded(Model, table="constraint_inf"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[float] = column(Float64, check=Lt(float("inf")))

    assert Bounded.__wreath_column_map__["value"].validate(1e308) == 1e308


def test_a_quote_in_a_bound_cannot_break_out_of_the_generated_source() -> None:
    class Quoted(Model, table="constraint_quoted"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[str] = column(Text, check=OneOf("it's \"fine\"", "also'fine"))

    validate = Quoted.__wreath_column_map__["value"].validate
    assert validate("it's \"fine\"") == "it's \"fine\""
    with pytest.raises(CheckViolation):
        validate("nope")


# -- through a route -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_rule_is_a_422_before_any_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[Intern], validate_schema="off")

    @app.post("/interns")
    async def create(
        request: Any,
        intern: Intern,
        session: Annotated[Session, FromORM("main", workload="write")],
    ) -> Any:
        raise AssertionError("the handler must not run for a body that breaks a rule")

    async with TestClient(app) as client:
        response = await client.post("/interns", json=body(salary=60_000))
    assert response.status == 422
    assert response.json()["errors"] == [
        {"loc": ["body", "salary"], "msg": "value must be at most 50000", "type": "le"}
    ]
    # A business rule is refused exactly where a type error is: before a
    # connection is leased.
    assert database.connection.calls == []
    assert database.acquired == 0
