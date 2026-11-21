# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pillow>=10"]
# ///
"""Package the local-first Browser Web Clipper (MV3) into universal, Chromium (Chrome/Edge), and Firefox AMO bundles."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from typing import Any
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIPPER_DIR = REPO_ROOT / "extensions" / "browser-clipper"
INCLUDE_FILES = (
    "manifest.json",
    "background.js",
    "popup.html",
    "popup.css",
    "popup.js",
    "README.md",
    "lib/clipper-client.js",
    "lib/readability-markdown.js",
)
_FIXED_ZIP_DT = (2026, 1, 1, 0, 0, 0)


def _render_clipper_icon_png(size: int) -> bytes:
    """Deterministically generate crisp Second Brain clipper icon PNGs (16, 48, 128 px) for store validation."""
    from PIL import Image, ImageDraw

    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, big // 16)
    radius = max(4, big // 5)
    draw.rounded_rectangle(
        [pad, pad, big - pad - 1, big - pad - 1],
        radius=radius,
        fill=(15, 23, 42, 255),
        outline=(56, 189, 248, 255),
        width=max(2, big // 18),
    )
    cx, cy = big // 2, big // 2
    r_outer = int(big * 0.27)
    r_inner = max(2, int(big * 0.11))
    draw.ellipse(
        [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
        outline=(56, 189, 248, 255),
        width=max(2, big // 16),
    )
    draw.ellipse(
        [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
        fill=(129, 140, 248, 255),
    )
    resized = img.resize((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _write_deterministic_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    zinfo = zipfile.ZipInfo(filename=arcname, date_time=_FIXED_ZIP_DT)
    zinfo.compress_type = zipfile.ZIP_DEFLATED
    zinfo.external_attr = 0o644 << 16
    zf.writestr(zinfo, data)


def build_clipper_zip(out_dir: str | Path | None = None) -> Path:
    """Build the universal MV3 Browser Web Clipper archive (preserving existing contract)."""
    manifest = json.loads((CLIPPER_DIR / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "1.0.0")
    dest_dir = Path(out_dir).expanduser().resolve() if out_dir else (REPO_ROOT / "dist")
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_path = dest_dir / f"second-brain-clipper-{version}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE_FILES:
            src = CLIPPER_DIR / rel
            if not src.is_file():
                raise FileNotFoundError(f"Missing browser clipper asset: {src}")
            _write_deterministic_entry(zf, rel, src.read_bytes())
    return archive_path


def build_store_clipper_bundles(out_dir: str | Path | None = None) -> dict[str, Any]:
    """Build store-ready Chrome/Edge MV3 (.zip) and Firefox Add-ons MV3 (.xpi) packages alongside universal zip."""
    universal_path = build_clipper_zip(out_dir)
    base_manifest = json.loads((CLIPPER_DIR / "manifest.json").read_text(encoding="utf-8"))
    version = str(base_manifest.get("version") or "1.0.0")
    dest_dir = universal_path.parent

    icons = {sz: _render_clipper_icon_png(sz) for sz in (16, 48, 128)}
    icon_map = {str(sz): f"icons/icon-{sz}.png" for sz in (16, 48, 128)}

    # 1. Chromium (Chrome Web Store + Microsoft Edge Add-ons): strict service_worker-only MV3 + icons
    chromium_manifest = dict(base_manifest)
    chromium_manifest.pop("browser_specific_settings", None)
    chromium_bg = dict(chromium_manifest.get("background") or {})
    chromium_bg.pop("scripts", None)
    chromium_manifest["background"] = chromium_bg
    chromium_manifest["icons"] = icon_map
    chromium_action = dict(chromium_manifest.get("action") or {})
    chromium_action["default_icon"] = icon_map
    chromium_manifest["action"] = chromium_action

    # Ensure background.js imports helper scripts in pure service-worker mode if importScripts is not already present
    raw_bg_js = (CLIPPER_DIR / "background.js").read_text(encoding="utf-8")
    if "importScripts(" not in raw_bg_js:
        chromium_bg_js = (
            'if (typeof importScripts === "function") {\n'
            '  try { importScripts("lib/clipper-client.js", "lib/readability-markdown.js"); } catch (_) {}\n'
            "}\n" + raw_bg_js
        ).encode("utf-8")
    else:
        chromium_bg_js = raw_bg_js.encode("utf-8")

    chromium_path = dest_dir / f"second-brain-clipper-chromium-{version}.zip"
    with zipfile.ZipFile(chromium_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_deterministic_entry(
            zf, "manifest.json", (json.dumps(chromium_manifest, indent=2) + "\n").encode("utf-8")
        )
        _write_deterministic_entry(zf, "background.js", chromium_bg_js)
        for rel in INCLUDE_FILES:
            if rel in {"manifest.json", "background.js"}:
                continue
            _write_deterministic_entry(zf, rel, (CLIPPER_DIR / rel).read_bytes())
        for sz, png_bytes in icons.items():
            _write_deterministic_entry(zf, f"icons/icon-{sz}.png", png_bytes)

    # 2. Firefox AMO MV3 (.xpi): background.scripts + browser_specific_settings.gecko + data_collection_permissions + icons
    firefox_manifest = dict(base_manifest)
    firefox_bg = dict(firefox_manifest.get("background") or {})
    firefox_bg.pop("service_worker", None)
    firefox_manifest["background"] = firefox_bg
    gecko = dict((firefox_manifest.get("browser_specific_settings") or {}).get("gecko") or {})
    gecko.setdefault("id", "clipper@secondbrain.local")
    gecko.setdefault("strict_min_version", "109.0")
    gecko.setdefault("data_collection_permissions", {"required": ["none"]})
    firefox_manifest["browser_specific_settings"] = {"gecko": gecko}
    firefox_manifest["icons"] = icon_map
    firefox_action = dict(firefox_manifest.get("action") or {})
    firefox_action["default_icon"] = icon_map
    firefox_manifest["action"] = firefox_action

    firefox_path = dest_dir / f"second-brain-clipper-firefox-{version}.xpi"
    with zipfile.ZipFile(firefox_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_deterministic_entry(
            zf, "manifest.json", (json.dumps(firefox_manifest, indent=2) + "\n").encode("utf-8")
        )
        for rel in INCLUDE_FILES:
            if rel == "manifest.json":
                continue
            _write_deterministic_entry(zf, rel, (CLIPPER_DIR / rel).read_bytes())
        for sz, png_bytes in icons.items():
            _write_deterministic_entry(zf, f"icons/icon-{sz}.png", png_bytes)

    return {
        "version": version,
        "universal_zip": str(universal_path),
        "universal_sha256": hashlib.sha256(universal_path.read_bytes()).hexdigest(),
        "chromium_zip": str(chromium_path),
        "chromium_sha256": hashlib.sha256(chromium_path.read_bytes()).hexdigest(),
        "firefox_xpi": str(firefox_path),
        "firefox_sha256": hashlib.sha256(firefox_path.read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "dist"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    res = build_store_clipper_bundles(args.out_dir)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Built Browser Web Clipper (universal): {res['universal_zip']}")
        print(f"Built Browser Web Clipper (Chrome/Edge): {res['chromium_zip']}")
        print(f"Built Browser Web Clipper (Firefox AMO): {res['firefox_xpi']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
