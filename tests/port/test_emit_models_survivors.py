from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")


def _emit(source: str, *, opinionated: bool = False) -> str:
    emitted = port.emit_module(source, opinionated=opinionated)
    compile(emitted, "<ported>", "exec", dont_inherit=True)
    return emitted


def test_settings_rewrite_preserves_every_non_settings_shape() -> None:
    context = port.TreeContext(
        index={
            "pydantic": set(),
            "settings": {"Nested", "Settings"},
            "orm": set(),
            "orm_mixin": set(),
        }
    )
    source = (
        "from pydantic import Field\n"
        "from pydantic_settings import BaseSettings\n\n\n"
        "class Mixin:\n"
        "    pass\n\n\n"
        "class Nested(BaseSettings):\n"
        "    value: int\n\n\n"
        "class Settings(Mixin, BaseSettings):\n"
        "    class Other:\n"
        "        pass\n"
        "    class Config:\n"
        "        env_prefix = 'APP_'\n"
        "    left = model_config = object()\n"
        "    model_config = right = object()\n"
        "    holder.model_config: object = object()\n"
        "    holder.count: int = Field(default=1, env='HOLDER_COUNT')\n"
        "    count: int = Field(default=0, description='count', env='COUNT')\n"
        "    plain: int = Field(default=1)\n"
        "    nested: Nested = Nested()\n"
        "    other: Other = Other()\n"
        "    qualified: Nested = module.Nested()\n"
        "    positional: Nested = Nested(1)\n"
        "    keyword: Nested = Nested(value=1)\n"
        "    tags: list[str] = []\n"
    )
    emitted = port.emit_module(source, context)
    compile(emitted, "<ported>", "exec", dont_inherit=True)

    assert "class Settings(Mixin):" in emitted
    assert "class Other:" in emitted
    assert "class Config:" not in emitted
    assert "left = model_config = object()" in emitted
    assert "model_config = right = object()" in emitted
    assert "holder.model_config: object = object()" in emitted
    assert "holder.count: int = Field(default=1, env='HOLDER_COUNT')" in emitted
    assert "Env('COUNT')" in emitted
    assert "Env('count')" not in emitted
    assert "plain: int = 1" in emitted
    assert "nested: Nested = field(default_factory=Nested)" in emitted
    assert "other: Other = Other()" in emitted
    assert "qualified: Nested = module.Nested()" in emitted
    assert "positional: Nested = Nested(1)" in emitted
    assert "keyword: Nested = Nested(value=1)" in emitted
    assert "tags: list[str] = field(default_factory=list)" in emitted
    assert "[settings.field_complex]" not in emitted
    assert "[settings.nested]" not in emitted


@pytest.mark.parametrize(
    "base",
    [
        "Factory.build(1)",
        "Factory.build(flag=True)",
        "Factory.model_as_partial(1)",
        "Factory.model_as_partial(flag=True)",
    ],
)
def test_only_the_exact_partial_model_base_is_claimed(base: str) -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Factory:\n"
        "    pass\n\n\n"
        f"class Patch(BaseModel, {base}):\n"
        "    value: int\n"
    )

    assert base in emitted


def test_partial_model_family_is_left_together_for_review() -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Factory:\n"
        "    pass\n\n\n"
        "class Patch(BaseModel, Factory.model_as_partial()):\n"
        "    value: int\n"
    )

    assert "class Patch(BaseModel, Factory.model_as_partial()):" in emitted
    assert "[pydantic.partial]" in emitted


def test_validator_rewrite_refuses_dynamic_and_async_markers() -> None:
    emitted = _emit(
        "import builtins\n"
        "from builtins import classmethod as cm\n"
        "from pydantic import BaseModel, field_validator\n\n\n"
        "def other(*fields):\n"
        "    return lambda function: function\n\n\n"
        "field_name = 'value'\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    @other('value')\n"
        "    def untouched(value):\n"
        "        return value\n"
        "    @field_validator(field_name)\n"
        "    def dynamic(value):\n"
        "        return value\n"
        "    @field_validator('value', 1)\n"
        "    def mixed(value):\n"
        "        return value\n"
        "    @field_validator('value')\n"
        "    async def asynchronous(value):\n"
        "        return value\n"
        "    @field_validator()\n"
        "    def no_fields(value):\n"
        "        return value\n"
        "    @field_validator('value')\n"
        "    @staticmethod\n"
        "    def static(value):\n"
        "        return value\n"
        "    @field_validator('value')\n"
        "    @builtins.classmethod\n"
        "    def qualified(cls, value):\n"
        "        return value\n"
        "    @field_validator('value')\n"
        "    @cm()\n"
        "    def exact(cls, value):\n"
        "        return value + 1\n"
        "    def __post_init__(self):\n"
        "        self.value *= 2\n"
    )

    assert "@other('value')" in emitted
    assert "@field_validator(field_name)" in emitted
    assert "@field_validator('value', 1)" in emitted
    assert "async def asynchronous" in emitted
    assert "@field_validator('value')\n    async def asynchronous" in emitted
    assert "@field_validator()" in emitted
    assert "@staticmethod" in emitted
    assert "@builtins.classmethod" not in emitted
    assert "@cm" not in emitted
    assert "self.value = self.exact(self.value)" in emitted
    assert emitted.index("self.value = self.exact") < emitted.index("self.value *= 2")


