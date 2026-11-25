# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Build deterministic Obsidian community plugin bundle (`dist/second-brain-obsidian-<ver>.zip`)."""



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
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "extensions" / "obsidian-second-brain"


def build_obsidian_plugin(out_dir: str | Path = ROOT / "dist") -> dict[str, Any]:
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    dest = Path(out_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / f"second-brain-obsidian-{version}.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fn in ("manifest.json", "main.js", "styles.css"):
            fp = PLUGIN_DIR / fn
            zinfo = zipfile.ZipInfo(f"second-brain-obsidian/{fn}", date_time=(2026, 1, 1, 0, 0, 0))
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = 0o644 << 16
            zf.writestr(zinfo, fp.read_bytes())

    return {
        "status": "built",
        "version": version,
        "zip_path": str(zip_path),
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    res = build_obsidian_plugin(args.out_dir)
    print(json.dumps(res, indent=2) if args.json else res["zip_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
