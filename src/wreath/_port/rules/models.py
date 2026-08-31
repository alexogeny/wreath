"""Pydantic models: `BaseModel` classes, their fields, validators and config."""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED

PYDANTIC: dict[str, tuple[str, str, str, str]] = {
    "pydantic.model": ("model", "pydantic_models", TRANSLATED, "class X(BaseModel) -> @dataclass"),
    # A dataclass has one slot per field -- the default -- so a `Field(...)`
    # translates when everything it carries is a default. Documentation and
    # constraints now have a runtime home in Annotated[wreath.binding.Field].
    "pydantic.field": (
        "field",
        "pydantic_models",
        TRANSLATED,
        "plain field maps 1:1: `Field(default=x)`/`Field(x)` -> `= x`, `Field(default_factory=f)` -> `= field(default_factory=f)`, and a list/dict/set default -> field(default_factory=...). Keep descriptions, examples, aliases and constraints in Annotated[T, wreath.binding.Field(...)].",
    ),
    "pydantic.model_kw_only": (
        "model",
        "pydantic_models",
        NEEDS_REVIEW,
        "A field with no default is declared after one that has a default. Pydantic does not mind; a dataclass refuses to be built at all. This is now @dataclass(kw_only=True), which fixes it and is how wreath builds request bodies anyway. The one thing to check: anything that constructs this model with positional arguments has to switch to keywords.",
    ),
    "pydantic.model_kw_only_exact": (
        "model",
        "pydantic_models",
        TRANSLATED,
        "This becomes @dataclass(kw_only=True). No call in the analyzed tree constructs the model positionally, so making its existing keyword-only behavior explicit changes no call site.",
    ),
    "pydantic.field_marker": (
        "field",
        "pydantic_models",
        NEEDS_REVIEW,
        "Move alias, description and examples to Annotated[T, wreath.binding.Field(...)]. discriminator=, exclude= and strict= still need a design decision.",
    ),
    "pydantic.field_constraint": (
        "field",
        "pydantic_models",
        NEEDS_REVIEW,
        "Move gt/ge/lt/le, min_length/max_length and pattern to Annotated[T, wreath.binding.Field(...)]. Keep a matching ORM check as well when the value is persisted.",
    ),
    "pydantic.field_metadata_exact": (
        "field",
        "pydantic_models",
        TRANSLATED,
        "Field aliases and validation metadata map directly to Annotated[T, wreath.binding.Field(...)]; the ordinary dataclass default remains outside Annotated.",
    ),
    "pydantic.config_forbid": (
        "config",
        "other",
        TRANSLATED,
        "Drop extra='forbid'. Rejecting unknown fields is already what wreath does.",
    ),
    "pydantic.config_ignore": (
        "config",
        "other",
        NEEDS_REVIEW,
        "extra='ignore' means unknown fields were dropped quietly. Wreath always rejects them with a 422 and cannot be told otherwise, so any client sending extra keys will start getting errors. Either stop sending them or add the fields to the model.",
    ),
    "pydantic.config_class": (
        "config",
        "other",
        NEEDS_REVIEW,
        "This is pydantic v1's nested Config class. Delete it. Rejecting unknown fields is already wreath's behaviour; anything else it set has to be moved by hand.",
    ),
    "pydantic.validator": (
        "validator",
        "other",
        NEEDS_REVIEW,
        "A validator is code, so it has to be moved by hand. For a rule about one field, use narrow() on the column; for a rule spanning fields, use @rule(). Both run once when the app starts rather than on every request.",
    ),
    "pydantic.validator_literal": (
        "validator",
        "other",
        TRANSLATED,
        "This validator only repeats the field's Literal members. Delete it: Wreath binding validates the same closed set from the annotation.",
    ),
    "pydantic.partial": (
        "model_as_partial",
        "other",
        NEEDS_REVIEW,
        "This model is generated through model_as_partial(), and Wreath has no "
        "equivalent model primitive. Keep this Pydantic model family together "
        "until the partial request dataclass is written explicitly.",
    ),
    "pydantic.get_pydantic": (
        "get_pydantic",
        "other",
        UNSUPPORTED,
        "This get_pydantic() shape is dynamic or feeds another model transformer, so its resulting fields cannot be proved statically. Write out the dataclass or replace the dynamic step with model_dataclass().",
    ),
    "pydantic.get_pydantic_exact": (
        "get_pydantic",
        "other",
        TRANSLATED,
        "A literal include=/exclude= projection becomes model_dataclass(Model, ..., name=...). It compiles an ordinary keyword-only dataclass once at declaration time for binding, OpenAPI, and type generation.",
    ),
}
