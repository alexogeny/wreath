from __future__ import annotations

import pytest

from wreath import Depends, Wreath
from wreath.flags import (
    FeatureFlags,
    Flag,
    FlagProvider,
    FlagSet,
    FlagView,
    OpenFeatureProvider,
    TypedFlagProvider,
    flags_dependency,
)
from wreath.testing import TestClient


def test_typed_mapping_flags_preserve_scalar_types_and_defaults() -> None:
    provider = FeatureFlags({"enabled": "on", "limit": "12", "ratio": "0.5"})
    flags = FlagSet(provider, (
        Flag("enabled", False), Flag("limit", 3), Flag("ratio", 1.0), Flag("mode", "safe")
    ))
    assert flags.value(Flag("enabled", False), {"id": "ada"}) is True
    assert flags.value(Flag("limit", 3)) == 12
    assert flags.value(Flag("ratio", 1.0)) == 0.5
    assert flags.value(Flag("mode", "safe")) == "safe"
    with pytest.raises(KeyError, match="not declared"):
        flags.value(Flag("typo", False))


def test_flag_set_resolves_the_canonical_declaration_not_the_callers_copy() -> None:
    class RecordingProvider:
        def __init__(self) -> None:
            self.seen: list[Flag] = []

        def resolve(self, flag: Flag, context=None):
            self.seen.append(flag)
            return flag.default

    provider = RecordingProvider()
    declared = Flag("Checkout_Timeout", 2.5)
    flags = FlagSet(provider, (declared,))

    assert flags.value(Flag("checkout_timeout", 99.0)) == 2.5
    assert provider.seen == [declared]


class OpenFeatureClient:
    def get_boolean_value(self, name, default, context):
        return not default

    def get_string_value(self, name, default, context):
        return "remote"

    def get_integer_value(self, name, default, context):
        return 9

    def get_float_value(self, name, default, context):
        return 0.25


def test_openfeature_bridge_selects_the_typed_sdk_method() -> None:
    provider = OpenFeatureProvider(OpenFeatureClient())
    assert provider.resolve(Flag("switch", False)) is True
    assert provider.resolve(Flag("mode", "local")) == "remote"
    assert provider.resolve(Flag("limit", 2)) == 9
    assert provider.resolve(Flag("ratio", 1.0)) == 0.25


async def test_one_provider_contract_serves_lifespan_dependencies_and_typed_flags() -> None:
    events: list[str] = []

    class Provider:
        async def start(self) -> None:
            events.append("start")

        async def close(self) -> None:
            events.append("close")

        def resolve(self, flag: Flag, context=None):
            return flag.name == "live" or flag.default

    app = Wreath()
    provider = app.flags(Provider())
    dependency = flags_dependency(provider)

    async def read_flags(request, flags):
        return {"live": flags.enabled("live")}

    read_flags.__defaults__ = (Depends(dependency),)
    app.get("/")(read_flags)

    async with TestClient(app) as client:
        response = await client.get("/")
        assert response.json() == {"live": True}
    assert events == ["start", "close"]


def test_original_boolean_provider_is_adapted_without_changing_its_identity() -> None:
    class BooleanOnly:
        def enabled(self, name, context=None):
            return name == "live"

    app = Wreath()
    original = BooleanOnly()
    provider = app.flags(original)

    assert isinstance(original, FlagProvider)
    assert not isinstance(original, TypedFlagProvider)
    assert provider is original
    assert app.state.flags is original
    assert FlagView(provider).enabled("live")


def test_mapping_provider_satisfies_both_public_provider_protocols() -> None:
    provider = FeatureFlags()

    assert isinstance(provider, FlagProvider)
    assert isinstance(provider, TypedFlagProvider)


def test_mapping_fast_paths_preserve_subclass_resolution_overrides() -> None:
    class CustomizedFlags(FeatureFlags):
        def __init__(self) -> None:
            super().__init__({"live": "on", "dark": "on"})
            self.seen: list[str] = []

        def resolve(self, flag, context=None):
            self.seen.append(flag.name)
            return False

    provider = CustomizedFlags()

    assert provider.enabled("live") is False
    assert provider.all() == {"live": False, "dark": False}
    assert provider.seen == ["live", "live", "dark"]


def test_boolean_provider_refuses_a_typed_flag_at_declaration_time() -> None:
    class BooleanOnly:
        def enabled(self, name, context=None):
            return True

    with pytest.raises(TypeError, match=r"limit.*TypedFlagProvider\.resolve"):
        FlagSet(BooleanOnly(), (Flag("limit", 3),))


def test_app_refuses_an_object_with_neither_provider_operation() -> None:
    with pytest.raises(TypeError, match=r"resolve\(flag, context\).*enabled"):
        Wreath().flags(object())


def test_app_refuses_a_second_feature_flag_provider() -> None:
    app = Wreath()
    app.flags(first="on")
    with pytest.raises(ValueError, match="exactly once"):
        app.flags(second="on")
