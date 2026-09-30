"""AST guard for inward dependencies and the two documented exception families.

The exception set is an inventory of exact module pairs, not permission for an
entire provider or service directory. Synthetic negative controls exercise the
same detector as the package scan, including nested and relative imports.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "wobblebot"

# Keep synchronized with the Architecture exceptions in CLAUDE.md and AGENTS.md.
_LLM_HELPERS_BY_ADAPTER = {
    "anthropic": {"llm_cloud_call", "llm_cost_gate", "llm_pricing", "llm_retry"},
    "anthropic_assistant": {"llm_cloud_call", "llm_cost_gate", "llm_pricing", "llm_retry"},
    "google": {"llm_cloud_call", "llm_cost_gate", "llm_pricing", "llm_retry"},
    "openai": {"llm_cloud_call", "llm_cost_gate", "llm_pricing", "llm_retry"},
    "ollama_cloud": {"llm_cloud_call", "llm_cost_gate", "llm_pricing", "llm_retry"},
    "ollama": {"llm_cloud_call"},
    "ollama_assistant": {"llm_cloud_call"},
    "moe_advisor": {"aggregators"},
    "fallback_advisor": {"aggregators", "llm_failures", "llm_trace"},
}
_ALLOWED_EDGES = {
    (f"wobblebot.adapters.{adapter}", f"wobblebot.services.{helper}")
    for adapter, helpers in _LLM_HELPERS_BY_ADAPTER.items()
    for helper in helpers
} | {("wobblebot.services.simulator", "wobblebot.adapters.mock_exchange")}
_CORE_LAYERS = {"domain", "ports", "services"}
_DRIVER_LAYERS = {"cli", "web"}
_DELIVERY_FRAMEWORKS = {"discord", "fastapi", "mcp", "starlette", "uvicorn"}


@dataclass(frozen=True)
class ImportEdge:
    """One static dependency, retaining its location for a useful failure."""

    source: str
    target: str
    line: int


def _module_name(path: Path, package_root: Path) -> str:
    parts = path.relative_to(package_root.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_edges(
    module: str, source: str, known_modules: set[str], *, is_package: bool = False
) -> Iterator[ImportEdge]:
    """Resolve both import forms without importing or executing project code."""
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportEdge(module, alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            for alias in node.names:
                # ``from package import submodule`` depends on the submodule;
                # ``from module import Symbol`` depends on the defining module.
                candidate = f"{base}.{alias.name}"
                target = candidate if candidate in known_modules else base
                yield ImportEdge(module, target, node.lineno)


def _is_forbidden(edge: ImportEdge) -> bool:
    source_parts = edge.source.split(".")
    if len(source_parts) < 2:
        return False
    source_layer = source_parts[1]
    if source_layer not in _CORE_LAYERS | {"adapters"}:
        return False
    target_parts = edge.target.split(".")
    if target_parts[0] != "wobblebot":
        return source_layer in _CORE_LAYERS and target_parts[0] in _DELIVERY_FRAMEWORKS
    if len(target_parts) < 2:
        return False
    target_layer = target_parts[1]
    if source_layer == "domain":
        return target_layer != "domain"
    if source_layer == "ports":
        return target_layer not in {"domain", "ports"}
    if target_layer in _DRIVER_LAYERS:
        return True
    if (source_layer, target_layer) in {("services", "adapters"), ("adapters", "services")}:
        return (edge.source, edge.target) not in _ALLOWED_EDGES
    return False


def _scan_package(package_root: Path) -> list[ImportEdge]:
    assert package_root.is_dir(), f"architecture guard root did not resolve: {package_root}"
    sources = {_module_name(path, package_root): path for path in package_root.rglob("*.py")}
    assert sources, f"architecture guard found no Python files: {package_root}"
    known_modules = set(sources)
    return [
        edge
        for module, path in sorted(sources.items())
        for edge in _import_edges(
            module,
            path.read_text(encoding="utf-8-sig"),
            known_modules,
            is_package=path.name == "__init__.py",
        )
    ]


def test_package_obeys_architecture_boundaries() -> None:
    edges = _scan_package(_PACKAGE_ROOT)
    violations = [edge for edge in edges if _is_forbidden(edge)]
    assert not violations, "Forbidden imports:\n" + "\n".join(
        f"{edge.source}:{edge.line} -> {edge.target}" for edge in violations
    )
    # Removing an exception's last use must also remove its permission; stale
    # allowances would otherwise make a later unrelated dependency invisible.
    observed = {(edge.source, edge.target) for edge in edges}
    assert _ALLOWED_EDGES <= observed, f"Unused exceptions: {_ALLOWED_EDGES - observed}"


@pytest.mark.parametrize(
    ("module", "source", "target"),
    [
        (
            "wobblebot.domain.policy",
            "import wobblebot.adapters.kraken_exchange as api",
            "wobblebot.adapters.kraken_exchange",
        ),
        (
            "wobblebot.domain.policy",
            "from ..services.grid_engine import GridEngine",
            "wobblebot.services.grid_engine",
        ),
        (
            "wobblebot.domain.nested.policy",
            "from ...ports.exchange import ExchangePort",
            "wobblebot.ports.exchange",
        ),
        ("wobblebot.domain.policy", "from wobblebot import config", "wobblebot.config"),
        ("wobblebot.ports.exchange", "from .. import adapters", "wobblebot.adapters"),
        (
            "wobblebot.ports.exchange",
            "from wobblebot.services import grid_engine",
            "wobblebot.services.grid_engine",
        ),
        (
            "wobblebot.services.worker",
            "def run():\n    from ..adapters import kraken_exchange",
            "wobblebot.adapters.kraken_exchange",
        ),
        (
            "wobblebot.services.worker",
            "if TYPE_CHECKING:\n    import wobblebot.cli.live",
            "wobblebot.cli.live",
        ),
        ("wobblebot.services.worker", "from wobblebot.web import app", "wobblebot.web"),
        (
            "wobblebot.services.llm_cloud_call",
            "from wobblebot.adapters.ollama import OllamaAdapter",
            "wobblebot.adapters.ollama",
        ),
        (
            "wobblebot.adapters.openai",
            "from wobblebot.services import harvester",
            "wobblebot.services.harvester",
        ),
        (
            "wobblebot.adapters.new_provider",
            "from wobblebot.services.llm_cloud_call import CloudCallConfig",
            "wobblebot.services.llm_cloud_call",
        ),
        (
            "wobblebot.adapters.ollama",
            "from wobblebot.services.llm_pricing import lookup_pricing",
            "wobblebot.services.llm_pricing",
        ),
        (
            "wobblebot.services.simulator",
            "from wobblebot.adapters.shadow_exchange import ShadowExchangeAdapter",
            "wobblebot.adapters.shadow_exchange",
        ),
        (
            "wobblebot.adapters.mock_exchange",
            "from wobblebot.services.simulator import SimulationResult",
            "wobblebot.services.simulator",
        ),
        ("wobblebot.services.worker", "from fastapi import FastAPI", "fastapi"),
        ("wobblebot.ports.exchange", "import mcp.server", "mcp.server"),
        ("wobblebot.domain.policy", "import discord", "discord"),
    ],
)
def test_detector_rejects_forbidden_imports(module: str, source: str, target: str) -> None:
    known_modules = {
        target,
        "wobblebot.adapters",
        "wobblebot.adapters.kraken_exchange",
        "wobblebot.config",
        "wobblebot.services.grid_engine",
        "wobblebot.services.harvester",
    }
    violations = [
        edge for edge in _import_edges(module, source, known_modules) if _is_forbidden(edge)
    ]
    assert [(edge.source, edge.target) for edge in violations] == [(module, target)]


@pytest.mark.parametrize(("module", "target"), sorted(_ALLOWED_EDGES))
def test_detector_permits_only_documented_exception_pairs(module: str, target: str) -> None:
    edges = list(_import_edges(module, f"from {target} import helper", {target}))
    assert len(edges) == 1
    assert not _is_forbidden(edges[0])


@pytest.mark.parametrize(
    ("module", "is_package", "forbidden"),
    [("wobblebot.services", True, True), ("wobblebot.services.simulator", False, False)],
)
def test_relative_package_imports_resolve_submodules(
    module: str, is_package: bool, forbidden: bool
) -> None:
    target = "wobblebot.adapters.mock_exchange"
    edges = list(
        _import_edges(
            module, "from ..adapters import mock_exchange", {target}, is_package=is_package
        )
    )
    assert edges[0].target == target
    assert _is_forbidden(edges[0]) is forbidden


def test_scanner_fails_if_source_root_is_missing(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="root did not resolve"):
        _scan_package(tmp_path / "missing")


def test_scanner_fails_if_source_root_is_empty(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="found no Python files"):
        _scan_package(tmp_path)


def test_package_root_import_is_outside_the_layer_rules() -> None:
    edge = ImportEdge("wobblebot", "importlib.metadata", 1)
    assert not _is_forbidden(edge)


def test_package_scan_detects_an_isolated_source_mutation(tmp_path: Path) -> None:
    """A delayed relative adapter import makes an otherwise clean fixture fail."""
    package_root = tmp_path / "wobblebot"
    domain_root = package_root / "domain"
    adapters_root = package_root / "adapters"
    domain_root.mkdir(parents=True)
    adapters_root.mkdir()
    policy = domain_root / "policy.py"
    policy.write_text("from decimal import Decimal\n", encoding="utf-8")
    (adapters_root / "kraken_exchange.py").write_text("", encoding="utf-8")
    assert not any(_is_forbidden(edge) for edge in _scan_package(package_root))

    policy.write_text(
        "from decimal import Decimal\ndef delayed():\n"
        "    from ..adapters import kraken_exchange\n",
        encoding="utf-8",
    )
    violations = [edge for edge in _scan_package(package_root) if _is_forbidden(edge)]
    assert violations == [
        ImportEdge("wobblebot.domain.policy", "wobblebot.adapters.kraken_exchange", 3)
    ]
