# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Cross-platform release signature & notarization verifier for Windows (.exe/.msi) and macOS (.app/.dmg)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def verify_windows_authenticode(file_path: Path, *, require_trusted_ca: bool = True) -> dict[str, Any]:
    """Inspect Windows Authenticode status, CA chain trust, and RFC-3161 timestamp via PowerShell."""
    resolved = file_path.resolve()
    if not resolved.is_file():
        return {"path": str(resolved), "platform": "windows", "ok": False, "reason": "file_not_found"}

    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        return {
            "path": str(resolved),
            "platform": "windows",
            "ok": False,
            "reason": "powershell_unavailable_on_host",
        }

    ps_code = (
        "$sig = Get-AuthenticodeSignature -LiteralPath $env:SB_VERIFY_TARGET; "
        "$out = @{ "
        "status = [string]$sig.Status; "
        "status_message = [string]$sig.StatusMessage; "
        "subject = if ($sig.SignerCertificate) { [string]$sig.SignerCertificate.Subject } else { '' }; "
        "issuer = if ($sig.SignerCertificate) { [string]$sig.SignerCertificate.Issuer } else { '' }; "
        "thumbprint = if ($sig.SignerCertificate) { [string]$sig.SignerCertificate.Thumbprint } else { '' }; "
        "not_after = if ($sig.SignerCertificate) { $sig.SignerCertificate.NotAfter.ToString('o') } else { '' }; "
        "timestamper = if ($sig.TimeStamperCertificate) { [string]$sig.TimeStamperCertificate.Subject } else { '' } "
        "}; "
        "$out | ConvertTo-Json -Compress"
    )
    env = dict(os.environ)
    env["SB_VERIFY_TARGET"] = str(resolved)
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", ps_code],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {
            "path": str(resolved),
            "platform": "windows",
            "ok": False,
            "reason": f"powershell_error:{proc.stderr.strip() or proc.returncode}",
        }
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        return {"path": str(resolved), "platform": "windows", "ok": False, "reason": f"invalid_json:{exc}"}

    status = str(info.get("status") or "")
    subject = str(info.get("subject") or "")
    issuer = str(info.get("issuer") or "")
    timestamper = str(info.get("timestamper") or "")
    is_self_signed = bool(subject and subject == issuer)

    if status == "NotSigned":
        return {**info, "path": str(resolved), "platform": "windows", "ok": False, "self_signed": False, "reason": "unsigned_binary"}
    if status == "HashMismatch":
        return {**info, "path": str(resolved), "platform": "windows", "ok": False, "self_signed": is_self_signed, "reason": "hash_mismatch"}
    if require_trusted_ca:
        if is_self_signed:
            return {
                **info,
                "path": str(resolved),
                "platform": "windows",
                "ok": False,
                "self_signed": True,
                "reason": "self_signed_certificate_not_trusted_ca",
            }
        if status != "Valid":
            return {
                **info,
                "path": str(resolved),
                "platform": "windows",
                "ok": False,
                "self_signed": False,
                "reason": f"untrusted_chain_status:{status}",
            }
        if not timestamper:
            return {
                **info,
                "path": str(resolved),
                "platform": "windows",
                "ok": False,
                "self_signed": False,
                "reason": "missing_rfc3161_timestamp",
            }
    return {
        **info,
        "path": str(resolved),
        "platform": "windows",
        "ok": True,
        "self_signed": is_self_signed,
        "reason": "trusted_authenticode_valid" if require_trusted_ca else f"signed:{status}",
    }


