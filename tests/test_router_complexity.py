from __future__ import annotations

import wreath.router as router_module
from wreath.router import Router


def _replacement_calls(depth: int) -> tuple[int, int]:
    calls = 0
    real_replace = router_module.replace

    def counting_replace(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_replace(*args, **kwargs)

    router_module.replace = counting_replace
    try:
        current = Router()
        for index in range(depth):
            parent = Router(prefix=f"/level-{index}")

            @parent.get(f"/route-{index}")
            async def endpoint(request):
                return "ok"

            parent.include_router(current)
            current = parent
        return calls, len(current.routes)
    finally:
        router_module.replace = real_replace


def test_nested_router_composition_is_linear_in_final_routes() -> None:
    calls_40, routes_40 = _replacement_calls(40)
    calls_80, routes_80 = _replacement_calls(80)

    assert routes_40 == 40
    assert routes_80 == 80
    assert calls_80 <= 2.2 * calls_40