@pytest.mark.parametrize(
    "extra_body",
    [
        "        print('runtime')\n",
        "        left = right = True\n",
        "        holder.option = True\n",
        "        from_attributes = other = True\n",
        "        7\n",
    ],
)
def test_config_class_with_unrecognised_statements_stays_visible(extra_body: str) -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    class Config:\n"
        "        from_attributes = True\n"
        f"{extra_body}"
    )

    assert "class Config:" in emitted
    assert "[pydantic.config_class]" in emitted


@pytest.mark.parametrize("body", ["        pass\n", "        title = 'public'\n"])
def test_empty_or_unsafe_config_class_stays_visible(body: str) -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    class Config:\n"
        f"{body}"
    )

    assert "class Config:" in emitted
    assert "[pydantic.config_class]" in emitted


def test_docstring_does_not_make_an_empty_config_redundant() -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    class Config:\n"
        "        'configuration'\n"
    )

    assert "class Config:" in emitted
    assert "[pydantic.config_class]" in emitted


def test_docstring_is_allowed_beside_safe_config_assignments() -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    class Config:\n"
        "        'configuration'\n"
        "        from_attributes = True\n"
    )

    assert "class Config:" not in emitted
    assert "[pydantic.config_class]" not in emitted


def test_only_a_name_target_can_be_model_config() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class Holder:\n"
        "    pass\n\n\n"
        "holder = Holder()\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    holder.model_config: object = ConfigDict(extra='forbid')\n"
        "    holder.model_config = ConfigDict(extra='forbid')\n"
        "    unrelated = ConfigDict(extra='forbid')\n"
    )

    assert "holder.model_config: object = ConfigDict(extra='forbid')" in emitted
    assert "holder.model_config = ConfigDict(extra='forbid')" in emitted
    assert "unrelated = ConfigDict(extra='forbid')" in emitted


def test_non_field_annotation_and_callable_default_stay_unchanged() -> None:
    emitted = _emit(
        "from pydantic import BaseModel\n\n\n"
        "def make_value():\n"
        "    return 1\n\n\n"
        "class Model(BaseModel):\n"
        "    required: int\n"
        "    made: int = make_value()\n"
    )

    assert "required: int" in emitted
    assert "made: int = make_value()" in emitted


def test_inexact_field_metadata_stays_for_review() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, Field\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int = Field(default=1, multiple_of=2)\n"
    )

    assert "Field(default=1, multiple_of=2)" in emitted
    assert "[pydantic.field_constraint]" in emitted


@pytest.mark.parametrize(
    "config",
    [
        "model_config: object",
        "model_config = ConfigDict()",
        "model_config = ConfigDict(**options)",
        "model_config = ConfigDict(title='public')",
    ],
)
def test_non_redundant_model_config_stays_visible(config: str) -> None:
    emitted = _emit(
        "from pydantic import BaseModel, ConfigDict\n\n"
        "options = {}\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        f"    {config}\n"
    )

    assert config in emitted


def test_only_safe_nonempty_model_config_is_removed() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    model_config = ConfigDict(from_attributes=True, use_enum_values=True)\n"
    )

    assert "model_config" not in emitted
    assert "ConfigDict" not in emitted


def test_safe_annotated_model_config_is_removed() -> None:
    emitted = _emit(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class Model(BaseModel):\n"
        "    value: int\n"
        "    model_config: object = ConfigDict(from_attributes=True)\n"
    )

    assert "model_config" not in emitted
    assert "ConfigDict" not in emitted


def test_opinionated_mode_keeps_unknown_extra_and_only_drops_ignore() -> None:
    allowed = _emit(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class Model(BaseModel):\n"
        "    model_config = ConfigDict(extra='allow')\n"
        "    value: int\n",
        opinionated=True,
    )
    ignored = _emit(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class Model(BaseModel):\n"
        "    model_config = ConfigDict(extra='ignore')\n"
        "    value: int\n"
    )

    assert "ConfigDict(extra='allow')" in allowed
    assert "[pydantic.config_ignore]" in ignored
