# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["PyYAML"]
# ///
"""Build Mobile Inbox Drop PWA release zip & WinGet / Homebrew Cask distribution manifests.

Outputs under `--out-dir` (default `dist/`):
  * `second-brain-inbox-drop-pwa.zip` — deterministic offline-capable static WebCrypto PWA bundle
  * `manifests/winget/SecondBrain.SecondBrain.yaml` — WinGet singleton manifest (schema 1.6.0)
  * `manifests/homebrew/second-brain.rb` — Homebrew Cask formula
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from atom_mutator import atomic_write_bytes  # noqa: E402
from config import _is_reparse_or_link  # noqa: E402
from inbox_drop import export_static_pwa_bundle  # noqa: E402


_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PLACEHOLDER_SHA256 = "0" * 64


def _resolve_project_version(explicit_version: str | None = None) -> str:
    if explicit_version and explicit_version.strip():
        ver = explicit_version.strip().lstrip("v")
    else:
        import second_brain

        ver = str(second_brain.__version__).strip()
    if not _SEMVER_RE.fullmatch(ver):
        raise ValueError(f"Invalid semantic version: {ver!r}")
    return ver


def _assert_no_symlink_ancestors(path: Path) -> None:
    for cur in (path, *path.parents):
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
            raise PermissionError(f"Refusing to write distribution artifact under symlink/reparse path: {cur}")


def build_inbox_drop_pwa_zip(out_dir: str | Path = ROOT / "dist") -> dict[str, Any]:
    """Export and deterministically zip the static Zero-Knowledge Mobile Inbox Drop PWA."""
    dest_root = Path(os.path.abspath(str(out_dir)))
    _assert_no_symlink_ancestors(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    zip_path = dest_root / "second-brain-inbox-drop-pwa.zip"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        stage_dir = Path(tmp) / "inbox-drop"
        exported = export_static_pwa_bundle(stage_dir)
        files = sorted(
            p for p in stage_dir.rglob("*") if p.is_file() and not _is_reparse_or_link(str(p))
        )
        if not files:
            raise RuntimeError("No Mobile Inbox Drop PWA files exported.")

        buf_path = dest_root / f".tmp-pwa-{os.getpid()}.zip"
        try:
            with zipfile.ZipFile(buf_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fpath in files:
                    rel = fpath.relative_to(stage_dir).as_posix()
                    zinfo = zipfile.ZipInfo(filename=f"inbox-drop/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
                    zinfo.compress_type = zipfile.ZIP_DEFLATED
                    zinfo.external_attr = 0o644 << 16
                    zf.writestr(zinfo, fpath.read_bytes())
            raw_zip = buf_path.read_bytes()
            atomic_write_bytes(str(zip_path), raw_zip)
        finally:
            if buf_path.exists():
                try:
                    buf_path.unlink()
                except OSError:
                    pass

    return {
        "zip_path": str(zip_path),
        "size_bytes": len(raw_zip),
        "sha256": hashlib.sha256(raw_zip).hexdigest(),
        "files": list(exported.get("files") or []),
    }


def _load_checksums(dist_dir: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    sums_file = dist_dir / "SHA256SUMS.txt"
    if sums_file.is_file() and not _is_reparse_or_link(str(sums_file)):
        for line in sums_file.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and _SHA256_HEX_RE.fullmatch(parts[0]):
                fname = parts[-1].lstrip("*./")
                if not _SAFE_ARTIFACT_NAME_RE.fullmatch(fname) or ".." in fname:
                    raise ValueError(f"Unsafe artifact filename in SHA256SUMS.txt: {fname!r}")
                checksums[fname] = parts[0].lower()

    if dist_dir.is_dir() and not _is_reparse_or_link(str(dist_dir)):
        for entry in sorted(dist_dir.iterdir()):
            if (
                entry.is_file()
                and not _is_reparse_or_link(str(entry))
                and entry.name != "SHA256SUMS.txt"
                and _SAFE_ARTIFACT_NAME_RE.fullmatch(entry.name)
            ):
                checksums.setdefault(entry.name, hashlib.sha256(entry.read_bytes()).hexdigest())
    return checksums


def _match_artifact(checksums: dict[str, str], suffix: str, *, fallback_name: str, require_real: bool) -> tuple[str, str]:
    matches = sorted(name for name in checksums if name.lower().endswith(suffix.lower()))
    if matches:
        chosen = matches[0]
        return chosen, checksums[chosen]
    if require_real:
        raise FileNotFoundError(f"Required release artifact matching *{suffix} not found in SHA256SUMS.txt or dist/.")
    return fallback_name, _PLACEHOLDER_SHA256


def build_package_manager_manifests(
    *,
    out_dir: str | Path = ROOT / "dist",
    dist_dir: str | Path | None = None,
    version: str | None = None,
    repo_slug: str | None = None,
    require_checksums: bool = False,
) -> dict[str, Any]:
    """Generate validated WinGet YAML and Homebrew Cask `.rb` manifests."""
    ver = _resolve_project_version(version)
    slug = (repo_slug or os.environ.get("GITHUB_REPOSITORY") or "second-brain/second-brain").strip()
    if not _REPO_SLUG_RE.fullmatch(slug):
        raise ValueError(f"Invalid GitHub repository slug: {slug!r}")

    dest_root = Path(os.path.abspath(str(out_dir)))
    artifact_dir = Path(os.path.abspath(str(dist_dir))) if dist_dir is not None else dest_root
    _assert_no_symlink_ancestors(dest_root)
    _assert_no_symlink_ancestors(artifact_dir)

    checksums = _load_checksums(artifact_dir)
    exe_name, exe_sha = _match_artifact(
        checksums,
        ".exe",
        fallback_name=f"Second-Brain-Setup-{ver}.exe",
        require_real=require_checksums,
    )
    msi_name, msi_sha = _match_artifact(
        checksums,
        ".msi",
        fallback_name=f"Second-Brain-{ver}-x64.msi",
        require_real=False,
    )
    dmg_name, dmg_sha = _match_artifact(
        checksums,
        ".dmg",
        fallback_name=f"Second-Brain-{ver}-universal.dmg",
        require_real=require_checksums,
    )

    base_download = f"https://github.com/{slug}/releases/download/v{ver}"
    # Emit only the NSIS (`nullsoft`) installer in the WinGet singleton manifest so `winget validate`
    # passes strictly without requiring an extracted MSI ProductCode GUID.
    installers: list[dict[str, Any]] = [
        {
            "Architecture": "x64",
            "InstallerType": "nullsoft",
            "InstallerUrl": f"{base_download}/{exe_name}",
            "InstallerSha256": exe_sha.upper(),
        }
    ]

    winget_doc: dict[str, Any] = {
        "PackageIdentifier": "SecondBrain.SecondBrain",
        "PackageVersion": ver,
        "PackageLocale": "en-US",
        "Publisher": "Second Brain",
        "PublisherUrl": f"https://github.com/{slug}",
        "PackageName": "Second Brain",
        "PackageUrl": f"https://github.com/{slug}",
        "License": "MIT",
        "ShortDescription": "Local-first verifiable AI memory platform with cryptographic byte-offset provenance.",
        "Commands": ["second-brain"],
        "Installers": installers,
        "ManifestType": "singleton",
        "ManifestVersion": "1.6.0",
    }
    winget_header = (
        "# yaml-language-server: $schema=https://aka.ms/winget-manifest.singleton.1.6.0.schema.json\n"
    )
    winget_yaml = winget_header + yaml.safe_dump(winget_doc, sort_keys=False, allow_unicode=True)
    # Validate round-trip YAML parse
    parsed_yaml = yaml.safe_load(winget_yaml)
    if not isinstance(parsed_yaml, dict) or parsed_yaml.get("PackageVersion") != ver:
        raise RuntimeError("Generated WinGet YAML failed validation.")

    dmg_url_expr = dmg_name.replace(ver, "#{version}")
    sha_expr = f':no_check' if dmg_sha == _PLACEHOLDER_SHA256 else f'"{dmg_sha.lower()}"'
    homebrew_rb = (
        f'cask "second-brain" do\n'
        f'  version "{ver}"\n'
        f"  sha256 {sha_expr}\n\n"
        f'  url "https://github.com/{slug}/releases/download/v#{{version}}/{dmg_url_expr}"\n'
        f'  name "Second Brain"\n'
        f'  desc "Local-first verifiable AI memory platform with cryptographic provenance"\n'
        f'  homepage "https://github.com/{slug}"\n\n'
        f"  depends_on macos: \">= :ventura\"\n\n"
        f'  app "Second Brain.app"\n\n'
        f"  zap trash: [\n"
        f'    "~/Library/Application Support/SecondBrain",\n'
        f'    "~/Library/Preferences/ai.secondbrain.desktop.plist",\n'
        f"  ]\n"
        f"end\n"
    )

    winget_path = dest_root / "manifests" / "winget" / "SecondBrain.SecondBrain.yaml"
    homebrew_path = dest_root / "manifests" / "homebrew" / "second-brain.rb"
    winget_path.parent.mkdir(parents=True, exist_ok=True)
    homebrew_path.parent.mkdir(parents=True, exist_ok=True)

    atomic_write_bytes(str(winget_path), winget_yaml.encode("utf-8"))
    atomic_write_bytes(str(homebrew_path), homebrew_rb.encode("utf-8"))

    return {
        "version": ver,
        "repo_slug": slug,
        "winget_manifest": str(winget_path),
        "homebrew_cask": str(homebrew_path),
        "artifacts_bound": {
            "windows_exe": {"filename": exe_name, "sha256": exe_sha},
            "windows_msi": {"filename": msi_name, "sha256": msi_sha},
            "macos_dmg": {"filename": dmg_name, "sha256": dmg_sha},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(ROOT / "dist"), help="Output directory (default: dist/).")
    parser.add_argument("--dist-dir", default=None, help="Directory containing release binaries & SHA256SUMS.txt.")
    parser.add_argument("--version", default=None, help="Explicit semantic version override.")
    parser.add_argument("--repo-slug", default=None, help="GitHub owner/repo slug.")
    parser.add_argument("--require-checksums", action="store_true", help="Fail if real .exe/.dmg checksums are missing.")
    parser.add_argument("--pwa-only", action="store_true", help="Only build second-brain-inbox-drop-pwa.zip.")
    parser.add_argument("--manifests-only", action="store_true", help="Only build WinGet & Homebrew manifests.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result: dict[str, Any] = {}
    if not args.manifests_only:
        result["inbox_drop_pwa"] = build_inbox_drop_pwa_zip(args.out_dir)
    if not args.pwa_only:
        result["manifests"] = build_package_manager_manifests(
            out_dir=args.out_dir,
            dist_dir=args.dist_dir,
            version=args.version,
            repo_slug=args.repo_slug,
            require_checksums=bool(args.require_checksums),
        )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if "inbox_drop_pwa" in result:
            pwa = result["inbox_drop_pwa"]
            print(f"Built Mobile Inbox Drop PWA: {pwa['zip_path']} ({pwa['size_bytes']} bytes, sha256={pwa['sha256'][:16]}...)")
        if "manifests" in result:
            mf = result["manifests"]
            print(f"Built WinGet manifest:       {mf['winget_manifest']}")
            print(f"Built Homebrew Cask:         {mf['homebrew_cask']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
