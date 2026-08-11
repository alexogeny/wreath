"""Request parameters: the `Query`/`Path`/`Header`/`Cookie`/`File` markers, the
request body, and form binding.
"""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

PARAMS: dict[str, tuple[str, str, str, str]] = {
    # -- params ---------------------------------------------------------------
    "param.query": (
        "param",
        "params",
        TRANSLATED,
        "Query(default, ge=, le=) -> Annotated[T, Query(minimum=, maximum=)] = default",
    ),
    "param.query_strconstraint": (
        "param",
        "params",
        NEEDS_REVIEW,
        "Wreath's Query marker carries a minimum and a maximum for numbers and nothing else, so a length or pattern rule on a query parameter has no home. Either check it in the handler and raise UnprocessableEntity, or move the value into a request body where a model can validate it.",
    ),
    "param.path": ("param", "params", TRANSLATED, "Path(...) -> Annotated[T, Path()]"),
    "param.header": ("param", "params", TRANSLATED, "Header(...) -> Annotated[T, Header(alias=)]"),
    "param.cookie": ("param", "params", TRANSLATED, "Cookie(...) -> Annotated[T, Cookie()]"),
    "param.form": ("param", "params", TRANSLATED, "Form(...) -> Annotated[T, Form()]"),
    "param.file": (
        "param",
        "params",
        TRANSLATED,
        "File()/UploadFile -> Annotated[UploadFile, File()]",
    ),
    "param.body": (
        "param",
        "params",
        TRANSLATED,
        "A parameter annotated with a model is the request body, with no marker needed. The model becomes a dataclass.",
    ),
    "param.body_embed": (
        "param",
        "params",
        NEEDS_REVIEW,
        "embed=True wraps the body in a single key named after the parameter, and wreath has no switch for it. Either add that wrapping field to the model, or drop embed and send the object unwrapped.",
    ),
}

FORMS: dict[str, tuple[str, str, str, str]] = {
    "form.as_form": (
        "form_binding",
        "other",
        TRANSLATED,
        "Delete the as_form decorator. A parameter written Annotated[Model, Form()] binds a whole multipart form to the model.",
    ),
}
