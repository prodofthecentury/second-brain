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
    """Configure `sys.path` and `sys.modules['scripts']` for wheel or repo execution."""
    pkg_dir = Path(__file__).resolve().parent
    bundled_scripts = pkg_dir / "scripts"
    repo_root = pkg_dir.parent
    repo_scripts = repo_root / "scripts"

    if bundled_scripts.is_dir():
        scripts_dir = bundled_scripts
        pkg_str = str(pkg_dir)
        if pkg_str not in sys.path:
            sys.path.insert(0, pkg_str)
        scripts_str = str(scripts_dir)
        if sys.path[:1] != [scripts_str]:
            if scripts_str in sys.path:
                sys.path.remove(scripts_str)
            sys.path.insert(0, scripts_str)
        sys.modules.setdefault("scripts", importlib.import_module("second_brain.scripts"))
    elif repo_scripts.is_dir():
        scripts_dir = repo_scripts
        repo_str = str(repo_root)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)
        scripts_str = str(scripts_dir)
        if sys.path[:1] != [scripts_str]:
            if scripts_str in sys.path:
                sys.path.remove(scripts_str)
            sys.path.insert(0, scripts_str)
    else:
        raise RuntimeError(f"Second Brain runtime scripts directory not found near {pkg_dir}")

    return scripts_dir


def main(argv: list[str] | None = None) -> int:
    bootstrap_runtime()
    from scripts import cli as _scripts_cli

    return _scripts_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
