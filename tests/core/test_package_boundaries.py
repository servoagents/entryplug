"""Keep the exokernel import direction visible during future provider work."""

from __future__ import annotations

import ast
from pathlib import Path

import entryplug
from entryplug import api

SRC = Path(__file__).resolve().parents[2] / "src"


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_core_and_harbor_dependencies_point_inward() -> None:
    external = (
        "entryplug_harbor",
        "entryplug_evaluation",
        "entryplug_mcp",
        "entryplug_a2a",
        "entryplug_openenv",
    )
    for area in ("core", "embodiment", "harness"):
        for path in (SRC / "entryplug" / area).glob("*.py"):
            assert not any(
                module == prefix or module.startswith(prefix + ".")
                for module in _imports(path)
                for prefix in external
            ), path
    for path in (SRC / "entryplug" / "core").glob("*.py"):
        assert not any(
            module.startswith(("entryplug.embodiment", "entryplug.harness"))
            for module in _imports(path)
        ), path
    for path in (SRC / "entryplug_harbor").glob("*.py"):
        assert not any(
            module == "entryplug_evaluation" or module.startswith("entryplug_evaluation.")
            for module in _imports(path)
        ), path


def test_public_api_reexports_the_small_runtime_contract() -> None:
    for name in api.__all__:
        assert getattr(entryplug, name) is getattr(api, name)
