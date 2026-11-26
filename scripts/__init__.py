"""Importable runtime package for Second Brain.

Source checkouts import these modules as ``scripts.<name>``. Wheel installs
keep the same files under ``second_brain.scripts`` and alias that package to
``scripts`` from ``second_brain.cli.bootstrap_runtime``. Modules do not add
this directory to ``sys.path``.

A bare ``import atom_mutator`` (and the other module names in this directory)
resolves to the same module object as ``scripts.atom_mutator``, so existing
tests can keep patching the short name.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_SHORT_NAMES = frozenset(
    path.stem for path in _DIR.glob("*.py") if path.stem != "__init__"
)


class _ReuseLoader(importlib.abc.Loader):
    def __init__(self, module: object) -> None:
        self._module = module

    def create_module(self, spec):  # noqa: ANN001
        return self._module

    def exec_module(self, module) -> None:  # noqa: ANN001
        return None


class _PackageAliasFinder(importlib.abc.MetaPathFinder):
    """Publish each scripts module under its historical short name."""

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001
        if path is not None or fullname not in _SHORT_NAMES:
            return None
        sibling = sys.modules.get(f"scripts.{fullname}")
        if sibling is None:
            sibling = importlib.import_module(f"scripts.{fullname}")
        return importlib.machinery.ModuleSpec(fullname, _ReuseLoader(sibling))


if not any(isinstance(finder, _PackageAliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _PackageAliasFinder())

