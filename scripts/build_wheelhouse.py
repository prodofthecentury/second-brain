"""Build offline wheelhouse (`dist/wheelhouse/`) for Second Brain desktop bootstrap.

1. Builds `second_brain-<version>-py3-none-any.whl` via `uv build --wheel`.
2. Resolves pinned core runtime dependencies from `uv.lock` (`uv export --no-dev`)
   and materializes PEP 427 `.whl` archives directly from the local `uv` cache
   (`wheels-v*/pypi/...` -> `archive-v0/...`) with verified SHA-256 `RECORD` digests,
   falling back to `pip download` only if an uncached wheel is encountered.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "dist" / "wheelhouse"
_REQ_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)")


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _wheel_dist_prefix(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).strip().lower()


def _compatible_wheel_score(tag_suffix: str) -> int:
    """Return >0 preference score if `tag_suffix` (`<py>-<abi>-<plat>`) matches current Python/OS."""
    parts = tag_suffix.split("-")
    if len(parts) < 3:
        return -1
    py_tag, abi_tag, plat_tag = parts[0], parts[1], "-".join(parts[2:])
    py_major = sys.version_info.major
    py_minor = sys.version_info.minor
    cp_exact = f"cp{py_major}{py_minor}"

    py_tags = set(py_tag.split("."))
    py_ok = (
        cp_exact in py_tags
        or f"py{py_major}{py_minor}" in py_tags
        or f"py{py_major}" in py_tags
        or (abi_tag == "abi3" and any(t.startswith(f"cp{py_major}") and int(t[3:] or "0") <= py_minor for t in py_tags if t[2:].isdigit()))
    )
    if not py_ok:
        return -1

    abi_ok = abi_tag in {"none", "abi3", cp_exact, f"{cp_exact}d"}
    if not abi_ok:
        return -1

    machine = platform.machine().lower()
    is_x64 = machine in {"amd64", "x86_64"}
    is_arm64 = machine in {"arm64", "aarch64"}

    plat_parts = plat_tag.split(".")
    plat_ok = False
    for p in plat_parts:
        if p == "any":
            plat_ok = True
        elif sys.platform == "win32":
            if is_x64 and p == "win_amd64":
                plat_ok = True
            elif is_arm64 and p == "win_arm64":
                plat_ok = True
        elif sys.platform == "darwin" and p.startswith("macosx_"):
            if p.endswith("universal2") or (is_arm64 and p.endswith("arm64")) or (is_x64 and p.endswith("x86_64")):
                plat_ok = True
        elif sys.platform.startswith("linux") and (p.startswith("manylinux") or p.startswith("linux_")):
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
    return score


def _repack_uv_archive_to_wheel(archive_dir: Path, out_whl: Path) -> Path:
    """Pack an extracted `uv` `archive-v0/<id>` directory into a valid PEP 427 `.whl`."""
    dist_infos = [d for d in archive_dir.iterdir() if d.is_dir() and d.name.endswith(".dist-info")]
    if len(dist_infos) != 1:
        raise RuntimeError(f"Expected 1 .dist-info in {archive_dir}, found {len(dist_infos)}")
    dist_info_rel = dist_infos[0].name
    record_rel = f"{dist_info_rel}/RECORD"

    files_to_write: list[tuple[Path, str]] = []
    for file_path in sorted(archive_dir.rglob("*")):
        if not file_path.is_file():
            continue
        rel_posix = file_path.relative_to(archive_dir).as_posix()
        if rel_posix == record_rel or rel_posix.endswith(".pyc") or "/__pycache__/" in f"/{rel_posix}":
            continue
        files_to_write.append((file_path, rel_posix))

    tmp_whl = out_whl.with_suffix(".whl.tmp")
    record_rows: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(tmp_whl, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for disk_path, rel_posix in files_to_write:
            data = disk_path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            zf.writestr(rel_posix, data)
            record_rows.append((rel_posix, f"sha256={digest}", str(len(data))))
        record_rows.append((record_rel, "", ""))
        rec_buf = io.StringIO()
        writer = csv.writer(rec_buf, lineterminator="\n")
        writer.writerows(record_rows)
        zf.writestr(record_rel, rec_buf.getvalue().encode("utf-8"))

    tmp_whl.replace(out_whl)
    return out_whl


def _find_and_pack_cached_wheel(uv_cache: Path, pkg_name: str, version: str, out_dir: Path) -> Path | None:
    norm_name = _normalize_dist_name(pkg_name)
    dist_prefix = _wheel_dist_prefix(pkg_name)
    wheel_roots = sorted(uv_cache.glob("wheels-v*/pypi"), reverse=True)
    candidates: list[tuple[int, str, Path]] = []

    for wheel_root in wheel_roots:
        pkg_dir = wheel_root / norm_name
        if not pkg_dir.is_dir():
            continue
        prefix = f"{version}-"
        for entry in pkg_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix in {".http", ".msgpack", ".lock"}:
                continue
            if not entry.name.startswith(prefix):
                continue
            tag_suffix = entry.name[len(prefix):]
            score = _compatible_wheel_score(tag_suffix)
            if score < 0:
                continue
            try:
                pointer_rel = entry.read_bytes().decode("utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            archive_dir = (pkg_dir / pointer_rel).resolve() if not (uv_cache / pointer_rel).is_dir() else (uv_cache / pointer_rel)
            if archive_dir.is_dir():
                candidates.append((score, tag_suffix, archive_dir))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, best_tag, best_archive = candidates[0]
    dist_infos = [d for d in best_archive.iterdir() if d.is_dir() and d.name.endswith(".dist-info")]
    dist_stem = dist_infos[0].name[:-len(".dist-info")] if dist_infos else f"{dist_prefix}-{version}"
    out_whl = out_dir / f"{dist_stem}-{best_tag}.whl"
    return _repack_uv_archive_to_wheel(best_archive, out_whl)


def _export_pinned_requirements(repo_root: Path, extras: list[str]) -> list[tuple[str, str]]:
    uv_bin = shutil.which("uv") or "uv"
    cmd = [uv_bin, "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"]
    for extra in extras:
        cmd.extend(["--extra", extra])
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, encoding="utf-8", check=True)
    pins: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = _REQ_PIN_RE.match(line)
        if not match:
            continue
        name, ver = match.group(1), match.group(2)
        key = f"{_normalize_dist_name(name)}=={ver}"
        if key not in seen:
            seen.add(key)
            pins.append((name, ver))
    return pins


def build_wheelhouse(
    out_dir: Path = DEFAULT_OUT_DIR,
    *,
    repo_root: Path = REPO_ROOT,
    extras: list[str] | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    uv_bin = shutil.which("uv") or "uv"

    # Remove stale second_brain wheels so only the current version remains
    for old_sb in out_dir.glob("second_brain-*.whl"):
        old_sb.unlink(missing_ok=True)

    subprocess.run(
        [uv_bin, "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=str(repo_root),
        check=True,
    )
    built_wheels = list(out_dir.glob("second_brain-*.whl"))
    if not built_wheels:
        raise RuntimeError(f"uv build did not produce second_brain-*.whl in {out_dir}")

    cache_proc = subprocess.run(
        [uv_bin, "cache", "dir"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    uv_cache = Path(cache_proc.stdout.strip())

    pins = _export_pinned_requirements(repo_root, list(extras or []))
    missing: list[str] = []
    for pkg_name, ver in pins:
        packed = _find_and_pack_cached_wheel(uv_cache, pkg_name, ver, out_dir)
        if packed is not None:
            built_wheels.append(packed)
        else:
            missing.append(f"{pkg_name}=={ver}")

    if missing:
        # Populate uv cache for any missing platform wheels, then pack them
        tmp_venv = out_dir / ".tmp-cache-warm-venv"
        try:
            subprocess.run([uv_bin, "venv", str(tmp_venv), "--python", sys.executable], cwd=str(repo_root), check=True)
            py_in_tmp = tmp_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run([uv_bin, "pip", "install", "--python", str(py_in_tmp), *missing], cwd=str(repo_root), check=True)
        finally:
            shutil.rmtree(tmp_venv, ignore_errors=True)
        still_missing: list[str] = []
        for req in missing:
            pkg_name, ver = req.split("==", 1)
            packed = _find_and_pack_cached_wheel(uv_cache, pkg_name, ver, out_dir)
            if packed is not None:
                built_wheels.append(packed)
            else:
                still_missing.append(req)
        if still_missing:
            raise RuntimeError(f"Could not materialize wheel(s) into wheelhouse: {', '.join(still_missing)}")

    return sorted(set(built_wheels))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Target wheelhouse directory.")
    parser.add_argument("--extra", action="append", default=[], help="Optional extra(s) to include (repeatable).")
    args = parser.parse_args(argv)

    wheels = build_wheelhouse(out_dir=args.out_dir.resolve(), extras=args.extra)
    print(f"Populated wheelhouse at {args.out_dir.resolve()} with {len(wheels)} wheel(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
