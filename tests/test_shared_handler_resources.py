import inspect

import pytest

from wreath import Wreath, binding


async def endpoint() -> str:
    return "ok"


def test_shared_requestless_handler_signature_is_inspected_per_route(monkeypatch):
    app = Wreath(ai_scraping="allow")
    for path in ("/first", "/second", "/third"):
        app.get(path)(endpoint)
    seen = []
    original = binding.inspect.signature

    def counted(target, *args, **kwargs):
        if target is endpoint:
            seen.append(target)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", counted)
    image = app._application_image
    assert image.binding_specs() == (None, None, None)
    assert image.return_annotations() == (str, str, str)
    assert image.requestless() == (True, True, True)
    assert len(seen) == 3


async def request_only(request) -> str:
    return "ok"


@pytest.mark.parametrize("handler, requestless", [(endpoint, True), (request_only, False)])
def test_shared_unbound_facts_ignore_path_and_host(handler, requestless, monkeypatch):
    app = Wreath(ai_scraping="allow")
    app.get("/first")(handler)
    app.get("/{unused}", host="{tenant}.example")(handler)
    app.get("/third", host="other.example")(handler)
    original = binding.inspect.signature
    calls = 0

    def signature(target, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", signature)
    image = app._application_image
    assert image.binding_specs() == (None,) * 3
    assert image.requestless() == (requestless,) * 3
    assert image.return_annotations() == (str,) * 3
    assert calls == 3
    app.get("/new")(handler)
    assert image.binding_specs() == (None,) * 4
    assert calls == 7


def test_bound_handler_is_reanalyzed_for_each_path_and_host():
    async def bound(request, id: int) -> str:
        return str(id)

    app = Wreath(ai_scraping="allow")
    app.get("/{id}")(bound)
    app.get("/query")(bound)
    app.get("/host", host="{id}.example")(bound)
    path, query, host = app._application_image.binding_specs()
    assert path.path_params == (("id", "id", int),)
    assert query.path_params == ()
    assert query.query_params[0][:3] == ("id", "id", int)
    assert host.path_params == (("id", "id", int),)
    assert path is not host


def test_distinct_handlers_are_not_conflated():
    async def integer() -> int:
        return 1

    app = Wreath(ai_scraping="allow")
    for path, handler in [
        ("/a", endpoint),
        ("/b", integer),
        ("/c", request_only),
        ("/d", endpoint),
    ]:
        app.get(path)(handler)
    assert app._application_image.return_annotations() == (str, int, str, str)
    assert app._application_image.requestless() == (True, True, False, True)


def test_dynamic_callable_signatures_remain_per_route():
    class Dynamic:
        calls = 0

        @property
        def __signature__(self):
            self.calls += 1
            parameters = (
                []
                if self.calls % 2
                else [inspect.Parameter("request", inspect.Parameter.POSITIONAL_ONLY)]
            )
            return inspect.Signature(parameters)

        async def __call__(self, *args):
            return "ok"

    dynamic = Dynamic()
    app = Wreath(ai_scraping="allow")
    for path in ("/a", "/b", "/c"):
        app.get(path)(dynamic)
    dynamic.calls = 0
    assert app._application_image.requestless() == (True, False, True)
    assert dynamic.calls == 3


@pytest.mark.parametrize("attribute", ["__signature__", "__wrapped__"])
def test_function_signature_overrides_are_not_reused(attribute, monkeypatch):
    async def handler():
        return "ok"

    setattr(handler, attribute, inspect.Signature() if attribute == "__signature__" else endpoint)
    app = Wreath(ai_scraping="allow")
    for path in ("/a", "/b", "/c"):
        app.get(path)(handler)
    original = binding.inspect.signature
    calls = 0

    def signature(target, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", signature)
    assert app._application_image.binding_specs() == (None,) * 3
    assert calls == 3


def test_string_annotations_are_not_reused(monkeypatch):
    async def handler():
        return "ok"

    handler.__annotations__ = {"return": "str"}
    app = Wreath(ai_scraping="allow")
    for path in ("/a", "/b", "/c"):
        app.get(path)(handler)
    original = binding.inspect.signature
    calls = 0

    def signature(target, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", signature)
    assert app._application_image.return_annotations() == (str,) * 3
    assert calls == 3


def test_dynamic_handler_cannot_leave_stale_shared_facts():
    async def handler() -> str:
        return "ok"

    class Dynamic:
        @property
        def __signature__(self):
            handler.__annotations__ = {"return": int}
            return inspect.Signature()

        async def __call__(self):
            return "ok"

    app = Wreath(ai_scraping="allow")
    app.get("/before")(handler)
    app.get("/dynamic")(Dynamic())
    app.get("/after")(handler)
    handler.__annotations__ = {"return": str}
    assert app._application_image.return_annotations() == (str, inspect.Parameter.empty, int)


def test_reanalysis_observes_changed_function_annotations():
    async def handler() -> str:
        return "ok"

    app = Wreath(ai_scraping="allow")
    app.get("/a")(handler)
    app.get("/b")(handler)
    assert app._application_image.return_annotations() == (str, str)
    handler.__annotations__ = {"return": int}
    app.get("/c")(handler)
    assert app._application_image.return_annotations() == (int, int, int)


def test_failing_dynamic_annotations_keep_existing_fallback():
    async def handler():
        return "ok"

    def annotations(format):
        raise ValueError("synthetic annotation failure")

    handler.__annotate__ = annotations
    app = Wreath(ai_scraping="allow")
    app.get("/a")(handler)
    app.get("/b")(handler)
    assert app._application_image.binding_specs() == (None, None)
    assert app._application_image.return_annotations() == (inspect.Parameter.empty,) * 2


def test_intervening_annotation_hook_refreshes_shared_handler():
    async def first():
        return "ok"

    async def middle():
        return "ok"

    first.__annotations__ = {"return": str}

    def annotate(format):
        first.__annotations__["return"] = int
        return {"return": str}

    middle.__annotate__ = annotate
    app = Wreath(ai_scraping="allow")
    app.get("/one")(first)
    app.get("/two")(middle)
    app.get("/three")(first)
    assert app._application_image.return_annotations() == (str, str, int)


def test_nonconsecutive_functions_are_inspected_again(monkeypatch):
    app = Wreath(ai_scraping="allow")
    for path, handler in [("/one", endpoint), ("/two", request_only), ("/three", endpoint)]:
        app.get(path)(handler)
    original = binding.inspect.signature
    calls = []

    def signature(target, *args, **kwargs):
        calls.append(target)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", signature)
    assert app._application_image.return_annotations() == (str, str, str)
    assert calls == [endpoint, request_only, endpoint]


def test_annotation_dictionary_hooks_remain_per_route(monkeypatch):
    async def handler():
        return "ok"

    class Annotations(dict):
        def values(self):
            raise AssertionError("cache admission must not invoke annotation mapping hooks")

    handler.__annotations__ = Annotations({"return": str})
    app = Wreath(ai_scraping="allow")
    app.get("/one")(handler)
    app.get("/two")(handler)
    original = binding.inspect.signature
    calls = 0

    def signature(target, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", signature)
    assert app._application_image.return_annotations() == (str, str)
    assert calls == 2


def test_consecutive_lazy_annotation_hook_matches_uncached_resolution():
    async def handler():
        return "ok"

    calls = 0

    def annotations(format):
        nonlocal calls
        calls += 1
        return {"return": str if calls == 1 else int}

    handler.__annotate__ = annotations
    app = Wreath(ai_scraping="allow")
    app.get("/one")(handler)
    app.get("/two")(handler)
    app.get("/three")(handler)
    assert app._application_image.return_annotations() == (str, str, str)
    assert calls == 1


def test_annotation_key_subclasses_are_not_reused(monkeypatch):
    class Key(str):
        pass

    async def handler():
        return "ok"

    handler.__annotations__ = {Key("return"): str}
    app = Wreath(ai_scraping="allow")
    app.get("/one")(handler)
    app.get("/two")(handler)
    original = binding.inspect.signature
    calls = 0

    def signature(target, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", signature)
    assert app._application_image.return_annotations() == (str, str)
    assert calls == 2


def test_same_function_annotation_hook_can_change_its_code():
    async def handler():
        return "ok"

    async def replacement(request):
        return "ok"

    def annotations(format):
        handler.__code__ = replacement.__code__
        return {"return": str}

    handler.__annotate__ = annotations
    app = Wreath(ai_scraping="allow")
    app.get("/one")(handler)
    app.get("/two")(handler)
    assert app._application_image.requestless() == (True, False)


@pytest.mark.parametrize("keyword_only", [False, True])
def test_default_bearing_function_signatures_are_not_reused(monkeypatch, keyword_only):
    if keyword_only:

        async def handler(*, request=None) -> str:
            return "ok"
    else:

        async def handler(request=None) -> str:
            return "ok"

    app = Wreath(ai_scraping="allow")
    app.get("/one")(handler)
    app.get("/two")(handler)
    original = binding.inspect.signature
    calls = 0

    def signature(target, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(target, *args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", signature)
    assert app._application_image.return_annotations() == (str, str)
    assert calls == 2
