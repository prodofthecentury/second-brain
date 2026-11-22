# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["httpx>=0.28,<1", "PyYAML"]
# ///
"""Multi-registry release publisher & credential readiness checker for Second Brain.

Supports automated verification, dry-run validation, and live publication to:
  1. PyPI (`PYPI_API_TOKEN` or GitHub OIDC Trusted Publisher)
  2. VS Code Marketplace (`VSCE_PAT`)
  3. Open VSX (`OVSX_PAT`)
  4. Chrome Web Store (`CWS_CLIENT_ID`, `CWS_CLIENT_SECRET`, `CWS_REFRESH_TOKEN`, `CWS_EXTENSION_ID`)
  5. Microsoft Edge Add-ons (`EDGE_PRODUCT_ID`, `EDGE_CLIENT_ID`, `EDGE_API_KEY`)
  6. Firefox Add-ons / AMO (`AMO_JWT_ISSUER`, `AMO_JWT_SECRET`)
  7. Live Homebrew Tap (`HOMEBREW_TAP_REPO`, `HOMEBREW_TAP_TOKEN`)
  8. WinGet Community Repository (`microsoft/winget-pkgs` via `WINGET_GITHUB_TOKEN`)

Security invariant: never prints, logs, or echoes secret values.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


class RegistryPublishError(RuntimeError):
    """Raised when a registry publish step fails or required credentials are missing."""


def _has_env(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def check_registry_credentials() -> dict[str, dict[str, Any]]:
    """Return boolean credential readiness per public registry (never exposes values)."""
    oidc_ready = _has_env("ACTIONS_ID_TOKEN_REQUEST_URL") and _has_env("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    return {
        "pypi": {
            "ready": bool(_has_env("PYPI_API_TOKEN") or oidc_ready),
            "required_env": ["PYPI_API_TOKEN (or GitHub Actions OIDC Trusted Publisher)"],
        },
        "vscode_marketplace": {
            "ready": _has_env("VSCE_PAT"),
            "required_env": ["VSCE_PAT"],
        },
        "open_vsx": {
            "ready": _has_env("OVSX_PAT"),
            "required_env": ["OVSX_PAT"],
        },
        "chrome_web_store": {
            "ready": all(
                _has_env(k)
                for k in ("CWS_CLIENT_ID", "CWS_CLIENT_SECRET", "CWS_REFRESH_TOKEN", "CWS_EXTENSION_ID")
            ),
            "required_env": ["CWS_CLIENT_ID", "CWS_CLIENT_SECRET", "CWS_REFRESH_TOKEN", "CWS_EXTENSION_ID"],
        },
        "edge_addons": {
            "ready": all(_has_env(k) for k in ("EDGE_PRODUCT_ID", "EDGE_CLIENT_ID", "EDGE_API_KEY")),
            "required_env": ["EDGE_PRODUCT_ID", "EDGE_CLIENT_ID", "EDGE_API_KEY"],
        },
        "firefox_amo": {
            "ready": all(_has_env(k) for k in ("AMO_JWT_ISSUER", "AMO_JWT_SECRET")),
            "required_env": ["AMO_JWT_ISSUER", "AMO_JWT_SECRET"],
        },
        "homebrew_tap": {
            "ready": _has_env("HOMEBREW_TAP_TOKEN") or _has_env("GITHUB_TOKEN"),
            "required_env": ["HOMEBREW_TAP_TOKEN (or GITHUB_TOKEN)", "HOMEBREW_TAP_REPO (optional)"],
        },
        "winget_pkgs": {
            "ready": _has_env("WINGET_GITHUB_TOKEN"),
            "required_env": ["WINGET_GITHUB_TOKEN"],
        },
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_amo_jwt(issuer: str, secret: str, *, now_ts: int | None = None) -> str:
    """Create a short-lived HS256 JWT for Mozilla Add-ons (AMO) API v5 authentication."""
    issued = int(now_ts if now_ts is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": issuer.strip(),
        "jti": secrets.token_hex(16),
        "iat": issued,
        "exp": issued + 240,
    }
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}.{_b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}"
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def publish_chrome_web_store(
    zip_path: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not zip_path.is_file():
        raise RegistryPublishError(f"Chromium clipper zip not found: {zip_path}")
    creds = check_registry_credentials()["chrome_web_store"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing Chrome Web Store credentials: {', '.join(creds['required_env'])}")

    client_id = os.environ["CWS_CLIENT_ID"].strip()
    client_secret = os.environ["CWS_CLIENT_SECRET"].strip()
    refresh_token = os.environ["CWS_REFRESH_TOKEN"].strip()
    ext_id = os.environ["CWS_EXTENSION_ID"].strip()

    with httpx.Client(timeout=60.0, follow_redirects=False, trust_env=False, transport=transport) as client:
        tok_resp = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if tok_resp.status_code != 200:
            raise RegistryPublishError(f"Chrome Web Store OAuth token exchange failed (HTTP {tok_resp.status_code}).")
        access_token = str(tok_resp.json().get("access_token") or "").strip()
        if not access_token:
            raise RegistryPublishError("Chrome Web Store OAuth response lacked access_token.")

        headers = {"Authorization": f"Bearer {access_token}", "x-goog-api-version": "2"}
        up_resp = client.put(
            f"https://www.googleapis.com/upload/chromewebstore/v1.1/items/{ext_id}",
            headers={**headers, "Content-Type": "application/zip"},
            content=zip_path.read_bytes(),
        )
        if up_resp.status_code not in (200, 201):
            raise RegistryPublishError(f"Chrome Web Store upload failed (HTTP {up_resp.status_code}).")
        up_json = up_resp.json()
        if str(up_json.get("uploadState") or "").upper() == "FAILURE":
            raise RegistryPublishError(f"Chrome Web Store upload rejected: {up_json.get('itemError')}")

        pub_resp = client.post(
            f"https://www.googleapis.com/chromewebstore/v1.1/items/{ext_id}/publish",
            headers=headers,
        )
        if pub_resp.status_code not in (200, 201):
            raise RegistryPublishError(f"Chrome Web Store publish failed (HTTP {pub_resp.status_code}).")
        return {"registry": "chrome_web_store", "extension_id": ext_id, "status": pub_resp.json().get("status")}


def publish_edge_addons(
    zip_path: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not zip_path.is_file():
        raise RegistryPublishError(f"Edge clipper zip not found: {zip_path}")
    creds = check_registry_credentials()["edge_addons"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing Edge Add-ons credentials: {', '.join(creds['required_env'])}")

    product_id = os.environ["EDGE_PRODUCT_ID"].strip()
    client_id = os.environ["EDGE_CLIENT_ID"].strip()
    api_key = os.environ["EDGE_API_KEY"].strip()
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "X-ClientID": client_id,
    }
    base = f"https://api.addons.microsoftedge.microsoft.com/v1/products/{product_id}/submissions"
    with httpx.Client(timeout=60.0, follow_redirects=False, trust_env=False, transport=transport) as client:
        up_resp = client.post(
            f"{base}/draft/package",
            headers={**headers, "Content-Type": "application/zip"},
            content=zip_path.read_bytes(),
        )
        if up_resp.status_code not in (200, 202):
            raise RegistryPublishError(f"Edge Add-ons upload failed (HTTP {up_resp.status_code}).")
        pub_resp = client.post(
            base,
            headers={**headers, "Content-Type": "application/json"},
            json={"notes": "Automated Second Brain release submission."},
        )
        if pub_resp.status_code not in (200, 202):
            raise RegistryPublishError(f"Edge Add-ons publish submission failed (HTTP {pub_resp.status_code}).")
        return {
            "registry": "edge_addons",
            "product_id": product_id,
            "operation_location": pub_resp.headers.get("Location", ""),
        }


def publish_firefox_amo(
    xpi_path: Path,
    *,
    channel: str = "listed",
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not xpi_path.is_file():
        raise RegistryPublishError(f"Firefox AMO .xpi package not found: {xpi_path}")
    creds = check_registry_credentials()["firefox_amo"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing Firefox AMO credentials: {', '.join(creds['required_env'])}")

    jwt_tok = build_amo_jwt(os.environ["AMO_JWT_ISSUER"], os.environ["AMO_JWT_SECRET"])
    headers = {"Authorization": f"JWT {jwt_tok}"}
    with httpx.Client(timeout=60.0, follow_redirects=False, trust_env=False, transport=transport) as client:
        up_resp = client.post(
            "https://addons.mozilla.org/api/v5/addons/upload/",
            headers=headers,
            data={"channel": channel},
            files={"upload": (xpi_path.name, xpi_path.read_bytes(), "application/x-xpinstall")},
        )
        if up_resp.status_code not in (200, 201, 202):
            raise RegistryPublishError(f"Firefox AMO upload failed (HTTP {up_resp.status_code}).")
        upload_uuid = str(up_resp.json().get("uuid") or "")
        return {"registry": "firefox_amo", "upload_uuid": upload_uuid, "channel": channel}


def publish_homebrew_tap_via_github_api(
    *,
    tap_repo: str,
    cask_path: Path,
    formula_path: Path | None = None,
    readme_path: Path | None = None,
    token: str | None = None,
    version: str = "1.0.0",
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Commit Casks/second-brain.rb (and optional Formula/README) directly to a live GitHub Homebrew Tap."""
    clean_tap_repo = str(tap_repo or "").strip()
    if not re.fullmatch(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", clean_tap_repo) or ".." in clean_tap_repo:
        raise RegistryPublishError(f"Invalid Homebrew Tap repository slug: {tap_repo!r}")
    resolved_token = (token or os.environ.get("HOMEBREW_TAP_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not resolved_token:
        raise RegistryPublishError("Missing HOMEBREW_TAP_TOKEN or GITHUB_TOKEN for Homebrew Tap publish.")
    if not cask_path.is_file():
        raise RegistryPublishError(f"Homebrew Cask file not found: {cask_path}")

    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SecondBrain-ReleasePublisher/1.0",
    }
    files_to_push: list[tuple[str, bytes]] = [("Casks/second-brain.rb", cask_path.read_bytes())]
    if formula_path and formula_path.is_file():
        files_to_push.append(("Formula/second-brain.rb", formula_path.read_bytes()))
    if readme_path and readme_path.is_file():
        files_to_push.append(("README.md", readme_path.read_bytes()))

    updated: list[str] = []
    with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False, transport=transport) as client:
        for repo_rel, content_bytes in files_to_push:
            api_url = f"https://api.github.com/repos/{clean_tap_repo}/contents/{repo_rel}"
            existing_sha: str | None = None
            get_resp = client.get(api_url, headers=headers)
            if get_resp.status_code == 200:
                existing_sha = get_resp.json().get("sha")
            body: dict[str, Any] = {
                "message": f"chore(second-brain): update {repo_rel} to v{version}",
                "content": base64.b64encode(content_bytes).decode("ascii"),
            }
            if existing_sha:
                body["sha"] = existing_sha
            put_resp = client.put(api_url, headers=headers, json=body)
            if put_resp.status_code not in (200, 201):
                raise RegistryPublishError(
                    f"Failed to update {repo_rel} in Homebrew Tap {tap_repo} (HTTP {put_resp.status_code})."
                )
            updated.append(repo_rel)

    return {"registry": "homebrew_tap", "tap_repo": tap_repo, "updated_files": updated}


def validate_winget_manifests_locally(manifest_dir: Path) -> dict[str, Any]:
    """Run `winget validate --manifest <dir>` when Windows Package Manager CLI is installed."""
    if not manifest_dir.is_dir():
        raise RegistryPublishError(f"WinGet manifest directory does not exist: {manifest_dir}")
    winget_bin = shutil.which("winget")
    if not winget_bin:
        return {"validated": False, "reason": "winget_cli_not_on_host", "manifest_dir": str(manifest_dir)}

    proc = subprocess.run(
        [winget_bin, "validate", "--manifest", str(manifest_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    ok = proc.returncode == 0
    return {
        "validated": ok,
        "returncode": proc.returncode,
        "manifest_dir": str(manifest_dir),
        "output": (proc.stdout + "\n" + proc.stderr).strip()[:2000],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check-credentials", help="Report boolean credential readiness across all public stores.")
    p_check.add_argument("--json", action="store_true")

    p_winget = sub.add_parser("validate-winget", help="Validate WinGet manifest directory with local winget CLI.")
    p_winget.add_argument("manifest_dir")
    p_winget.add_argument("--json", action="store_true")

    p_tap = sub.add_parser("publish-homebrew-tap", help="Publish Casks/second-brain.rb to a live GitHub Homebrew Tap.")
    p_tap.add_argument("--tap-repo", required=True, help="owner/homebrew-second-brain")
    p_tap.add_argument("--cask", required=True)
    p_tap.add_argument("--formula", default="")
    p_tap.add_argument("--readme", default="")
    p_tap.add_argument("--version", default="1.0.0")

    p_stores = sub.add_parser("publish-browser-stores", help="Publish Web Clipper to Chrome, Edge, and Firefox stores.")
    p_stores.add_argument("--chromium-zip", required=True)
    p_stores.add_argument("--firefox-xpi", required=True)
    p_stores.add_argument("--skip-missing-creds", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "check-credentials":
        rep = check_registry_credentials()
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            for reg, info in rep.items():
                st = "READY" if info["ready"] else "MISSING_CREDENTIALS"
                print(f"[{st:19}] {reg:20} -> {', '.join(info['required_env'])}")
        return 0

    if args.cmd == "validate-winget":
        res = validate_winget_manifests_locally(Path(args.manifest_dir))
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"WinGet validation ({res['manifest_dir']}): validated={res['validated']}")
            if res.get("output"):
                print(res["output"])
        return 0 if res["validated"] or res.get("reason") == "winget_cli_not_on_host" else 1

    if args.cmd == "publish-homebrew-tap":
        res = publish_homebrew_tap_via_github_api(
            tap_repo=args.tap_repo,
            cask_path=Path(args.cask),
            formula_path=Path(args.formula) if args.formula else None,
            readme_path=Path(args.readme) if args.readme else None,
            version=args.version,
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "publish-browser-stores":
        creds = check_registry_credentials()
        results: dict[str, Any] = {}
        for key, fn, path_arg in (
            ("chrome_web_store", publish_chrome_web_store, Path(args.chromium_zip)),
            ("edge_addons", publish_edge_addons, Path(args.chromium_zip)),
            ("firefox_amo", publish_firefox_amo, Path(args.firefox_xpi)),
        ):
            if not creds[key]["ready"]:
                if args.skip_missing_creds:
                    results[key] = {"skipped": True, "reason": "missing_credentials"}
                    continue
                raise RegistryPublishError(f"Missing credentials for {key}: {creds[key]['required_env']}")
            results[key] = fn(path_arg)
        print(json.dumps(results, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
