"""Cross-platform VSIX packager for the Second Brain VS Code extension.

Reads `extensions/vscode-second-brain/package.json`, validates metadata/icons,
dynamically emits OPC `[Content_Types].xml` entries for every packaged file
extension (excluding `node_modules/`, `test/`, `.vscode-test/`, and `*.vsix`),
and writes `dist/second-brain-vscode-<version>.vsix`.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from xml.sax.saxutils import escape
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXT_DIR = REPO_ROOT / "extensions" / "vscode-second-brain"
DEFAULT_OUT_DIR = REPO_ROOT / "dist"

EXCLUDED_TOP_DIRS = frozenset({
    "node_modules",
    "test",
    "tests",
    ".vscode-test",
    ".git",
    "__pycache__",
    "out-test",
    "coverage",
})
EXCLUDED_DIR_NAMES = frozenset({"node_modules", "__pycache__", ".git", ".vscode-test"})
EXCLUDED_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", ".gitignore", "package-lock.json"})
EXCLUDED_SUFFIXES = frozenset({".vsix", ".pyc", ".pyo"})

CONTENT_TYPE_BY_EXT: dict[str, str] = {
    "cjs": "application/javascript",
    "css": "text/css",
    "gif": "image/gif",
    "htm": "text/html",
    "html": "text/html",
    "ico": "image/x-icon",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "js": "application/javascript",
    "json": "application/json",
    "map": "application/json",
    "md": "text/markdown",
    "mjs": "application/javascript",
    "png": "image/png",
    "svg": "image/svg+xml",
    "ts": "text/plain",
    "txt": "text/plain",
    "vsixmanifest": "text/xml",
    "webp": "image/webp",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "xml": "text/xml",
    "yaml": "text/yaml",
    "yml": "text/yaml",
}


def _xml_esc(value: object) -> str:
    return escape(str(value), {'"': "&quot;", "'": "&apos;"})


def _content_type_for_extension(ext: str) -> str:
    normalized = ext.lstrip(".").lower()
    if normalized in CONTENT_TYPE_BY_EXT:
        return CONTENT_TYPE_BY_EXT[normalized]
    guessed, _ = mimetypes.guess_type(f"file.{normalized}")
    return guessed or "application/octet-stream"


def _should_package_file(rel_parts: tuple[str, ...], file_path: Path) -> bool:
    if not rel_parts:
        return False
    if rel_parts[0] in EXCLUDED_TOP_DIRS:
        return False
    if any(part in EXCLUDED_DIR_NAMES for part in rel_parts[:-1]):
        return False
    if file_path.name in EXCLUDED_FILE_NAMES:
        return False
    if file_path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return True


def collect_extension_files(ext_dir: Path) -> list[tuple[Path, str]]:
    """Return sorted `(disk_path, archive_rel_posix)` pairs under `extension/`."""
    packaged: list[tuple[Path, str]] = []
    for path in sorted(ext_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ext_dir)
        if not _should_package_file(rel.parts, path):
            continue
        packaged.append((path, f"extension/{rel.as_posix()}"))
    return packaged


def _render_content_types_xml(archive_paths: list[str]) -> str:
    extensions: set[str] = {"vsixmanifest"}
    extensionless_parts: list[str] = []
    for arc_path in archive_paths:
        suffix = Path(arc_path).suffix.lstrip(".").lower()
        if suffix:
            extensions.add(suffix)
        else:
            extensionless_parts.append(arc_path)

    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
    ]
    for ext in sorted(extensions):
        ctype = _xml_esc(_content_type_for_extension(ext))
        parts.append(f'<Default Extension="{_xml_esc(ext)}" ContentType="{ctype}"/>')
    for part_name in sorted(extensionless_parts):
        parts.append(f'<Override PartName="/{_xml_esc(part_name)}" ContentType="text/plain"/>')
    parts.append("</Types>")
    return "".join(parts)


def _render_vsix_manifest(pkg: dict, *, has_readme: bool, icon_archive_path: str | None) -> str:
    name = _xml_esc(pkg["name"])
    version = _xml_esc(pkg["version"])
    publisher = _xml_esc(pkg["publisher"])
    display_name = _xml_esc(pkg.get("displayName") or pkg["name"])
    description = _xml_esc(pkg.get("description") or "")
    categories = ",".join(_xml_esc(c) for c in (pkg.get("categories") or []))
    engine_vscode = _xml_esc((pkg.get("engines") or {}).get("vscode", "^1.85.0"))

    icon_meta = f"<Icon>{_xml_esc(icon_archive_path)}</Icon>" if icon_archive_path else ""
    categories_meta = f"<Categories>{categories}</Categories>" if categories else ""

    assets = ['<Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json"/>']
    if has_readme:
        assets.append('<Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md"/>')
    if icon_archive_path:
        assets.append(
            f'<Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="{_xml_esc(icon_archive_path)}"/>'
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" '
        'xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">'
        "<Metadata>"
        f'<Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>'
        f"<DisplayName>{display_name}</DisplayName>"
        f'<Description xml:space="preserve">{description}</Description>'
        f"{categories_meta}"
        f"{icon_meta}"
        "</Metadata>"
        "<Installation>"
        f'<InstallationTarget Id="Microsoft.VisualStudio.Code" Version="{engine_vscode}"/>'
        "</Installation>"
        "<Dependencies/>"
        f"<Assets>{''.join(assets)}</Assets>"
        "</PackageManifest>"
    )


def build_vsix(ext_dir: Path = DEFAULT_EXT_DIR, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    pkg_json_path = ext_dir / "package.json"
    if not pkg_json_path.is_file():
        raise FileNotFoundError(f"VS Code extension package.json not found: {pkg_json_path}")

    pkg = json.loads(pkg_json_path.read_text(encoding="utf-8"))
    version = str(pkg["version"]).strip()
    if not version:
        raise ValueError("Extension package.json version must be non-empty.")

    icon_rel_raw = str(pkg.get("icon") or "").strip()
    icon_archive_path: str | None = None
    if icon_rel_raw:
        icon_rel = icon_rel_raw.lstrip("./").replace("\\", "/")
        icon_disk = ext_dir / icon_rel
        if not icon_disk.is_file():
            raise FileNotFoundError(f"Extension icon declared in package.json does not exist: {icon_disk}")
        icon_archive_path = f"extension/{icon_rel}"

    packaged_files = collect_extension_files(ext_dir)
    archive_paths = [arc for _, arc in packaged_files]
    has_readme = "extension/README.md" in archive_paths

    content_types_xml = _render_content_types_xml(archive_paths)
    manifest_xml = _render_vsix_manifest(pkg, has_readme=has_readme, icon_archive_path=icon_archive_path)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"second-brain-vscode-{version}.vsix"
    tmp_path = out_path.with_suffix(".vsix.tmp")

    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml.encode("utf-8"))
        zf.writestr("extension.vsixmanifest", manifest_xml.encode("utf-8"))
        for disk_path, arc_path in packaged_files:
            zf.write(disk_path, arc_path)

    tmp_path.replace(out_path)
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ext-dir", type=Path, default=DEFAULT_EXT_DIR, help="Path to VS Code extension source dir.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory for built .vsix.")
    args = parser.parse_args(argv)

    built = build_vsix(ext_dir=args.ext_dir.resolve(), out_dir=args.out_dir.resolve())
    print(f"Built VSIX: {built} ({built.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
