from __future__ import annotations

import inspect

import wreath.binding as binding
from wreath.binding import Depends


def _node(child):
    async def node(request, **kwargs):
        return 1

    params = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if child is not None:
        # Two parameters pointing at the *same* next-level callable: a shared
        # DAG, not a tree. There are `depth` distinct callables in total.
        kw = inspect.Parameter.KEYWORD_ONLY
        params.append(inspect.Parameter("a", kw, default=Depends(child)))
        params.append(inspect.Parameter("b", kw, default=Depends(child)))
    node.__signature__ = inspect.Signature(params)
    return node


def shared_binary_dag(depth: int):
    node = _node(None)
    for _ in range(depth):
        node = _node(node)
    return node


def signature_calls(root) -> int:
    count = 0
    real = binding.inspect.signature

    def counting(fn, *args, **kwargs):
        nonlocal count
        count += 1
        return real(fn, *args, **kwargs)

    binding.inspect.signature = counting
    try:
        binding._compile_dependency(root, ())
    finally:
        binding.inspect.signature = real
    return count


def test_shared_dependency_dag_compiles_each_callable_once() -> None:
    depth = 12
    assert signature_calls(shared_binary_dag(depth)) <= 2 * depth
