"""Deterministically generate multi-platform Second Brain icons from desktop/assets/icon.png.

Outputs:
  - desktop/assets/icon.ico (Windows multi-size: 16, 24, 32, 48, 64, 128, 256)
  - desktop/assets/icon.icns (macOS ICNS bundle via Pillow)
  - extensions/vscode-second-brain/media/icon.png (128x128 VS Code Marketplace icon)
  - mobile/inbox-drop/icons/ (icon-192.png, icon-512.png, apple-touch-icon.png)
  - mobile/vault-client/www/icons/ (icon-192.png, icon-512.png, apple-touch-icon.png)
  - extensions/browser-clipper/icons/ (icon-16.png, icon-32.png, icon-48.png, icon-128.png)
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
import io
from pathlib import Path
import struct

from PIL import Image, PngImagePlugin

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_PNG = REPO_ROOT / "desktop" / "assets" / "icon.png"
DEFAULT_WIN_ICO = REPO_ROOT / "desktop" / "assets" / "icon.ico"
DEFAULT_MAC_ICNS = REPO_ROOT / "desktop" / "assets" / "icon.icns"
DEFAULT_VSCODE_PNG = REPO_ROOT / "extensions" / "vscode-second-brain" / "media" / "icon.png"
DEFAULT_INBOX_DROP_ICONS_DIR = REPO_ROOT / "mobile" / "inbox-drop" / "icons"
DEFAULT_VAULT_CLIENT_ICONS_DIR = REPO_ROOT / "mobile" / "vault-client" / "www" / "icons"
DEFAULT_CLIPPER_ICONS_DIR = REPO_ROOT / "extensions" / "browser-clipper" / "icons"

PWA_ICON_SPECS: tuple[tuple[str, int], ...] = (
    ("icon-192.png", 192),
    ("icon-512.png", 512),
    ("apple-touch-icon.png", 180),
)
CLIPPER_ICON_SPECS: tuple[tuple[str, int], ...] = (
    ("icon-16.png", 16),
    ("icon-32.png", 32),
    ("icon-48.png", 48),
    ("icon-128.png", 128),
)

ICO_SIZES: tuple[tuple[int, int], ...] = (
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
)


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def _save_deterministic_png(rgba: Image.Image, size: int, target: Path) -> Path:
    resized = rgba.resize((size, size), Image.Resampling.LANCZOS)
    png_buf = io.BytesIO()
    resized.save(
        png_buf,
        format="PNG",
        pnginfo=PngImagePlugin.PngInfo(),
        optimize=False,
        compress_level=9,
    )
    _write_bytes_atomic(target, png_buf.getvalue())
    return target


def generate_icons(
    source_png: Path = DEFAULT_SOURCE_PNG,
    win_ico: Path = DEFAULT_WIN_ICO,
    mac_icns: Path = DEFAULT_MAC_ICNS,
    vscode_png: Path = DEFAULT_VSCODE_PNG,
    inbox_drop_icons_dir: Path = DEFAULT_INBOX_DROP_ICONS_DIR,
    vault_client_icons_dir: Path = DEFAULT_VAULT_CLIENT_ICONS_DIR,
    clipper_icons_dir: Path = DEFAULT_CLIPPER_ICONS_DIR,
) -> dict[str, Path]:
    if not source_png.is_file():
        raise FileNotFoundError(f"Source icon not found: {source_png}")

    with Image.open(source_png) as src:
        rgba = src.convert("RGBA")

    # 1. Windows multi-resolution .ico (16, 24, 32, 48, 64, 128, 256)
    ico_base = rgba.resize((256, 256), Image.Resampling.LANCZOS)
    ico_buf = io.BytesIO()
    ico_base.save(ico_buf, format="ICO", sizes=list(ICO_SIZES))
    ico_bytes = ico_buf.getvalue()
    reserved, ico_type, ico_count = struct.unpack_from("<HHH", ico_bytes, 0)
    if (reserved, ico_type, ico_count) != (0, 1, len(ICO_SIZES)):
        raise RuntimeError(
            f"Generated ICO header invalid: reserved={reserved}, type={ico_type}, count={ico_count}"
        )
    _write_bytes_atomic(win_ico, ico_bytes)

    # 2. macOS .icns (1024x1024 retina base so Pillow populates standard ICNS slots)
    icns_base = rgba.resize((1024, 1024), Image.Resampling.LANCZOS)
    icns_buf = io.BytesIO()
    icns_base.save(icns_buf, format="ICNS")
    icns_bytes = icns_buf.getvalue()
    if icns_bytes[:4] != b"icns":
        raise RuntimeError("Generated ICNS file missing 'icns' magic header.")
    _write_bytes_atomic(mac_icns, icns_bytes)

    # 3. Deterministic PNGs for VS Code, Mobile PWAs, and Browser Clipper
    _save_deterministic_png(rgba, 128, vscode_png)
    outputs: dict[str, Path] = {
        "ico": win_ico,
        "icns": mac_icns,
        "vscode_png": vscode_png,
    }
    for fname, sz in PWA_ICON_SPECS:
        outputs[f"inbox_drop_{fname}"] = _save_deterministic_png(rgba, sz, inbox_drop_icons_dir / fname)
        outputs[f"vault_client_{fname}"] = _save_deterministic_png(rgba, sz, vault_client_icons_dir / fname)
    for fname, sz in CLIPPER_ICON_SPECS:
        outputs[f"clipper_{fname}"] = _save_deterministic_png(rgba, sz, clipper_icons_dir / fname)

    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PNG, help="Source PNG path.")
    parser.add_argument("--ico", type=Path, default=DEFAULT_WIN_ICO, help="Output Windows .ico path.")
    parser.add_argument("--icns", type=Path, default=DEFAULT_MAC_ICNS, help="Output macOS .icns path.")
    parser.add_argument("--vscode-png", type=Path, default=DEFAULT_VSCODE_PNG, help="Output 128x128 VS Code PNG path.")
    args = parser.parse_args(argv)

    outputs = generate_icons(
        source_png=args.source,
        win_ico=args.ico,
        mac_icns=args.icns,
        vscode_png=args.vscode_png,
    )
    for kind, path in outputs.items():
        print(f"[{kind}] {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
