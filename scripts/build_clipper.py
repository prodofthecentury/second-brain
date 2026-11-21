# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Package the local-first Browser Web Clipper (MV3) extension into a clean .zip archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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


def build_clipper_zip(out_dir: str | Path | None = None) -> Path:
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
            zf.write(src, arcname=rel)
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "dist"))
    args = parser.parse_args(argv)
    out = build_clipper_zip(args.out_dir)
    print(f"Built Browser Web Clipper bundle: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
