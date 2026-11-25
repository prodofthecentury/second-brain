# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Build and optionally sign the native Apple iOS Shortcut (`Second Brain Quick Capture`).

Generates a standard Apple Shortcuts binary plist workflow (`WFWorkflow`) capturing text,
URL, or Share Sheet input and posting it to the configured loopback / tunnel inbox capture endpoint.
On macOS hosts with Xcode/Shortcuts CLI (`shortcuts sign --mode anyone`), signs the `.shortcut`
artifact for iOS 15+ import; on Windows/Linux emits the deterministic `.unsigned.shortcut` binary
plist + signing metadata JSON ready for macOS CI signing.
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
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def build_ios_shortcut_plist(
    *,
    endpoint_url: str = "http://127.0.0.1:8790/v1/inbox/drop",
    bearer_placeholder: str = "REPLACE_WITH_PAIR_OR_TUNNEL_TOKEN",
) -> dict[str, Any]:
    """Return a compliant Apple Shortcuts `WFWorkflow` dictionary for Quick Capture."""
    clean_url = str(endpoint_url or "http://127.0.0.1:8790/v1/inbox/drop").strip()
    return {
        "WFWorkflowClientVersion": "2612.0.4",
        "WFWorkflowClientRelease": "2.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 463140863,
            "WFWorkflowIconGlyphNumber": 59446,
        },
        "WFWorkflowImportQuestions": [
            {
                "ActionIndex": 1,
                "Category": "Parameter",
                "DefaultValue": clean_url,
                "ParameterKey": "WFURLActionURL",
                "Text": "Second Brain Capture Endpoint URL (Tailscale/Cloudflare tunnel or local URL)",
            },
            {
                "ActionIndex": 2,
                "Category": "Parameter",
                "DefaultValue": f"Bearer {bearer_placeholder}",
                "ParameterKey": "WFHTTPHeaders",
                "Text": "Authorization Bearer Token",
            },
        ],
        "WFWorkflowInputContentItemClasses": [
            "WFStringContentItem",
            "WFURLContentItem",
            "WFRichTextContentItem",
            "WFArticleContentItem",
        ],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": ["NCWidget", "Watch", "ActionExtension"],
        "WFWorkflowActions": [
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.ask",
                "WFWorkflowActionParameters": {
                    "WFAskActionPrompt": "Capture note for Second Brain Vault (0-raw/inbox/)",
                    "WFInputType": "Text",
                    "WFAskActionDefaultAnswer": "",
                },
            },
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.url",
                "WFWorkflowActionParameters": {
                    "WFURLActionURL": clean_url,
                },
            },
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
                "WFWorkflowActionParameters": {
                    "WFHTTPMethod": "POST",
                    "WFHTTPBodyType": "Json",
                    "WFHTTPHeaders": {
                        "Authorization": f"Bearer {bearer_placeholder}",
                        "Content-Type": "application/json",
                    },
                    "WFJSONValues": {
                        "source": "ios_shortcut",
                        "origin": "mobile_shortcut",
                        "text": "{ShortcutInput_or_Ask}",
                    },
                },
            },
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.notification",
                "WFWorkflowActionParameters": {
                    "WFNotificationActionTitle": "Second Brain",
                    "WFNotificationActionBody": "Capture queued into quarantined 0-raw/inbox/.",
                },
            },
        ],
    }


def build_ios_shortcut(
    out_dir: str | Path = ROOT / "dist",
    *,
    version: str = "1.2.0",
    endpoint_url: str = "http://127.0.0.1:8790/v1/inbox/drop",
    sign_if_available: bool = True,
) -> dict[str, Any]:
    """Generate `second-brain-ios-capture-<ver>.unsigned.shortcut` and sign on macOS if available."""
    dest = Path(out_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    plist_doc = build_ios_shortcut_plist(endpoint_url=endpoint_url)
    raw_bytes = plistlib.dumps(plist_doc, fmt=plistlib.FMT_BINARY, sort_keys=True)

    unsigned_path = dest / f"second-brain-ios-capture-{version}.unsigned.shortcut"
    unsigned_path.write_bytes(raw_bytes)

    signed_path = dest / f"second-brain-ios-capture-{version}.shortcut"
    signed = False
    sign_note = "macos_shortcuts_cli_unavailable"
    shortcuts_bin = shutil.which("shortcuts") if (sign_if_available and sys.platform == "darwin") else None
    if shortcuts_bin:
        proc = subprocess.run(
            [shortcuts_bin, "sign", "--mode", "anyone", "-i", str(unsigned_path), "-o", str(signed_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and signed_path.is_file():
            signed = True
            sign_note = "signed_by_apple_shortcuts_cli"
        else:
            sign_note = f"shortcuts_sign_failed:{(proc.stderr or proc.stdout).strip()[:200]}"

    return {
        "status": "signed" if signed else "unsigned_generated",
        "unsigned_path": str(unsigned_path),
        "unsigned_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "signed": signed,
        "signed_path": str(signed_path) if signed else None,
        "signed_sha256": hashlib.sha256(signed_path.read_bytes()).hexdigest() if signed else None,
        "note": sign_note,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    parser.add_argument("--version", default="1.2.0")
    parser.add_argument("--endpoint-url", default="http://127.0.0.1:8790/v1/inbox/drop")
    parser.add_argument("--no-sign", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    res = build_ios_shortcut(
        args.out_dir,
        version=args.version,
        endpoint_url=args.endpoint_url,
        sign_if_available=not args.no_sign,
    )
    print(json.dumps(res, indent=2) if args.json else f"Shortcut: {res['unsigned_path']} (signed={res['signed']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
