"""Clean wheel runtime shim for the `second-brain` CLI entrypoint.

In wheel installs, Second Brain's runtime modules live inside the `second_brain`
package (`second_brain/scripts`, `second_brain/dashboard`, `second_brain/plugins`)
so `site-packages/` is never polluted with top-level `scripts/`, `dashboard/`, or
`plugins/` directories. In editable/source checkouts where `second_brain/scripts`
does not exist on disk, this shim resolves `<repo>/scripts` directly.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys


def bootstrap_runtime() -> Path:
    """Make ``import scripts`` resolve without putting the scripts directory on ``sys.path``.

    A wheel stores the modules at ``second_brain.scripts``. A source checkout
    already has a top-level ``scripts`` package when the repo root is importable.
    """
    pkg_dir = Path(__file__).resolve().parent
    bundled_scripts = pkg_dir / "scripts"
    repo_root = pkg_dir.parent
    repo_scripts = repo_root / "scripts"

    if bundled_scripts.is_dir() and (bundled_scripts / "config.py").is_file() and not repo_scripts.is_dir():
        pkg = importlib.import_module("second_brain.scripts")
        sys.modules.setdefault("scripts", pkg)
        return bundled_scripts
    if repo_scripts.is_dir():
        try:
            importlib.import_module("scripts")
        except ModuleNotFoundError:
            sys.path.insert(0, str(repo_root))
            importlib.import_module("scripts")
        return repo_scripts
    raise RuntimeError(f"Second Brain runtime scripts directory not found near {pkg_dir}")


def main(argv: list[str] | None = None) -> int:
    bootstrap_runtime()
    from scripts import cli as _scripts_cli

    return _scripts_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
