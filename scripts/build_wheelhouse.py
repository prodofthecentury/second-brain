"""Build offline wheelhouse (`dist/wheelhouse/`) for Second Brain desktop bootstrap.

1. Builds `second_brain-<version>-py3-none-any.whl` via `uv build --wheel`.
2. Resolves pinned core runtime dependencies from `uv.lock` with full PEP 508
   environment marker evaluation (preventing multi-marker version duplicates such
   as `soupsieve` 2.10 + 3.0.4) across target OS/architecture slices — defaulting
   automatically on macOS (`darwin`) to universal (`arm64` + `x86_64`) so both
   Apple Silicon and Intel Macs install offline from universal `.dmg`/`.zip` builds.
3. Materializes PEP 427 `.whl` archives with fail-closed supply-chain checks:
   - Reuses existing regular `.whl` files in `out_dir` whose SHA-256 matches `uv.lock`
   - Supports `--strict-lock-downloads` (or `SECOND_BRAIN_STRICT_WHEELHOUSE=1`) to
     materialize exact `uv.lock`-hashed wheels directly from allowlisted PyPI hosts
   - When packing from the local `uv` cache (`wheels-v*/pypi/...` -> `archive-v0/...`),
     confines pointers strictly inside `uv_cache`, rejects symlinks/reparse points,
     and verifies every member against `.dist-info/RECORD` SHA-256 hashes before packing
   - Falls back to bounded, host-allowlisted, SHA-256-verified HTTPS downloads for
     uncached cross-arch wheels and prunes any stale `.whl` files from `out_dir`.
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
import ast
import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tomllib
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "dist" / "wheelhouse"
_VERSION_NUM_RE = re.compile(r"\d+")
_SAFE_WHEEL_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.whl$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO_SHA256 = "0" * 64
_ALLOWED_WHEEL_HOSTS: frozenset[str] = frozenset({"files.pythonhosted.org", "pypi.org"})
_MAX_WHEEL_BYTES = 256 * 1024 * 1024
_STREAM_CHUNK_BYTES = 65_536
_REQ_PY_CLAUSE_RE = re.compile(r"^\s*(==|!=|<=|>=|~=|<|>)\s*([0-9]+(?:\.[0-9]+)*(?:\.\*)?)\s*$")
_MANYLINUX_VER_RE = re.compile(r"^manylinux_(\d+)_(\d+)_")
_MACOSX_VER_RE = re.compile(r"^macosx_(\d+)_(\d+)_")


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


_DARWIN_SYSTEM_SYMLINKS: dict[str, str] = {
    "/var": "private/var",
    "/tmp": "private/tmp",
    "/etc": "private/etc",
}


def _is_immutable_darwin_root_symlink(path: Path) -> bool:
    if sys.platform != "darwin":
        return False
    expected = _DARWIN_SYSTEM_SYMLINKS.get(str(path))
    if not expected:
        return False
    try:
        return os.readlink(str(path)).lstrip("/") == expected
    except OSError:
        return False


def _assert_no_symlink_chain(path: Path, *, stop_at: Path | None = None) -> None:
    stop_norm = os.path.normcase(os.path.abspath(str(stop_at))) if stop_at is not None else None
    cur = Path(os.path.abspath(str(path)))
    while True:
        if os.path.lexists(str(cur)) and _is_reparse_or_link(cur):
            if not _is_immutable_darwin_root_symlink(cur):
                raise PermissionError(f"Refusing to traverse symlink or reparse point: {cur}")
        if stop_norm is not None and os.path.normcase(str(cur)) == stop_norm:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _wheel_dist_prefix(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).strip().lower()


def _parse_version_tuple(ver_str: str) -> tuple[int, ...]:
    nums = [int(m.group(0)) for m in _VERSION_NUM_RE.finditer(str(ver_str))]
    return tuple(nums) if nums else (0,)


def _validate_wheel_filename(fname: str) -> str:
    clean = str(fname or "")
    if (
        not clean
        or ".." in clean
        or "/" in clean
        or "\\" in clean
        or not _SAFE_WHEEL_FILENAME_RE.fullmatch(clean)
    ):
        raise PermissionError(f"Refusing unsafe wheel filename: {fname!r}")
    return clean


def _safe_dest_path(out_dir: Path, fname: str) -> Path:
    clean = _validate_wheel_filename(fname)
    out_resolved = out_dir.resolve()
    _assert_no_symlink_chain(out_dir)
    dest = out_resolved / clean
    if os.path.lexists(str(dest)) and _is_reparse_or_link(dest):
        raise PermissionError(f"Refusing to overwrite symlinked wheel path: {dest}")
    if dest.parent != out_resolved:
        raise PermissionError(f"Wheel destination escapes out_dir: {dest}")
    return dest


def _validate_wheel_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").strip().lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise PermissionError(f"Refusing wheel URL with invalid port: {url!r}") from exc
    if (
        (parsed.scheme or "").lower() != "https"
        or host not in _ALLOWED_WHEEL_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PermissionError(
            f"Refusing wheel URL outside allowlisted HTTPS PyPI hosts {sorted(_ALLOWED_WHEEL_HOSTS)}: {url!r}"
        )
    return parsed


def _validate_sha256_pin(expected_sha256: str, *, label: str = "wheel") -> str:
    clean = str(expected_sha256 or "").strip().lower()
    if not _SHA256_HEX_RE.fullmatch(clean) or clean == _ZERO_SHA256:
        raise ValueError(f"Missing or invalid SHA-256 pin for {label}: {expected_sha256!r}")
    return clean


def _requires_python_matches(spec: str | None, py_version: tuple[int, int, int]) -> bool:
    """Evaluate a PEP 440 `requires-python` specifier string against `(major, minor, micro)`."""
    if not spec or not str(spec).strip():
        return True
    for raw_clause in str(spec).split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        m = _REQ_PY_CLAUSE_RE.fullmatch(clause)
        if not m:
            continue
        op, raw_ver = m.group(1), m.group(2)
        wildcard = raw_ver.endswith(".*")
        target = _parse_version_tuple(raw_ver.rstrip(".*"))
        width = max(len(py_version), len(target))
        lhs = py_version + (0,) * (width - len(py_version))
        rhs = target + (0,) * (width - len(target))
        if op == "==":
            ok = (py_version[: len(target)] == target) if wildcard else (lhs == rhs)
        elif op == "!=":
            ok = (py_version[: len(target)] != target) if wildcard else (lhs != rhs)
        elif op == ">=":
            ok = lhs >= rhs
        elif op == "<=":
            ok = lhs <= rhs
        elif op == ">":
            ok = lhs > rhs
        elif op == "<":
            ok = lhs < rhs
        elif op == "~=":
            prefix_len = max(1, len(target) - 1)
            ok = lhs >= rhs and py_version[:prefix_len] == target[:prefix_len]
        else:
            ok = True
        if not ok:
            return False
    return True


def resolve_target_slices(target_platform: str = "auto") -> list[tuple[str, str]]:
    """Resolve `[(target_os, target_arch), ...]` slices for wheelhouse materialization."""
    clean = (target_platform or "auto").strip().lower()
    machine = platform.machine().lower()
    host_is_arm = machine in {"arm64", "aarch64"}

    if clean == "auto":
        if sys.platform == "darwin":
            # desktop/package.json builds universal macOS .dmg/.zip installers
            return [("darwin", "arm64"), ("darwin", "x86_64")]
        if sys.platform == "win32":
            # desktop/package.json packages Windows x64 (NSIS/MSI); Windows ARM64 runs x64 via Prism
            return [("win32", "x86_64")]
        return [("linux", "aarch64" if host_is_arm else "x86_64")]

    if clean == "host":
        if sys.platform == "darwin":
            return [("darwin", "arm64" if host_is_arm else "x86_64")]
        if sys.platform == "win32":
            return [("win32", "arm64" if host_is_arm else "x86_64")]
        return [("linux", "aarch64" if host_is_arm else "x86_64")]

    mapping: dict[str, list[tuple[str, str]]] = {
        "darwin-universal": [("darwin", "arm64"), ("darwin", "x86_64")],
        "darwin-arm64": [("darwin", "arm64")],
        "darwin-x86_64": [("darwin", "x86_64")],
        "win-amd64": [("win32", "x86_64")],
        "win-arm64": [("win32", "arm64")],
        "linux-x86_64": [("linux", "x86_64")],
        "linux-aarch64": [("linux", "aarch64")],
    }
    if clean not in mapping:
        raise ValueError(
            f"Unsupported --target-platform {target_platform!r}; choose from: auto, host, {', '.join(sorted(mapping))}"
        )
    return list(mapping[clean])


def _marker_env(
    *,
    target_os: str,
    target_arch: str,
    py_version: tuple[int, int, int],
    extra: str = "",
) -> dict[str, str]:
    major, minor, micro = py_version
    if target_os == "win32":
        os_name = "nt"
        platform_system = "Windows"
        platform_machine = "ARM64" if target_arch in {"arm64", "aarch64"} else "AMD64"
    elif target_os == "darwin":
        os_name = "posix"
        platform_system = "Darwin"
        platform_machine = "arm64" if target_arch in {"arm64", "aarch64"} else "x86_64"
    else:
        os_name = "posix"
        platform_system = "Linux"
        platform_machine = "aarch64" if target_arch in {"arm64", "aarch64"} else "x86_64"

    full_ver = f"{major}.{minor}.{micro}"
    return {
        "os_name": os_name,
        "sys_platform": target_os,
        "platform_system": platform_system,
        "platform_machine": platform_machine,
        "platform_release": "",
        "platform_version": "",
        "implementation_name": "cpython",
        "implementation_version": full_ver,
        "platform_python_implementation": "CPython",
        "python_version": f"{major}.{minor}",
        "python_full_version": full_ver,
        "extra": extra,
    }


def evaluate_pep508_marker(
    marker: str | None,
    *,
    target_os: str,
    target_arch: str,
    py_version: tuple[int, int, int],
    extra: str = "",
) -> bool:
    """Safely evaluate a PEP 508 environment marker via AST (zero `eval()`)."""
    if not marker or not marker.strip():
        return True
    env = _marker_env(target_os=target_os, target_arch=target_arch, py_version=py_version, extra=extra)
    version_keys = {"python_version", "python_full_version", "implementation_version"}
    tree = ast.parse(marker, mode="eval")

    def _eval_node(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(_eval_node(v)) for v in node.values)
            if isinstance(node.op, ast.Or):
                return any(bool(_eval_node(v)) for v in node.values)
            raise ValueError(f"Unsupported boolean op in marker: {marker!r}")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(_eval_node(node.operand))
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise ValueError(f"Unknown PEP 508 marker variable {node.id!r} in {marker!r}")
            return (node.id, env[node.id])
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return ("__const__", node.value)
        if isinstance(node, ast.Compare):
            left_raw = _eval_node(node.left)
            result = True
            cur_left = left_raw
            for op, comp in zip(node.ops, node.comparators):
                right_raw = _eval_node(comp)
                l_key, l_val = cur_left if isinstance(cur_left, tuple) else ("__const__", str(cur_left))
                r_key, r_val = right_raw if isinstance(right_raw, tuple) else ("__const__", str(right_raw))
                use_ver = (l_key in version_keys or r_key in version_keys) and not isinstance(op, (ast.In, ast.NotIn))
                if use_ver:
                    lhs: Any = _parse_version_tuple(l_val)
                    rhs: Any = _parse_version_tuple(r_val)
                    max_len = max(len(lhs), len(rhs))
                    lhs = lhs + (0,) * (max_len - len(lhs))
                    rhs = rhs + (0,) * (max_len - len(rhs))
                else:
                    lhs, rhs = l_val, r_val
                if isinstance(op, ast.Eq):
                    ok = lhs == rhs
                elif isinstance(op, ast.NotEq):
                    ok = lhs != rhs
                elif isinstance(op, ast.Lt):
                    ok = lhs < rhs
                elif isinstance(op, ast.LtE):
                    ok = lhs <= rhs
                elif isinstance(op, ast.Gt):
                    ok = lhs > rhs
                elif isinstance(op, ast.GtE):
                    ok = lhs >= rhs
                elif isinstance(op, ast.In):
                    ok = str(l_val) in str(r_val)
                elif isinstance(op, ast.NotIn):
                    ok = str(l_val) not in str(r_val)
                else:
                    raise ValueError(f"Unsupported comparison in marker: {marker!r}")
                if not ok:
                    result = False
                    break
                cur_left = right_raw
            return result
        raise ValueError(f"Unsupported AST construct in marker: {marker!r}")

    return bool(_eval_node(tree))


def _compatible_wheel_score(
    tag_suffix: str,
    *,
    target_os: str | None = None,
    target_arch: str | None = None,
    py_version: tuple[int, int, int] | None = None,
) -> int:
    """Return >0 preference score if `tag_suffix` (`<py>-<abi>-<plat>`) matches target Python/OS/arch."""
    parts = tag_suffix.split("-")
    if len(parts) < 3:
        return -1
    py_tag, abi_tag, plat_tag = parts[0], parts[1], "-".join(parts[2:])
    py_major, py_minor, _ = py_version or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    eff_os = target_os or ("win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux"))
    raw_arch = (target_arch or platform.machine()).lower()
    is_x64 = raw_arch in {"amd64", "x86_64"}
    is_arm64 = raw_arch in {"arm64", "aarch64"}

    cp_exact = f"cp{py_major}{py_minor}"
    py_tags = set(py_tag.split("."))
    py_ok = (
        cp_exact in py_tags
        or f"py{py_major}{py_minor}" in py_tags
        or f"py{py_major}" in py_tags
        or (
            abi_tag == "abi3"
            and any(
                t.startswith(f"cp{py_major}") and int(t[3:] or "0") <= py_minor
                for t in py_tags
                if t[2:].isdigit()
            )
        )
    )
    if not py_ok:
        return -1

    abi_ok = abi_tag in {"none", "abi3", cp_exact, f"{cp_exact}d"}
    if not abi_ok:
        return -1

    plat_parts = plat_tag.split(".")
    plat_ok = False
    is_universal2 = False
    for p in plat_parts:
        if p == "any":
            plat_ok = True
        elif eff_os == "win32":
            if is_x64 and p == "win_amd64":
                plat_ok = True
            elif is_arm64 and p == "win_arm64":
                plat_ok = True
        elif eff_os == "darwin" and p.startswith("macosx_"):
            if p.endswith("universal2"):
                plat_ok = True
                is_universal2 = True
            elif (is_arm64 and p.endswith("arm64")) or (is_x64 and p.endswith("x86_64")):
                plat_ok = True
        elif eff_os.startswith("linux") and (p.startswith("manylinux") or p.startswith("linux_")):
            if (is_x64 and p.endswith("x86_64")) or (is_arm64 and p.endswith("aarch64")):
                plat_ok = True
    if not plat_ok:
        return -1

    score = 10
    if cp_exact in py_tags:
        score += 20
    if abi_tag == cp_exact:
        score += 10
    elif abi_tag == "abi3":
        score += 5
    if "any" not in plat_parts:
        score += 15
    if is_universal2:
        score += 3
    return score


def _wheel_candidate_sort_key(score: int, tag_suffix: str) -> tuple[int, int, tuple[int, int], str]:
    """Deterministic compatibility sort key (higher is better):

    1. Primary compatibility `score` (exact cpXY / universal2 / native wheel).
    2. Highest numeric `cp3XX` minor version (so `cp311-abi3` beats `cp39-abi3` on Python 3.12+).
    3. Widest OS/glibc compatibility = **lowest** minimum `manylinux` / `macosx` version
       (so `manylinux2014` `(2, 17)` beats `manylinux_2_28` beats `manylinux_2_34`).
    """
    parts = tag_suffix.split("-")
    py_tag = parts[0] if parts else ""
    plat_tag = "-".join(parts[2:]) if len(parts) >= 3 else ""

    max_cp_minor = -1
    for t in py_tag.split("."):
        if t.startswith("cp3") and t[3:].isdigit():
            max_cp_minor = max(max_cp_minor, int(t[3:]))

    plat_versions: list[tuple[int, int]] = []
    for p in plat_tag.split("."):
        if p.startswith("manylinux2014_"):
            plat_versions.append((2, 17))
        elif p.startswith("manylinux2010_"):
            plat_versions.append((2, 12))
        elif p.startswith("manylinux1_"):
            plat_versions.append((2, 5))
        else:
            m_ml = _MANYLINUX_VER_RE.match(p)
            if m_ml:
                plat_versions.append((int(m_ml.group(1)), int(m_ml.group(2))))
                continue
            m_mac = _MACOSX_VER_RE.match(p)
            if m_mac:
                plat_versions.append((int(m_mac.group(1)), int(m_mac.group(2))))

    if plat_versions:
        min_os_ver = min(plat_versions)
        os_compat_rank = (-min_os_ver[0], -min_os_ver[1])
    else:
        os_compat_rank = (0, 0)

    return (score, max_cp_minor, os_compat_rank, tag_suffix)


def resolve_wheelhouse_plan(
    repo_root: Path = REPO_ROOT,
    *,
    extras: list[str] | None = None,
    target_platform: str = "auto",
    py_version: tuple[int, int, int] | None = None,
) -> list[dict[str, str]]:
    """Resolve exact deduplicated wheel plan from `uv.lock` across target `(os, arch)` slices."""
    eff_py = py_version or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    slices = resolve_target_slices(target_platform)
    lock_path = repo_root / "uv.lock"
    if not lock_path.is_file():
        raise FileNotFoundError(f"Missing uv.lock at {lock_path}")

    lock_data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = lock_data.get("package")
    if not isinstance(packages, list):
        raise RuntimeError(f"Malformed uv.lock at {lock_path}: missing [[package]] list")

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    root_pkg: dict[str, Any] | None = None
    for pkg in packages:
        if not isinstance(pkg, dict) or not pkg.get("name"):
            continue
        norm = _normalize_dist_name(str(pkg["name"]))
        ver = str(pkg.get("version") or "")
        by_key[(norm, ver)] = pkg
        by_name.setdefault(norm, []).append(pkg)
        if norm == "second-brain":
            root_pkg = pkg

    if root_pkg is None:
        raise RuntimeError("Could not find 'second-brain' root package in uv.lock")

    wanted_extras = [e.strip() for e in (extras or []) if e and e.strip()]
    plan_by_filename: dict[str, dict[str, str]] = {}

    for target_os, target_arch in slices:
        queue: list[tuple[str, str | None]] = []

        def _enqueue_deps(dep_list: list[Any], extra_ctx: str = "") -> None:
            for dep in dep_list:
                if not isinstance(dep, dict) or not dep.get("name"):
                    continue
                dep_norm = _normalize_dist_name(str(dep["name"]))
                if dep_norm == "second-brain":
                    continue
                marker = dep.get("marker")
                if not evaluate_pep508_marker(
                    str(marker) if marker else None,
                    target_os=target_os,
                    target_arch=target_arch,
                    py_version=eff_py,
                    extra=extra_ctx,
                ):
                    continue
                dep_ver = str(dep["version"]) if dep.get("version") else None
                queue.append((dep_norm, dep_ver))

        _enqueue_deps(list(root_pkg.get("dependencies") or []))
        opt_map = root_pkg.get("optional-dependencies") or {}
        if isinstance(opt_map, dict):
            for ext in wanted_extras:
                _enqueue_deps(list(opt_map.get(ext) or []), extra_ctx=ext)

        visited: set[tuple[str, str]] = set()
        while queue:
            pkg_norm, explicit_ver = queue.pop(0)
            cand_pkg: dict[str, Any] | None = None
            if explicit_ver and (pkg_norm, explicit_ver) in by_key:
                cand_pkg = by_key[(pkg_norm, explicit_ver)]
            else:
                matches = by_name.get(pkg_norm, [])
                if len(matches) == 1:
                    cand_pkg = matches[0]
                elif matches:
                    compat = [
                        item
                        for item in matches
                        if _requires_python_matches(
                            str(item.get("requires-python") or ""),
                            eff_py,
                        )
                    ]
                    pool = compat if compat else matches
                    matches_sorted = sorted(
                        pool,
                        key=lambda item: _parse_version_tuple(str(item.get("version") or "0")),
                        reverse=True,
                    )
                    cand_pkg = matches_sorted[0]
            if cand_pkg is None:
                raise RuntimeError(f"Dependency {pkg_norm!r} not found in uv.lock")

            resolved_ver = str(cand_pkg.get("version") or "")
            visit_key = (pkg_norm, resolved_ver)
            if visit_key in visited:
                continue
            visited.add(visit_key)

            _enqueue_deps(list(cand_pkg.get("dependencies") or []))

            wheel_candidates: list[tuple[tuple[int, int, tuple[int, int], str], str, str, str, str, int]] = []
            for whl_entry in cand_pkg.get("wheels") or []:
                if not isinstance(whl_entry, dict):
                    continue
                url = str(whl_entry.get("url") or "").strip()
                if not url.endswith(".whl"):
                    continue
                _validate_wheel_url(url)
                fname = _validate_wheel_filename(urllib.parse.unquote(url.rsplit("/", 1)[-1]))
                stem = fname[:-4]
                ver_marker = f"-{resolved_ver}-"
                idx = stem.find(ver_marker)
                if idx < 0:
                    parts = stem.split("-")
                    if len(parts) < 5:
                        continue
                    tag_suffix = "-".join(parts[-3:])
                else:
                    tag_suffix = stem[idx + len(ver_marker) :]
                score = _compatible_wheel_score(
                    tag_suffix,
                    target_os=target_os,
                    target_arch=target_arch,
                    py_version=eff_py,
                )
                if score < 0:
                    continue
                raw_hash = str(whl_entry.get("hash") or "").strip()
                sha256_hex = raw_hash[len("sha256:") :] if raw_hash.startswith("sha256:") else ""
                sha256_clean = _validate_sha256_pin(sha256_hex, label=fname)
                raw_size = whl_entry.get("size")
                size_int = int(raw_size) if isinstance(raw_size, int) and raw_size > 0 else 0
                sort_key = _wheel_candidate_sort_key(score, tag_suffix)
                wheel_candidates.append((sort_key, tag_suffix, fname, url, sha256_clean, size_int))

            if not wheel_candidates:
                raise RuntimeError(
                    f"No compatible prebuilt wheel in uv.lock for {pkg_norm}=={resolved_ver} "
                    f"on {target_os}/{target_arch} (Python {eff_py[0]}.{eff_py[1]})"
                )

            wheel_candidates.sort(key=lambda item: item[0], reverse=True)
            _, best_tag, best_fname, best_url, best_sha, best_size = wheel_candidates[0]
            plan_by_filename[best_fname] = {
                "package": pkg_norm,
                "version": resolved_ver,
                "tag_suffix": best_tag,
                "filename": best_fname,
                "url": best_url,
                "sha256": best_sha,
                "size": str(best_size),
                "target_os": target_os,
                "target_arch": target_arch,
            }

    return [plan_by_filename[k] for k in sorted(plan_by_filename)]


def _write_bytes_atomic_exclusive(dest_path: Path, data: bytes) -> Path:
    """Write bytes atomically using a randomized exclusive temp file (rejecting symlinks)."""
    _assert_no_symlink_chain(dest_path.parent)
    if os.path.lexists(str(dest_path)) and _is_reparse_or_link(dest_path):
        raise PermissionError(f"Refusing to overwrite symlinked file: {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(f".{dest_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(str(tmp_path), flags, 0o600)
    try:
        view = memoryview(data)
        total = 0
        while total < len(view):
            written = os.write(fd, view[total:])
            if written <= 0:
                raise OSError(f"Short write to {tmp_path}")
            total += written
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, dest_path)
    return dest_path


def _repack_uv_archive_to_wheel(
    archive_dir: Path,
    out_whl: Path,
    *,
    uv_cache_root: Path | None = None,
) -> Path:
    """Pack an extracted `uv` `archive-v0/<id>` directory into a valid PEP 427 `.whl`.

    Fail-closed supply-chain checks:
    - Rejects symlinks/reparse points on `archive_dir`, every descendant, and `out_whl`.
    - Confines `archive_dir` strictly inside `uv_cache_root` when supplied.
    - Verifies EVERY member against the archive's existing `.dist-info/RECORD` SHA-256
      hashes and byte lengths (rejecting modified, missing, or extra untracked files).
    - Preserves Unix file mode bits in `ZipInfo.external_attr`.
    """
    _assert_no_symlink_chain(archive_dir)
    archive_resolved = archive_dir.resolve()
    if uv_cache_root is not None:
        cache_resolved = uv_cache_root.resolve()
        if not archive_resolved.is_relative_to(cache_resolved):
            raise PermissionError(f"Cached wheel archive {archive_resolved} escapes uv cache {cache_resolved}")

    _validate_wheel_filename(out_whl.name)
    _assert_no_symlink_chain(out_whl.parent)
    if os.path.lexists(str(out_whl)) and _is_reparse_or_link(out_whl):
        raise PermissionError(f"Refusing to overwrite symlinked wheel: {out_whl}")

    dist_infos = [
        d
        for d in archive_resolved.iterdir()
        if not _is_reparse_or_link(d) and d.is_dir() and d.name.endswith(".dist-info")
    ]
    if len(dist_infos) != 1:
        raise RuntimeError(f"Expected 1 .dist-info in {archive_resolved}, found {len(dist_infos)}")
    dist_info_rel = dist_infos[0].name
    record_rel = f"{dist_info_rel}/RECORD"
    record_disk = archive_resolved / dist_info_rel / "RECORD"
    if not record_disk.is_file() or _is_reparse_or_link(record_disk):
        raise RuntimeError(f"Missing or symlinked RECORD in cached archive: {record_disk}")

    expected_records: dict[str, tuple[str, int | None]] = {}
    raw_record_text = record_disk.read_text(encoding="utf-8")
    for row in csv.reader(io.StringIO(raw_record_text)):
        if not row:
            continue
        rel_entry = row[0].replace("\\", "/")
        if rel_entry == record_rel or rel_entry.endswith((".pyc", ".pyo")):
            continue
        digest_field = row[1].strip() if len(row) > 1 else ""
        size_field = row[2].strip() if len(row) > 2 else ""
        if not digest_field.startswith("sha256=") or len(digest_field) <= len("sha256="):
            raise RuntimeError(f"Cached archive RECORD entry {rel_entry!r} lacks sha256 digest")
        exp_size = int(size_field) if size_field.isdigit() else None
        expected_records[rel_entry] = (digest_field[len("sha256=") :], exp_size)

    if not expected_records:
        raise RuntimeError(f"Cached archive RECORD in {archive_resolved} contains zero verifiable entries")

    files_to_write: list[tuple[Path, str, bytes, str, int]] = []
    seen_rels: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(str(archive_resolved), followlinks=False):
        cur_dir = Path(dirpath)
        if _is_reparse_or_link(cur_dir):
            raise PermissionError(f"Refusing symlinked directory in uv cache archive: {cur_dir}")
        kept_dirs: list[str] = []
        for dname in sorted(dirnames):
            d_full = cur_dir / dname
            if _is_reparse_or_link(d_full):
                raise PermissionError(f"Refusing symlinked subdirectory in uv cache archive: {d_full}")
            if dname == "__pycache__":
                continue
            kept_dirs.append(dname)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            file_path = cur_dir / fname
            if _is_reparse_or_link(file_path):
                raise PermissionError(f"Refusing symlinked file in uv cache archive: {file_path}")
            rel_posix = file_path.relative_to(archive_resolved).as_posix()
            if rel_posix == record_rel or rel_posix.endswith((".pyc", ".pyo")):
                continue
            if rel_posix not in expected_records:
                raise RuntimeError(f"Untracked file {rel_posix!r} in cached archive {archive_resolved} not in RECORD")
            st = os.lstat(str(file_path))
            if not stat.S_ISREG(st.st_mode):
                raise PermissionError(f"Refusing non-regular file in uv cache archive: {file_path}")
            data = file_path.read_bytes()
            actual_b64 = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            exp_b64, exp_size = expected_records[rel_posix]
            if actual_b64 != exp_b64 or (exp_size is not None and len(data) != exp_size):
                raise RuntimeError(
                    f"RECORD integrity mismatch for {rel_posix!r} in cached archive {archive_resolved}"
                )
            perm_mode = stat.S_IMODE(st.st_mode) or 0o644
            if os.name == "nt":
                perm_mode = 0o755 if rel_posix.endswith(".sh") else 0o644
            files_to_write.append((file_path, rel_posix, data, actual_b64, perm_mode))
            seen_rels.add(rel_posix)

    missing_members = sorted(set(expected_records) - seen_rels)
    if missing_members:
        raise RuntimeError(
            f"Cached archive {archive_resolved} missing RECORD member(s): {missing_members[:5]}"
        )

    files_to_write.sort(key=lambda item: item[1])
    zip_buf = io.BytesIO()
    record_rows: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for _disk_path, rel_posix, data, digest, perm_mode in files_to_write:
            zinfo = zipfile.ZipInfo(filename=rel_posix, date_time=(2026, 1, 1, 0, 0, 0))
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = (stat.S_IFREG | perm_mode) << 16
            zf.writestr(zinfo, data)
            record_rows.append((rel_posix, f"sha256={digest}", str(len(data))))
        record_rows.append((record_rel, "", ""))
        rec_buf = io.StringIO()
        writer = csv.writer(rec_buf, lineterminator="\n")
        writer.writerows(record_rows)
        rec_info = zipfile.ZipInfo(filename=record_rel, date_time=(2026, 1, 1, 0, 0, 0))
        rec_info.compress_type = zipfile.ZIP_DEFLATED
        rec_info.external_attr = (stat.S_IFREG | 0o644) << 16
        zf.writestr(rec_info, rec_buf.getvalue().encode("utf-8"))

    return _write_bytes_atomic_exclusive(out_whl, zip_buf.getvalue())


def _find_and_pack_cached_wheel(
    uv_cache: Path,
    pkg_name: str,
    version: str,
    out_dir: Path,
    *,
    expected_tag_suffix: str | None = None,
    expected_filename: str | None = None,
    target_os: str | None = None,
    target_arch: str | None = None,
    py_version: tuple[int, int, int] | None = None,
) -> Path | None:
    if not uv_cache.is_dir() or _is_reparse_or_link(uv_cache):
        return None
    cache_resolved = uv_cache.resolve()
    norm_name = _normalize_dist_name(pkg_name)
    dist_prefix = _wheel_dist_prefix(pkg_name)
    wheel_roots = sorted(cache_resolved.glob("wheels-v*/pypi"), reverse=True)
    candidates: list[tuple[tuple[int, int, tuple[int, int], str], str, Path]] = []

    for wheel_root in wheel_roots:
        if _is_reparse_or_link(wheel_root):
            continue
        pkg_dir = wheel_root / norm_name
        if not pkg_dir.is_dir() or _is_reparse_or_link(pkg_dir):
            continue
        prefix = f"{version}-"
        for entry in pkg_dir.iterdir():
            if _is_reparse_or_link(entry) or not entry.is_file():
                continue
            if entry.suffix in {".http", ".msgpack", ".lock"}:
                continue
            if not entry.name.startswith(prefix):
                continue
            tag_suffix = entry.name[len(prefix) :]
            if expected_tag_suffix is not None and tag_suffix != expected_tag_suffix:
                continue
            score = _compatible_wheel_score(
                tag_suffix,
                target_os=target_os,
                target_arch=target_arch,
                py_version=py_version,
            )
            if score < 0:
                continue
            try:
                pointer_rel = entry.read_bytes().decode("utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if not pointer_rel or "\x00" in pointer_rel:
                continue
            cand_archive = (
                (cache_resolved / pointer_rel)
                if (cache_resolved / pointer_rel).is_dir()
                else (pkg_dir / pointer_rel)
            )
            try:
                resolved_archive = cand_archive.resolve(strict=True)
            except OSError:
                continue
            if (
                not resolved_archive.is_relative_to(cache_resolved)
                or _is_reparse_or_link(cand_archive)
                or _is_reparse_or_link(resolved_archive)
                or not resolved_archive.is_dir()
            ):
                continue
            candidates.append((_wheel_candidate_sort_key(score, tag_suffix), tag_suffix, resolved_archive))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, best_tag, best_archive = candidates[0]
    if expected_filename:
        out_whl = _safe_dest_path(out_dir, expected_filename)
    else:
        dist_infos = [
            d for d in best_archive.iterdir() if not _is_reparse_or_link(d) and d.is_dir() and d.name.endswith(".dist-info")
        ]
        dist_stem = dist_infos[0].name[: -len(".dist-info")] if dist_infos else f"{dist_prefix}-{version}"
        out_whl = _safe_dest_path(out_dir, f"{dist_stem}-{best_tag}.whl")
    return _repack_uv_archive_to_wheel(best_archive, out_whl, uv_cache_root=cache_resolved)


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        try:
            _validate_wheel_url(newurl)
        except PermissionError as exc:
            raise urllib.error.URLError(str(exc)) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_verified_wheel(
    url: str,
    expected_sha256: str,
    dest_path: Path,
    *,
    expected_size: int | None = None,
) -> Path:
    """Download a pinned PyPI wheel over allowlisted HTTPS and verify exact `uv.lock` SHA-256 fail-closed."""
    _validate_wheel_url(url)
    expected_clean = _validate_sha256_pin(expected_sha256, label=dest_path.name)
    _validate_wheel_filename(dest_path.name)
    _assert_no_symlink_chain(dest_path.parent)
    if os.path.lexists(str(dest_path)) and _is_reparse_or_link(dest_path):
        raise PermissionError(f"Refusing to access symlinked wheel destination: {dest_path}")

    # Reuse existing regular file on disk if it already matches exact PyPI SHA-256
    if dest_path.is_file():
        existing_sha = hashlib.sha256(dest_path.read_bytes()).hexdigest().lower()
        if existing_sha == expected_clean:
            return dest_path

    max_bytes = (
        min(int(expected_size), _MAX_WHEEL_BYTES)
        if (expected_size is not None and int(expected_size) > 0)
        else _MAX_WHEEL_BYTES
    )

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HttpsOnlyRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": "SecondBrain-Wheelhouse/1.0"}, method="GET")
    buf = bytearray()
    hasher = hashlib.sha256()
    with opener.open(req, timeout=60.0) as resp:
        while True:
            chunk = resp.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            if len(buf) + len(chunk) > max_bytes:
                raise RuntimeError(
                    f"Wheel download for {dest_path.name} exceeded maximum allowed size ({max_bytes} bytes)"
                )
            buf.extend(chunk)
            hasher.update(chunk)

    if expected_size is not None and int(expected_size) > 0 and len(buf) != int(expected_size):
        raise RuntimeError(
            f"Size mismatch downloading {dest_path.name}: expected {expected_size} bytes, got {len(buf)}"
        )

    actual_sha = hasher.hexdigest().lower()
    if actual_sha != expected_clean:
        raise RuntimeError(
            f"SHA-256 mismatch downloading {dest_path.name}: expected {expected_clean}, got {actual_sha}"
        )

    return _write_bytes_atomic_exclusive(dest_path, bytes(buf))


def build_wheelhouse(
    out_dir: Path = DEFAULT_OUT_DIR,
    *,
    repo_root: Path = REPO_ROOT,
    extras: list[str] | None = None,
    target_platform: str = "auto",
    strict_lock_downloads: bool = False,
    wheel_fetcher: Callable[[str, str, Path], Path] | None = None,
) -> list[Path]:
    _assert_no_symlink_chain(out_dir)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_chain(out_dir)
    uv_bin = shutil.which("uv") or "uv"

    for old_sb in out_dir.glob("second_brain-*.whl"):
        if not _is_reparse_or_link(old_sb):
            old_sb.unlink(missing_ok=True)

    subprocess.run(
        [uv_bin, "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=str(repo_root),
        check=True,
    )
    built_sb = list(out_dir.glob("second_brain-*.whl"))
    if len(built_sb) != 1:
        raise RuntimeError(f"uv build did not produce single second_brain-*.whl in {out_dir}: {built_sb}")
    built_wheels: list[Path] = [built_sb[0].resolve()]

    cache_proc = subprocess.run(
        [uv_bin, "cache", "dir"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    uv_cache = Path(cache_proc.stdout.strip())
    py_ver = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)

    plan = resolve_wheelhouse_plan(
        repo_root=repo_root,
        extras=extras,
        target_platform=target_platform,
        py_version=py_ver,
    )
    env_strict = os.environ.get("SECOND_BRAIN_STRICT_WHEELHOUSE", "").strip().lower() in {"1", "true", "yes"}
    effective_strict = bool(strict_lock_downloads or env_strict)

    for item in plan:
        dest_whl = _safe_dest_path(out_dir, item["filename"])
        expected_sha = _validate_sha256_pin(item["sha256"], label=item["filename"])
        expected_size = int(item.get("size") or 0) or None

        # 1. Reuse already SHA-256-verified exact wheel on disk (never downgrade it to a repacked zip)
        if dest_whl.is_file() and not _is_reparse_or_link(dest_whl):
            if hashlib.sha256(dest_whl.read_bytes()).hexdigest().lower() == expected_sha:
                built_wheels.append(dest_whl.resolve())
                continue

        # 2. Local uv cache repack (RECORD-verified + cache-confined) unless strict exact-wheel mode is enabled
        if not effective_strict:
            try:
                packed = _find_and_pack_cached_wheel(
                    uv_cache,
                    item["package"],
                    item["version"],
                    out_dir,
                    expected_tag_suffix=item["tag_suffix"],
                    expected_filename=item["filename"],
                    target_os=item["target_os"],
                    target_arch=item["target_arch"],
                    py_version=py_ver,
                )
            except (RuntimeError, PermissionError, OSError):
                packed = None
            if packed is not None:
                built_wheels.append(packed.resolve())
                continue

        # 3. SHA-256-verified HTTPS download from allowlisted PyPI hosts
        if item["url"] and expected_sha:
            if wheel_fetcher is not None:
                fetched = wheel_fetcher(item["url"], expected_sha, dest_whl)
            else:
                fetched = _download_verified_wheel(
                    item["url"],
                    expected_sha,
                    dest_whl,
                    expected_size=expected_size,
                )
            built_wheels.append(fetched.resolve())
            continue

        raise RuntimeError(
            f"Could not materialize wheel {item['filename']} ({item['package']}=={item['version']}) into wheelhouse"
        )

    keep_resolved = {p.resolve() for p in built_wheels}
    for existing_whl in out_dir.glob("*.whl"):
        if _is_reparse_or_link(existing_whl):
            continue
        if existing_whl.resolve() not in keep_resolved:
            existing_whl.unlink(missing_ok=True)

    return sorted(keep_resolved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Target wheelhouse directory.")
    parser.add_argument("--extra", action="append", default=[], help="Optional extra(s) to include (repeatable).")
    parser.add_argument(
        "--target-platform",
        default="auto",
        help="Target OS/arch slice(s): auto (universal on macOS), host, darwin-universal, darwin-arm64, darwin-x86_64, win-amd64, win-arm64, linux-x86_64, linux-aarch64.",
    )
    parser.add_argument(
        "--strict-lock-downloads",
        action="store_true",
        help="Materialize exact uv.lock SHA-256-verified wheels from allowlisted PyPI hosts instead of repacking uv cache.",
    )
    args = parser.parse_args(argv)

    wheels = build_wheelhouse(
        out_dir=args.out_dir.resolve(),
        extras=args.extra,
        target_platform=args.target_platform,
        strict_lock_downloads=bool(args.strict_lock_downloads),
    )
    print(f"Populated wheelhouse at {args.out_dir.resolve()} with {len(wheels)} wheel(s) (target={args.target_platform}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
