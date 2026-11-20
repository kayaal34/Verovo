"""V-10: every declared entry point still resolves after code removal.

The 0.14.1 cleanup deleted ``stats.py`` and ``prereg.py`` while ``analysis.py``
still imported them at module level; the cycle was only caught later. These
tests freeze the reference graph, not merely "the files exist":

- every module in the three importable packages imports cleanly;
- every relative import (including lazy ones inside functions) resolves to a
  real module;
- the declared console scripts resolve;
- the package imports from outside the checkout (no cwd luck).
"""
import ast
import importlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PACKAGES = {
    "buyer_ledger": REPO / "buyer-ledger" / "buyer_ledger",
    "verovo_lab": REPO / "lab" / "verovo_lab",
    "verovo_healthcheck": REPO / "healthcheck" / "verovo_healthcheck",
}

for package_dir in (REPO / "buyer-ledger", REPO / "lab", REPO / "healthcheck"):
    if str(package_dir) not in sys.path:
        sys.path.insert(0, str(package_dir))


def _module_name(package: str, path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([package] + parts)


def _resolve_relative(module_path: Path, root: Path, level: int,
                      name: str = "") -> Path:
    """Resolve ``from ..pkg.mod import name`` to a filesystem module path."""
    base = module_path.parent
    for _ in range(level - 1):
        base = base.parent
    for part in [item for item in str(name or "").split(".") if item]:
        base = base / part
    return base


class ModuleImportTests(unittest.TestCase):
    def test_every_module_imports(self):
        failures = []
        for package, root in PACKAGES.items():
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts or path.name == "__main__.py":
                    # `python -m` entry scripts execute at import by design.
                    continue
                name = _module_name(package, path, root)
                try:
                    importlib.import_module(name)
                except Exception as exc:  # noqa: BLE001 collect all, report once
                    failures.append("%s: %s: %s" % (name, type(exc).__name__, exc))
        self.assertEqual([], failures, "入口模块无法导入：\n" + "\n".join(failures))

    def test_relative_imports_resolve_including_lazy_ones(self):
        problems = []
        for package, root in PACKAGES.items():
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom) or not node.level:
                        continue
                    if node.module:
                        target = _resolve_relative(path, root, node.level,
                                                   node.module)
                        candidates = [target.with_suffix(".py"),
                                      target / "__init__.py"]
                        if not any(item.is_file() for item in candidates):
                            problems.append("%s: from %s%s import ... -> %s missing"
                                            % (path.relative_to(REPO),
                                               "." * node.level, node.module, target))
                        continue
                    # ``from . import stats``: each alias may be a submodule or
                    # an attribute defined in the package __init__.
                    package_dir = _resolve_relative(path, root, node.level, "")
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        if (package_dir / (alias.name + ".py")).is_file() or \
                                (package_dir / alias.name / "__init__.py").is_file():
                            continue
                        init_text = ""
                        if (package_dir / "__init__.py").is_file():
                            init_text = (package_dir / "__init__.py").read_text(
                                encoding="utf-8")
                        declared = ("%s =" % alias.name) in init_text or \
                            ("def %s" % alias.name) in init_text or \
                            ("class %s" % alias.name) in init_text
                        if not declared:
                            problems.append("%s: from %s import %s -> missing"
                                            % (path.relative_to(REPO),
                                               "." * node.level, alias.name))
        self.assertEqual([], problems, "相对导入指向不存在的模块：\n" + "\n".join(problems))

    def test_console_scripts_resolve(self):
        problems = []
        for pyproject in (REPO / "buyer-ledger" / "pyproject.toml",
                          REPO / "healthcheck" / "pyproject.toml"):
            if not pyproject.is_file():
                continue
            try:
                import tomllib
            except ImportError:  # Python 3.9
                tomllib = None
            if tomllib is not None:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                scripts = (data.get("project") or {}).get("scripts") or {}
            else:  # minimal fallback: read [project.scripts] lines
                scripts = {}
                in_section = False
                for line in pyproject.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("["):
                        in_section = stripped == "[project.scripts]"
                        continue
                    if in_section and "=" in stripped:
                        key, _, value = stripped.partition("=")
                        scripts[key.strip()] = value.strip().strip('"')
            for name, target in scripts.items():
                module = str(target).split(":")[0]
                try:
                    importlib.import_module(module)
                except Exception as exc:  # noqa: BLE001
                    problems.append("%s -> %s: %s" % (name, target, exc))
        self.assertEqual([], problems, "console script 目标无法导入：\n" + "\n".join(problems))

    def test_packages_import_outside_the_checkout(self):
        code = ("import buyer_ledger, buyer_ledger.cli, verovo_lab, "
                "verovo_healthcheck; print('ok')")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([
            str(REPO / "buyer-ledger"), str(REPO / "lab"),
            str(REPO / "healthcheck")]))
        with __import__("tempfile").TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, "-c", code], cwd=tmp,
                                  env=env, capture_output=True, text=True)
        self.assertEqual(0, proc.returncode,
                         proc.stdout + proc.stderr)
        self.assertIn("ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
