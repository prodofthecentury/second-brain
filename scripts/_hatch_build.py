"""Hatchling wheel build hook for Second Brain.

Maps runtime directories (`scripts/`, `dashboard/`, `plugins/`, `mobile/`,
`.streamlit/config.toml`) under the `second_brain/` package namespace on
standard wheel builds while excluding test/canary suites, bytecode caches,
secrets/local configs, mobile build/test/fastlane artifacts, symlinks/reparse
points, and `node_modules`, and keeping editable (`uv sync`) installs clean.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # pragma: no cover - build backend is isolated during `uv build`
    class BuildHookInterface:  # type: ignore[no-redef]
        root: str = "."

_EXCLUDED_SCRIPT_NAMES: frozenset[str] = frozenset(
    {
        "_hatch_build.py",
        "test_all.py",
        "smoke_test.py",
        "run_desktop_tests.py",
        "run_vscode_tests.py",
    }
)
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".gradle",
        ".pytest_cache",
        "Pods",
        "build",
        "DerivedData",
    }
)
_EXCLUDED_MOBILE_DIR_NAMES: frozenset[str] = frozenset({"test", "tests", "androidTest", "fastlane"})
_EXCLUDED_MOBILE_REL_PREFIXES: tuple[str, ...] = (
    "vault-client/android/app/src/main/assets/public/",
    "vault-client/ios/App/App/public/",
)
_EXCLUDED_MOBILE_FILENAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "gradle-wrapper.jar",
    }
)
_FORBIDDEN_SECRET_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        "local.properties",
        "google-services.json",
        "play-key.json",
        "mcp_config.json",
    }
)
_FORBIDDEN_SECRET_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".jks",
    ".keystore",
    ".p8",
    ".sbvault",
    ".bak",
)


def _is_reparse_or_link(path: Path | str) -> bool:
    p = str(path)
    if os.path.islink(p):
        return True
    isj = getattr(os.path, "isjunction", None)
    if callable(isj):
        try:
            if isj(p):
                return True
        except OSError:
            pass
    if os.name == "nt":
        try:
            st = os.lstat(p)
            if bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return True
        except OSError:
            pass
    return False


def _is_forbidden_secret_or_temp(filename: str) -> bool:
    low = filename.lower()
    if low in _FORBIDDEN_SECRET_FILENAMES or low.startswith(".env."):
        return True
    if low.endswith(_FORBIDDEN_SECRET_SUFFIXES) or ".bak-" in low:
        return True
    if low.endswith((".pyc", ".pyo", ".ds_store")) or low.startswith(".tmp"):
        return True
    return False


def _should_skip_script(filename: str) -> bool:
    return (
        not filename.endswith(".py")
        or filename in _EXCLUDED_SCRIPT_NAMES
        or filename.endswith(("_tests.py", "_canary.py"))
    )


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return

        root = Path(self.root)
        force_include: dict[str, str] = build_data.setdefault("force_include", {})

        for src_dir_name, dst_prefix in (
            ("scripts", "second_brain/scripts"),
            ("dashboard", "second_brain/dashboard"),
            ("plugins", "second_brain/plugins"),
            ("mobile", "second_brain/mobile"),
        ):
            src_root = root / src_dir_name
            if not os.path.lexists(str(src_root)):
                continue
            if _is_reparse_or_link(src_root):
                raise PermissionError(f"Refusing to bundle symlinked build directory: {src_root}")
            if not src_root.is_dir():
                continue

            for dirpath, dirnames, filenames in os.walk(str(src_root), followlinks=False):
                cur_dir = Path(dirpath)
                if _is_reparse_or_link(cur_dir):
                    raise PermissionError(f"Refusing to traverse symlinked directory in build: {cur_dir}")

                kept_dirs: list[str] = []
                for dname in sorted(dirnames):
                    d_full = cur_dir / dname
                    if _is_reparse_or_link(d_full):
                        raise PermissionError(f"Refusing to bundle symlinked subdirectory: {d_full}")
                    if dname in _EXCLUDED_DIR_NAMES:
                        continue
                    if src_dir_name == "mobile" and dname in _EXCLUDED_MOBILE_DIR_NAMES:
                        continue
                    rel_dir_posix = d_full.relative_to(src_root).as_posix() + "/"
                    if src_dir_name == "mobile" and any(
                        rel_dir_posix.startswith(pref) for pref in _EXCLUDED_MOBILE_REL_PREFIXES
                    ):
                        continue
                    kept_dirs.append(dname)
                dirnames[:] = kept_dirs

                for fname in sorted(filenames):
                    path = cur_dir / fname
                    if _is_reparse_or_link(path):
                        raise PermissionError(f"Refusing to bundle symlinked file: {path}")
                    if not path.is_file():
                        continue
                    if _is_forbidden_secret_or_temp(fname):
                        continue
                    if src_dir_name == "scripts" and _should_skip_script(fname):
                        continue
                    rel = path.relative_to(src_root)
                    rel_posix = rel.as_posix()
                    if src_dir_name == "mobile":
                        if fname in _EXCLUDED_MOBILE_FILENAMES:
                            continue
                        if any(rel_posix.startswith(pref) for pref in _EXCLUDED_MOBILE_REL_PREFIXES):
                            continue
                    force_include[str(path)] = f"{dst_prefix}/{rel_posix}"

        streamlit_cfg = root / ".streamlit" / "config.toml"
        if os.path.lexists(str(streamlit_cfg)):
            if _is_reparse_or_link(streamlit_cfg) or _is_reparse_or_link(streamlit_cfg.parent):
                raise PermissionError(f"Refusing to bundle symlinked streamlit config: {streamlit_cfg}")
            if streamlit_cfg.is_file():
                force_include[str(streamlit_cfg)] = "second_brain/_streamlit/config.toml"
