"""AST helpers every other analysis leans on: parent links and constant truth."""

from __future__ import annotations

import ast


def parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """`id(child) -> parent` for one module, built in a single walk."""
    return {
        id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False
