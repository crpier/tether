"""Enforce the one-interface-per-Integration rule (ADR-0025).

Each Integration package presents exactly one interface: its ``__init__.py``.
Production modules outside the package must import the package, never its
submodules (or the legacy flat modules). The Integration's own tests may cross
internal seams, so ``tests/`` is deliberately out of scope here.
"""

import ast
from pathlib import Path

from snektest import test

TETHER_ROOT = Path(__file__).resolve().parent.parent / "tether"

# Integration packages. Legacy flat modules (`tether.gmail_store`, …) no
# longer exist; consumers import through the package interface.
INTEGRATIONS = {"youtube", "gmail", "health_connect", "transcripts"}


def _dotted_module(path: Path) -> str:
    rel = path.relative_to(TETHER_ROOT)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["tether", *parts])


def _home_cluster(module: str) -> str | None:
    for cluster in INTEGRATIONS:
        if module == f"tether.{cluster}" or module.startswith(f"tether.{cluster}."):
            return cluster
    return None


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


@test()
def integration_internals_stay_behind_the_package_interface() -> None:
    violations: list[str] = []
    for path in sorted(TETHER_ROOT.rglob("*.py")):
        module = _dotted_module(path)
        home = _home_cluster(module)
        tree = ast.parse(path.read_text())
        for imported in _imported_modules(tree):
            cluster = _home_cluster(imported)
            if cluster is None or cluster == home:
                continue
            # Importing the package itself is using the interface.
            if imported == f"tether.{cluster}":
                continue
            violations.append(f"{module} imports Integration internal {imported}")
    assert not violations, "\n".join(violations)
