# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["PyYAML"]
# ///
"""Build Mobile PWA release zip & WinGet / Homebrew distribution manifests.

Outputs under `--out-dir` (default `dist/`):
  * `second-brain-inbox-drop-pwa.zip` — deterministic offline-capable static WebCrypto PWA bundle
  * `manifests/winget/SecondBrain.SecondBrain.yaml` — WinGet singleton manifest (schema 1.6.0)
  * `manifests/homebrew/second-brain.rb` — Homebrew Formula (or macOS Cask when universal `.dmg` is present)
  * `manifests/homebrew/Formula/second-brain.rb` — Homebrew Python Formula when `.tar.gz` / `.whl` is present
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
_ZERO_SHA256 = "0" * 64


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
    """Export and deterministically zip the static Zero-Knowledge Mobile PWA."""
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
    """Load artifact SHA-256 digests from SHA256SUMS.txt and verify against on-disk files."""
    checksums: dict[str, str] = {}
    sums_file = dist_dir / "SHA256SUMS.txt"
    if sums_file.is_file() and not _is_reparse_or_link(str(sums_file)):
        raw_text = sums_file.read_text(encoding="utf-8-sig", errors="replace")
        for line in raw_text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and _SHA256_HEX_RE.fullmatch(parts[0]):
                digest = parts[0].lower()
                if digest == _ZERO_SHA256:
                    raise ValueError(f"Placeholder all-zero SHA-256 forbidden in SHA256SUMS.txt: {line!r}")
                fname = parts[-1].lstrip("*./")
                if not _SAFE_ARTIFACT_NAME_RE.fullmatch(fname) or ".." in fname:
                    raise ValueError(f"Unsafe artifact filename in SHA256SUMS.txt: {fname!r}")
                checksums[fname] = digest

    if dist_dir.is_dir() and not _is_reparse_or_link(str(dist_dir)):
        for entry in sorted(dist_dir.iterdir()):
            if (
                entry.is_file()
                and not _is_reparse_or_link(str(entry))
                and entry.name != "SHA256SUMS.txt"
                and _SAFE_ARTIFACT_NAME_RE.fullmatch(entry.name)
            ):
                disk_sha = hashlib.sha256(entry.read_bytes()).hexdigest()
                if entry.name in checksums and checksums[entry.name] != disk_sha:
                    raise ValueError(
                        f"SHA-256 mismatch for {entry.name}: SHA256SUMS.txt has {checksums[entry.name]}, "
                        f"disk file has {disk_sha}."
                    )
                checksums[entry.name] = disk_sha
    return checksums


def _filename_matches_version(filename: str, version: str) -> bool:
    """Return True iff `filename` carries exact semantic `version` without adjacent version digits."""
    pattern = re.compile(
        rf"(?<![0-9])(?<![0-9]\.)v?{re.escape(version)}(?![0-9])(?!\.[0-9])",
        re.IGNORECASE,
    )
    return bool(pattern.search(filename))


def _match_artifact(
    checksums: dict[str, str],
    suffixes: str | tuple[str, ...],
    *,
    version: str,
    required: bool,
) -> tuple[str, str] | None:
    suffix_tuple = (suffixes,) if isinstance(suffixes, str) else suffixes
    for sfx in suffix_tuple:
        sfx_low = sfx.lower()
        matches = sorted(
            name
            for name in checksums
            if name.lower().endswith(sfx_low)
            and _filename_matches_version(name, version)
            and _SHA256_HEX_RE.fullmatch(checksums[name])
            and checksums[name].lower() != _ZERO_SHA256
        )
        if matches:
            chosen = matches[0]
            return chosen, checksums[chosen].lower()
    if required:
        wanted = " | ".join(f"*{version}*{s}" for s in suffix_tuple)
        raise FileNotFoundError(
            f"Required release artifact matching ({wanted}) not found in SHA256SUMS.txt or dist/."
        )
    return None


def _collect_homebrew_resources() -> list[tuple[str, str, str]]:
    """Extract (name, url, sha256) for transitive core PyPI dependencies from uv.lock."""
    lock_path = ROOT / "uv.lock"
    if not lock_path.is_file():
        return []
    try:
        import tomllib

        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    packages = data.get("package")
    if not isinstance(packages, list):
        return []
    by_name = {str(p.get("name")): p for p in packages if isinstance(p, dict) and p.get("name")}
    sb = by_name.get("second-brain")
    if not isinstance(sb, dict):
        return []

    seen: set[str] = set()
    queue: list[str] = [
        str(dep["name"])
        for dep in (sb.get("dependencies") or [])
        if isinstance(dep, dict) and dep.get("name") and "win32" not in str(dep.get("marker") or "")
    ]
    while queue:
        pkg_name = queue.pop(0)
        if pkg_name in seen or pkg_name == "second-brain" or pkg_name not in by_name:
            continue
        seen.add(pkg_name)
        for dep in (by_name[pkg_name].get("dependencies") or []):
            if isinstance(dep, dict) and dep.get("name") and "win32" not in str(dep.get("marker") or ""):
                queue.append(str(dep["name"]))

    resources: list[tuple[str, str, str]] = []
    for pkg_name in sorted(seen):
        sdist = by_name[pkg_name].get("sdist")
        if not isinstance(sdist, dict):
            continue
        url = str(sdist.get("url") or "").strip()
        raw_hash = str(sdist.get("hash") or "").strip()
        if raw_hash.startswith("sha256:"):
            raw_hash = raw_hash[len("sha256:") :]
        if url.startswith("https://") and len(raw_hash) == 64:
            resources.append((pkg_name, url, raw_hash.lower()))
    return resources


def _render_homebrew_formula(*, slug: str, version: str, base_download: str, artifact_name: str, artifact_sha: str) -> str:
    resources = _collect_homebrew_resources()
    resource_stanzas = ""
    if resources:
        blocks = [
            f'  resource "{r_name}" do\n'
            f'    url "{r_url}"\n'
            f'    sha256 "{r_sha}"\n'
            "  end\n"
            for r_name, r_url, r_sha in resources
        ]
        resource_stanzas = "\n" + "\n".join(blocks)
    return (
        "class SecondBrain < Formula\n"
        "  include Language::Python::Virtualenv\n\n"
        '  desc "Local-first verifiable AI memory platform with cryptographic provenance"\n'
        f'  homepage "https://github.com/{slug}"\n'
        f'  url "{base_download}/{artifact_name}"\n'
        f'  version "{version}"\n'
        f'  sha256 "{artifact_sha.lower()}"\n'
        '  license "MIT"\n\n'
        '  depends_on "rust" => :build\n'
        '  depends_on "python@3.11"\n'
        f"{resource_stanzas}\n"
        "  def install\n"
        "    virtualenv_install_with_resources\n"
        "  end\n\n"
        "  test do\n"
        '    ENV["BRAIN_VAULT_DIR"] = (testpath/"brain").to_s\n'
        '    assert_predicate bin/"second-brain", :exist?\n'
        '    system bin/"second-brain", "install"\n'
        '    system bin/"second-brain", "doctor"\n'
        "  end\n"
        "end\n"
    )


def _render_homebrew_cask(*, slug: str, version: str, base_download: str, dmg_name: str, dmg_sha: str) -> str:
    return (
        'cask "second-brain" do\n'
        f'  version "{version}"\n'
        f'  sha256 "{dmg_sha.lower()}"\n\n'
        f'  url "{base_download}/{dmg_name}"\n'
        '  name "Second Brain"\n'
        '  desc "Local-first verifiable AI memory platform with cryptographic provenance"\n'
        f'  homepage "https://github.com/{slug}"\n\n'
        '  depends_on macos: ">= :ventura"\n\n'
        '  app "Second Brain.app"\n\n'
        "  zap trash: [\n"
        '    "~/Library/Application Support/SecondBrain",\n'
        '    "~/Library/Preferences/ai.secondbrain.desktop.plist",\n'
        "  ]\n"
        "end\n"
    )


def build_package_manager_manifests(
    *,
    out_dir: str | Path = ROOT / "dist",
    dist_dir: str | Path | None = None,
    version: str | None = None,
    repo_slug: str | None = None,
    require_checksums: bool = False,
) -> dict[str, Any]:
    """Generate validated WinGet YAML and Homebrew `.rb` manifests strictly bound to `version`."""
    ver = _resolve_project_version(version)
    slug = (repo_slug or os.environ.get("GITHUB_REPOSITORY") or "second-brain/second-brain").strip()
    if not _REPO_SLUG_RE.fullmatch(slug):
        raise ValueError(f"Invalid GitHub repository slug: {slug!r}")

    dest_root = Path(os.path.abspath(str(out_dir)))
    artifact_dir = Path(os.path.abspath(str(dist_dir))) if dist_dir is not None else dest_root
    _assert_no_symlink_ancestors(dest_root)
    _assert_no_symlink_ancestors(artifact_dir)

    checksums = _load_checksums(artifact_dir)
    exe_match = _match_artifact(checksums, ".exe", version=ver, required=True)
    assert exe_match is not None
    exe_name, exe_sha = exe_match

    msi_match = _match_artifact(checksums, ".msi", version=ver, required=False)
    dmg_match = _match_artifact(checksums, ".dmg", version=ver, required=False)
    py_match = _match_artifact(checksums, (".tar.gz", ".whl"), version=ver, required=False)

    if require_checksums and dmg_match is None and py_match is None:
        raise FileNotFoundError(
            f"Required Homebrew artifact (*{ver}*.dmg, *{ver}*.tar.gz, or *{ver}*.whl) not found in dist/."
        )
    if dmg_match is None and py_match is None:
        raise FileNotFoundError(
            f"Cannot generate Homebrew formula/cask without a real versioned artifact (*{ver}*.tar.gz, *{ver}*.whl, or *{ver}*.dmg)."
        )

    base_download = f"https://github.com/{slug}/releases/download/v{ver}"
    # Emit the NSIS (`nullsoft`) installer in the WinGet singleton manifest so `winget validate`
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
    parsed_yaml = yaml.safe_load(winget_yaml)
    if not isinstance(parsed_yaml, dict) or parsed_yaml.get("PackageVersion") != ver:
        raise RuntimeError("Generated WinGet YAML failed validation.")

    winget_path = dest_root / "manifests" / "winget" / "SecondBrain.SecondBrain.yaml"
    homebrew_path = dest_root / "manifests" / "homebrew" / "second-brain.rb"
    formula_subpath = dest_root / "manifests" / "homebrew" / "Formula" / "second-brain.rb"
    winget_path.parent.mkdir(parents=True, exist_ok=True)
    homebrew_path.parent.mkdir(parents=True, exist_ok=True)

    formula_rb: str | None = None
    if py_match is not None:
        py_name, py_sha = py_match
        formula_rb = _render_homebrew_formula(
            slug=slug,
            version=ver,
            base_download=base_download,
            artifact_name=py_name,
            artifact_sha=py_sha,
        )
        formula_subpath.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(str(formula_subpath), formula_rb.encode("utf-8"))

    if dmg_match is not None:
        dmg_name, dmg_sha = dmg_match
        primary_brew_rb = _render_homebrew_cask(
            slug=slug,
            version=ver,
            base_download=base_download,
            dmg_name=dmg_name,
            dmg_sha=dmg_sha,
        )
    else:
        assert formula_rb is not None
        primary_brew_rb = formula_rb

    atomic_write_bytes(str(winget_path), winget_yaml.encode("utf-8"))
    atomic_write_bytes(str(homebrew_path), primary_brew_rb.encode("utf-8"))

    # Also emit official microsoft/winget-pkgs multi-file 1.6.0 manifests (version + installer + defaultLocale)
    winget_pkgs_dir = (
        dest_root / "manifests" / "winget-pkgs" / "manifests" / "s" / "SecondBrain" / "SecondBrain" / ver
    )
    winget_pkgs_dir.mkdir(parents=True, exist_ok=True)
    ver_manifest = (
        "# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.1.6.0.schema.json\n"
        + yaml.safe_dump(
            {
                "PackageIdentifier": "SecondBrain.SecondBrain",
                "PackageVersion": ver,
                "DefaultLocale": "en-US",
                "ManifestType": "version",
                "ManifestVersion": "1.6.0",
            },
            sort_keys=False,
            allow_unicode=True,
        )
    )
    inst_manifest = (
        "# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.1.6.0.schema.json\n"
        + yaml.safe_dump(
            {
                "PackageIdentifier": "SecondBrain.SecondBrain",
                "PackageVersion": ver,
                "Commands": ["second-brain"],
                "Installers": installers,
                "ManifestType": "installer",
                "ManifestVersion": "1.6.0",
            },
            sort_keys=False,
            allow_unicode=True,
        )
    )
    loc_manifest = (
        "# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.1.6.0.schema.json\n"
        + yaml.safe_dump(
            {
                "PackageIdentifier": "SecondBrain.SecondBrain",
                "PackageVersion": ver,
                "PackageLocale": "en-US",
                "Publisher": "Second Brain",
                "PublisherUrl": f"https://github.com/{slug}",
                "PackageName": "Second Brain",
                "PackageUrl": f"https://github.com/{slug}",
                "License": "MIT",
                "ShortDescription": "Local-first verifiable AI memory platform with cryptographic byte-offset provenance.",
                "Tags": ["ai", "memory", "knowledge-base", "local-first", "rag", "mcp"],
                "ManifestType": "defaultLocale",
                "ManifestVersion": "1.6.0",
            },
            sort_keys=False,
            allow_unicode=True,
        )
    )
    atomic_write_bytes(str(winget_pkgs_dir / "SecondBrain.SecondBrain.yaml"), ver_manifest.encode("utf-8"))
    atomic_write_bytes(str(winget_pkgs_dir / "SecondBrain.SecondBrain.installer.yaml"), inst_manifest.encode("utf-8"))
    atomic_write_bytes(str(winget_pkgs_dir / "SecondBrain.SecondBrain.locale.en-US.yaml"), loc_manifest.encode("utf-8"))

    # Emit ready-to-push Homebrew Tap repository layout (Casks/second-brain.rb + Formula/second-brain.rb + README.md)
    tap_owner = slug.split("/", 1)[0]
    tap_dir = dest_root / "manifests" / "homebrew-tap"
    (tap_dir / "Casks").mkdir(parents=True, exist_ok=True)
    if dmg_match is not None:
        atomic_write_bytes(str(tap_dir / "Casks" / "second-brain.rb"), primary_brew_rb.encode("utf-8"))
    if formula_rb is not None:
        (tap_dir / "Formula").mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(str(tap_dir / "Formula" / "second-brain.rb"), formula_rb.encode("utf-8"))
    tap_readme = (
        f"# Homebrew Tap for Second Brain\n\n"
        f"Official Homebrew tap for [Second Brain](https://github.com/{slug}) (`v{ver}`).\n\n"
        f"## Install Desktop App (macOS Universal Cask)\n\n"
        f"```bash\n"
        f"brew tap {tap_owner}/second-brain\n"
        f"brew install --cask second-brain\n"
        f"```\n\n"
        f"## Install CLI Formula\n\n"
        f"```bash\n"
        f"brew tap {tap_owner}/second-brain\n"
        f"brew install second-brain\n"
        f"```\n"
    )
    atomic_write_bytes(str(tap_dir / "README.md"), tap_readme.encode("utf-8"))

    bound: dict[str, Any] = {
        "windows_exe": {"filename": exe_name, "sha256": exe_sha},
    }
    if msi_match is not None:
        bound["windows_msi"] = {"filename": msi_match[0], "sha256": msi_match[1]}
    if dmg_match is not None:
        bound["macos_dmg"] = {"filename": dmg_match[0], "sha256": dmg_match[1]}
    if py_match is not None:
        bound["python_source"] = {"filename": py_match[0], "sha256": py_match[1]}

    return {
        "version": ver,
        "repo_slug": slug,
        "winget_manifest": str(winget_path),
        "winget_pkgs_dir": str(winget_pkgs_dir),
        "homebrew_cask": str(homebrew_path),
        "homebrew_formula": str(formula_subpath) if py_match is not None else str(homebrew_path),
        "homebrew_tap_dir": str(tap_dir),
        "artifacts_bound": bound,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(ROOT / "dist"), help="Output directory (default: dist/).")
    parser.add_argument("--dist-dir", default=None, help="Directory containing release binaries & SHA256SUMS.txt.")
    parser.add_argument("--version", default=None, help="Explicit semantic version override.")
    parser.add_argument("--repo-slug", default=None, help="GitHub owner/repo slug.")
    parser.add_argument("--require-checksums", action="store_true", help="Require verified release checksums.")
    parser.add_argument("--pwa-only", action="store_true", help="Only build second-brain-inbox-drop-pwa.zip.")
    parser.add_argument("--manifests-only", action="store_true", help="Only build WinGet & Homebrew manifests.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result: dict[str, Any] = {}
    if not args.manifests_only:
        result["inbox_drop_pwa"] = build_inbox_drop_pwa_zip(args.out_dir)
        try:
            from mobile_vault_client import export_mobile_vault_client_bundle

            result["mobile_vault_client"] = export_mobile_vault_client_bundle(args.out_dir)
        except Exception as exc:
            raise RuntimeError(f"Failed to build Mobile Vault Client bundle: {exc}") from exc
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
            print(f"Built Mobile PWA:       {pwa['zip_path']} ({pwa['size_bytes']} bytes, sha256={pwa['sha256'][:16]}...)")
        if "manifests" in result:
            mf = result["manifests"]
            print(f"Built WinGet manifest:  {mf['winget_manifest']}")
            print(f"Built Homebrew formula: {mf['homebrew_cask']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