def verify_macos_notarization(target_path: Path, *, require_notarized: bool = True) -> dict[str, Any]:
    """Inspect macOS Developer ID signature (`codesign`), Notary ticket (`stapler`), and Gatekeeper (`spctl`)."""
    resolved = target_path.resolve()
    if not resolved.exists():
        return {"path": str(resolved), "platform": "macos", "ok": False, "reason": "target_not_found"}
    if sys.platform != "darwin":
        return {
            "path": str(resolved),
            "platform": "macos",
            "ok": False,
            "reason": "macos_host_required_for_codesign_stapler_spctl",
        }

    is_dmg = resolved.suffix.lower() == ".dmg"
    cs_cmd = ["codesign", "--verify", "--verbose=2"]
    if not is_dmg:
        cs_cmd.extend(["--deep", "--strict"])
    cs_cmd.append(str(resolved))
    cs_res = subprocess.run(cs_cmd, capture_output=True, text=True, timeout=60, check=False)
    if cs_res.returncode != 0:
        return {
            "path": str(resolved),
            "platform": "macos",
            "ok": False,
            "codesign_ok": False,
            "reason": f"codesign_failed:{(cs_res.stderr or cs_res.stdout).strip()}",
        }

    if not require_notarized:
        return {"path": str(resolved), "platform": "macos", "ok": True, "codesign_ok": True, "reason": "codesign_valid"}

    staple_res = subprocess.run(
        ["xcrun", "stapler", "validate", str(resolved)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    spctl_type = "open" if is_dmg else "execute"
    spctl_args = ["spctl", "--assess", "--type", spctl_type, "--verbose=4"]
    if is_dmg:
        spctl_args.extend(["--context", "context:primary-signature"])
    spctl_args.append(str(resolved))
    spctl_res = subprocess.run(spctl_args, capture_output=True, text=True, timeout=60, check=False)

    stapled_ok = staple_res.returncode == 0
    gatekeeper_ok = spctl_res.returncode == 0
    ok = stapled_ok and gatekeeper_ok
    return {
        "path": str(resolved),
        "platform": "macos",
        "ok": ok,
        "codesign_ok": True,
        "stapler_ok": stapled_ok,
        "gatekeeper_ok": gatekeeper_ok,
        "reason": "notarized_and_gatekeeper_accepted"
        if ok
        else f"stapler={staple_res.returncode};spctl={(spctl_res.stderr or spctl_res.stdout).strip()}",
    }


def inspect_signing_readiness_env() -> dict[str, Any]:
    """Check boolean presence of Windows OV/EV and Apple Developer ID / Notary credentials (never reads values)."""
    def _has(name: str) -> bool:
        return bool((os.environ.get(name) or "").strip())

    win_pfx = _has("SECOND_BRAIN_PFX_PATH") or _has("WINDOWS_PFX_BASE64")
    win_thumb = _has("SECOND_BRAIN_CERT_THUMBPRINT")
    win_hsm = _has("SECOND_BRAIN_CSP_NAME") and _has("SECOND_BRAIN_KEY_CONTAINER")
    win_azure = (
        _has("SECOND_BRAIN_AZURE_TRUSTED_SIGNING_ENDPOINT")
        and _has("SECOND_BRAIN_AZURE_CODE_SIGNING_ACCOUNT")
        and _has("SECOND_BRAIN_AZURE_CERT_PROFILE")
    )
    mac_cert = _has("CSC_LINK") or _has("MAC_CSC_LINK") or _has("CSC_NAME")
    mac_apple_id = _has("APPLE_ID") and _has("APPLE_APP_SPECIFIC_PASSWORD") and _has("APPLE_TEAM_ID")
    mac_api_key = (_has("APPLE_API_KEY") or _has("APPLE_API_KEY_BASE64")) and _has("APPLE_API_KEY_ID") and _has("APPLE_API_ISSUER")

    return {
        "windows_trusted_signing_configured": bool(win_pfx or win_thumb or win_hsm or win_azure),
        "windows_modes": {
            "pfx": win_pfx,
            "cert_store_thumbprint": win_thumb,
            "pkcs11_hsm": win_hsm,
            "azure_trusted_signing": win_azure,
        },
        "macos_developer_id_configured": mac_cert,
        "macos_notarization_configured": bool(mac_cert and (mac_apple_id or mac_api_key)),
        "macos_modes": {
            "developer_id_cert": mac_cert,
            "apple_id_notary": mac_apple_id,
            "app_store_connect_api_key": mac_api_key,
        },
    }


def verify_dist_directory(dist_dir: Path, *, require_trusted: bool = True) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if dist_dir.is_dir():
        for entry in sorted(dist_dir.iterdir()):
            if not entry.is_file():
                continue
            low = entry.suffix.lower()
            if low in {".exe", ".msi"} and os.name == "nt":
                results.append(verify_windows_authenticode(entry, require_trusted_ca=require_trusted))
            elif low == ".dmg" and sys.platform == "darwin":
                results.append(verify_macos_notarization(entry, require_notarized=require_trusted))
        mac_apps = sorted(dist_dir.glob("mac*/Second Brain.app"))
        if sys.platform == "darwin":
            for app in mac_apps:
                results.append(verify_macos_notarization(app, require_notarized=require_trusted))
    all_ok = bool(results) and all(bool(r.get("ok")) for r in results)
    return {
        "dist_dir": str(dist_dir.resolve()),
        "require_trusted": require_trusted,
        "checked_count": len(results),
        "all_ok": all_ok,
        "credentials_readiness": inspect_signing_readiness_env(),
        "artifacts": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default=str(ROOT / "dist"))
    parser.add_argument("--allow-self-signed", action="store_true")
    parser.add_argument("--check-env-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.check_env_only:
        env_rep = inspect_signing_readiness_env()
        print(json.dumps(env_rep, indent=2))
        return 0 if (env_rep["windows_trusted_signing_configured"] and env_rep["macos_notarization_configured"]) else 1

    report = verify_dist_directory(Path(args.dist_dir), require_trusted=not args.allow_self_signed)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Checked {report['checked_count']} installer artifact(s) in {report['dist_dir']}: all_ok={report['all_ok']}")
        for item in report["artifacts"]:
            state = "OK" if item.get("ok") else "BLOCKED"
            print(f"  [{state}] {Path(item['path']).name}: {item.get('reason')}")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
