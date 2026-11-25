# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Cross-platform release signature & notarization verifier for Windows (.exe/.msi), macOS (.app/.dmg), and Linux (.AppImage/.deb)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parents[1]


def _is_reparse_or_link(path: Path | str) -> bool:
    p = str(path)
    if os.path.islink(p):
        return True
    isj = getattr(os.path, "isjunction", None)
    if callable(isj):
        try:
            if isj(p):
                return True
        except OSError:
            pass
    if os.name == "nt":
        try:
            st = os.lstat(p)
            if bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return True
        except OSError:
            pass
    return False


def sign_manifest_ed25519(
    manifest_path: Path,
    private_key_pem: bytes,
    *,
    out_sig_path: Path | None = None,
) -> Path:
    """Create a detached Ed25519 signature (`SHA256SUMS.txt.sig`) over a release manifest."""
    resolved = manifest_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Release manifest not found: {resolved}")
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Expected an Ed25519 PEM private key.")
    sig_bytes = key.sign(resolved.read_bytes())
    target = out_sig_path or resolved.with_name(resolved.name + ".sig")
    target.write_text(base64.b64encode(sig_bytes).decode("ascii") + "\n", encoding="utf-8")
    return target


def verify_manifest_ed25519(
    manifest_path: Path,
    public_key_pem: bytes,
    *,
    sig_path: Path | None = None,
) -> dict[str, Any]:
    """Verify detached Ed25519 signature over `SHA256SUMS.txt`."""
    resolved = manifest_path.resolve()
    sig_file = (sig_path or resolved.with_name(resolved.name + ".sig")).resolve()
    if not resolved.is_file():
        return {"ok": False, "path": str(resolved), "reason": "manifest_not_found"}
    if not sig_file.is_file():
        return {"ok": False, "path": str(resolved), "sig_path": str(sig_file), "reason": "signature_not_found"}
    try:
        pub = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(pub, Ed25519PublicKey):
            return {"ok": False, "path": str(resolved), "sig_path": str(sig_file), "reason": "not_ed25519_public_key"}
        raw_sig_text = sig_file.read_text(encoding="utf-8").strip()
        sig_bytes = base64.b64decode(raw_sig_text, validate=True)
        pub.verify(sig_bytes, resolved.read_bytes())
        return {"ok": True, "path": str(resolved), "sig_path": str(sig_file), "reason": "ed25519_manifest_valid"}
    except (InvalidSignature, ValueError, OSError) as exc:
        return {"ok": False, "path": str(resolved), "sig_path": str(sig_file), "reason": f"ed25519_invalid:{exc}"}


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


def _parse_ar_members(data: bytes) -> list[tuple[str, bytes]]:
    """Parse Unix `ar` archive members from a `.deb` package."""
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("Not a valid ar archive (missing '!<arch>\\n' header).")
    members: list[tuple[str, bytes]] = []
    offset = 8
    total_len = len(data)
    while offset + 60 <= total_len:
        hdr = data[offset : offset + 60]
        if hdr[58:60] != b"`\n":
            raise ValueError(f"Malformed ar member header at offset {offset}.")
        raw_name = hdr[0:16].decode("ascii", errors="replace").strip()
        size_str = hdr[48:58].decode("ascii", errors="replace").strip()
        if not size_str.isdigit():
            raise ValueError(f"Invalid ar member size {size_str!r} for {raw_name!r}.")
        size = int(size_str)
        payload_start = offset + 60
        payload_end = payload_start + size
        if payload_end > total_len:
            raise ValueError(f"Truncated ar member {raw_name!r} at offset {offset}.")
        payload = data[payload_start:payload_end]
        if raw_name.startswith("#1/"):
            ext_len = int(raw_name[3:] or "0")
            name = payload[:ext_len].rstrip(b"\x00").decode("ascii", errors="replace")
            payload = payload[ext_len:]
        else:
            name = raw_name.rstrip("/") if raw_name != "/" else raw_name
        members.append((name, payload))
        offset = payload_end + (size % 2)
    return members


def _verify_linux_manifest_ed25519(resolved: Path, public_key_pem: bytes) -> dict[str, Any] | None:
    """Verify `resolved` digest against signed `SHA256SUMS.txt` + `SHA256SUMS.txt.sig` when present."""
    sums_path = resolved.parent / "SHA256SUMS.txt"
    sig_path = resolved.parent / "SHA256SUMS.txt.sig"
    if not sums_path.is_file() or not sig_path.is_file():
        return None
    manifest_res = verify_manifest_ed25519(sums_path, public_key_pem, sig_path=sig_path)
    if not manifest_res.get("ok"):
        return {"ok": False, "reason": f"ed25519_manifest_signature_invalid:{manifest_res.get('reason')}"}
    expected_hash: str | None = None
    for line in sums_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == resolved.name:
            expected_hash = parts[0].strip().lower()
            break
    if not expected_hash:
        return None
    actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest().lower()
    if actual_hash != expected_hash:
        return {"ok": False, "reason": "manifest_sha256_mismatch", "expected": expected_hash, "actual": actual_hash}
    return {"ok": True, "signature_type": "ed25519_manifest", "sha256": actual_hash}


def verify_linux_artifact(
    artifact_path: Path,
    *,
    require_signature: bool = True,
    public_key_pem: bytes | None = None,
    sig_path: Path | None = None,
) -> dict[str, Any]:
    """Verify Linux `.AppImage` (ELF + AppImage/SquashFS) or `.deb` (`ar` structure) and GPG/Ed25519 signature."""
    if os.path.lexists(str(artifact_path)) and _is_reparse_or_link(artifact_path):
        return {"path": str(artifact_path), "platform": "linux", "ok": False, "reason": "symlink_or_reparse_rejected"}
    resolved = artifact_path.resolve()
    if not resolved.is_file():
        return {"path": str(resolved), "platform": "linux", "ok": False, "reason": "file_not_found"}

    low = resolved.name.lower()
    if low.endswith(".appimage"):
        artifact_type = "appimage"
        try:
            with resolved.open("rb") as fh:
                head = fh.read(4 * 1024 * 1024)
        except OSError as exc:
            return {"path": str(resolved), "platform": "linux", "artifact_type": artifact_type, "ok": False, "reason": str(exc)}
        if not head.startswith(b"\x7fELF"):
            return {
                "path": str(resolved),
                "platform": "linux",
                "artifact_type": artifact_type,
                "ok": False,
                "format_ok": False,
                "reason": "invalid_appimage_elf_magic",
            }
        has_ai_magic = head[8:11] == b"AI\x02" or b"AI\x02" in head[:64]
        has_squashfs = b"hsqs" in head or b"sqsh" in head
        if not (has_ai_magic or has_squashfs):
            return {
                "path": str(resolved),
                "platform": "linux",
                "artifact_type": artifact_type,
                "ok": False,
                "format_ok": False,
                "reason": "invalid_appimage_payload_magic",
            }
        member_names: list[str] = []
    elif low.endswith(".deb"):
        artifact_type = "deb"
        try:
            members = _parse_ar_members(resolved.read_bytes())
        except (OSError, ValueError) as exc:
            return {
                "path": str(resolved),
                "platform": "linux",
                "artifact_type": artifact_type,
                "ok": False,
                "format_ok": False,
                "reason": f"invalid_deb_ar_archive: {exc}",
            }
        member_map = {name: payload for name, payload in members}
        member_names = [name for name, _ in members]
        deb_ver = member_map.get("debian-binary", b"").strip()
        has_control = any(n == "control.tar" or n.startswith("control.tar.") for n in member_names)
        has_data = any(n == "data.tar" or n.startswith("data.tar.") for n in member_names)
        if deb_ver != b"2.0" or not has_control or not has_data:
            return {
                "path": str(resolved),
                "platform": "linux",
                "artifact_type": artifact_type,
                "ok": False,
                "format_ok": False,
                "reason": "invalid_deb_structure",
            }
        for ar_name, payload in members:
            if ar_name == "control.tar" or ar_name.startswith("control.tar.") or ar_name == "data.tar" or ar_name.startswith("data.tar."):
                if ar_name.endswith(".zst"):
                    continue
                try:
                    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tf:
                        for m in tf.getmembers():
                            m_name = str(m.name or "")
                            norm_m = m_name.replace("\\", "/")
                            if (
                                m_name.startswith(("/", "\\"))
                                or (len(m_name) >= 2 and m_name[1] == ":" and m_name[0].isalpha())
                                or ".." in norm_m.split("/")
                            ):
                                return {
                                    "path": str(resolved),
                                    "platform": "linux",
                                    "artifact_type": artifact_type,
                                    "ok": False,
                                    "format_ok": False,
                                    "reason": f"unsafe_deb_tar_member:{ar_name}:{m_name}",
                                }
                except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
                    return {
                        "path": str(resolved),
                        "platform": "linux",
                        "artifact_type": artifact_type,
                        "ok": False,
                        "format_ok": False,
                        "reason": f"invalid_deb_tar_archive:{ar_name}:{exc}",
                    }
    else:
        return {
            "path": str(resolved),
            "platform": "linux",
            "ok": False,
            "reason": f"unsupported_linux_extension:{resolved.suffix}",
        }

    detached_candidates = [sig_path] if sig_path is not None else [
        resolved.with_name(resolved.name + ext) for ext in (".asc", ".sig", ".gpg")
    ]
    raw_detached = next((p for p in detached_candidates if p is not None and (os.path.lexists(str(p)) or p.is_file())), None)
    if raw_detached is not None and _is_reparse_or_link(raw_detached):
        return {
            "path": str(resolved),
            "platform": "linux",
            "artifact_type": artifact_type,
            "ok": False,
            "format_ok": True,
            "signed": True,
            "sig_path": str(raw_detached),
            "reason": "symlink_or_reparse_sig_rejected",
        }
    detached_sig = raw_detached.resolve() if (raw_detached is not None and raw_detached.is_file()) else None
    embedded_gpg_names = [n for n in member_names if n.startswith("_gpg")]

    unverified_reason = "signature_not_found"
    if detached_sig is not None:
        gpg_bin = shutil.which("gpg")
        if gpg_bin:
            cmd = [gpg_bin, "--batch", "--status-fd=1"]
            keyring_raw = (os.environ.get("SECOND_BRAIN_GPG_KEYRING") or os.environ.get("LINUX_GPG_KEYRING") or "").strip()
            expected_fpr = (
                (os.environ.get("SECOND_BRAIN_GPG_FINGERPRINT") or os.environ.get("LINUX_GPG_FINGERPRINT") or "")
                .strip()
                .upper()
                .replace(" ", "")
            )
            if keyring_raw:
                kr_path = Path(keyring_raw)
                if _is_reparse_or_link(kr_path) or not kr_path.is_file():
                    return {
                        "path": str(resolved),
                        "platform": "linux",
                        "artifact_type": artifact_type,
                        "ok": False,
                        "format_ok": True,
                        "signed": True,
                        "sig_path": str(detached_sig),
                        "reason": "invalid_gpg_keyring_path",
                    }
                cmd.extend(["--no-default-keyring", "--keyring", str(kr_path.resolve())])
            cmd.extend(["--verify", str(detached_sig), str(resolved)])
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                if expected_fpr:
                    validsig_lines = [ln for ln in (proc.stdout or "").splitlines() if "[GNUPG:] VALIDSIG " in ln]
                    if not validsig_lines or not any(expected_fpr in ln.upper() for ln in validsig_lines):
                        return {
                            "path": str(resolved),
                            "platform": "linux",
                            "artifact_type": artifact_type,
                            "ok": False,
                            "format_ok": True,
                            "signed": True,
                            "sig_path": str(detached_sig),
                            "reason": "gpg_fingerprint_mismatch",
                        }
                return {
                    "path": str(resolved),
                    "platform": "linux",
                    "artifact_type": artifact_type,
                    "ok": True,
                    "format_ok": True,
                    "signed": True,
                    "signature_type": "gpg_detached",
                    "sig_path": str(detached_sig),
                    "reason": "gpg_detached_signature_valid",
                }
            return {
                "path": str(resolved),
                "platform": "linux",
                "artifact_type": artifact_type,
                "ok": False,
                "format_ok": True,
                "signed": True,
                "sig_path": str(detached_sig),
                "reason": f"gpg_verification_failed: {(proc.stderr or proc.stdout).strip()[:300]}",
            }
        unverified_reason = "gpg_cli_not_found"

    if embedded_gpg_names and artifact_type == "deb":
        dpkg_sig_bin = shutil.which("dpkg-sig")
        if dpkg_sig_bin:
            proc = subprocess.run(
                [dpkg_sig_bin, "--verify", str(resolved)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                return {
                    "path": str(resolved),
                    "platform": "linux",
                    "artifact_type": artifact_type,
                    "ok": True,
                    "format_ok": True,
                    "signed": True,
                    "signature_type": "dpkg_sig",
                    "reason": "dpkg_sig_valid",
                }
            return {
                "path": str(resolved),
                "platform": "linux",
                "artifact_type": artifact_type,
                "ok": False,
                "format_ok": True,
                "signed": True,
                "reason": f"dpkg_sig_verification_failed: {(proc.stderr or proc.stdout).strip()[:300]}",
            }
        unverified_reason = "dpkg_sig_cli_not_found"

    if public_key_pem is not None:
        m_check = _verify_linux_manifest_ed25519(resolved, public_key_pem)
        if m_check is not None:
            if m_check["ok"]:
                return {
                    "path": str(resolved),
                    "platform": "linux",
                    "artifact_type": artifact_type,
                    "ok": True,
                    "format_ok": True,
                    "signed": True,
                    **m_check,
                    "reason": "ed25519_manifest_verified",
                }
            return {
                "path": str(resolved),
                "platform": "linux",
                "artifact_type": artifact_type,
                "ok": False,
                "format_ok": True,
                **m_check,
            }

    if not require_signature:
        return {
            "path": str(resolved),
            "platform": "linux",
            "artifact_type": artifact_type,
            "ok": True,
            "format_ok": True,
            "signed": bool(detached_sig or embedded_gpg_names),
            "reason": f"{artifact_type}_valid_unsigned" if not (detached_sig or embedded_gpg_names) else unverified_reason,
        }

    return {
        "path": str(resolved),
        "platform": "linux",
        "artifact_type": artifact_type,
        "ok": False,
        "format_ok": True,
        "signed": bool(detached_sig or embedded_gpg_names),
        "reason": unverified_reason,
    }


def inspect_signing_readiness_env() -> dict[str, Any]:
    """Check boolean presence of Windows OV/EV, Apple Developer ID / Notary, and Linux GPG credentials (never reads values)."""
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
    linux_gpg = _has("SECOND_BRAIN_GPG_PRIVATE_KEY") or _has("LINUX_GPG_PRIVATE_KEY") or _has("GPG_PRIVATE_KEY")
    linux_gpg_pub = _has("SECOND_BRAIN_GPG_PUBLIC_KEY") or _has("LINUX_GPG_PUBLIC_KEY") or _has("GPG_PUBLIC_KEY")
    ed25519_priv = _has("SECOND_BRAIN_RELEASE_ED25519_PRIVATE_KEY") or _has("RELEASE_ED25519_PRIVATE_KEY")
    ed25519_pub = _has("SECOND_BRAIN_RELEASE_ED25519_PUBLIC_KEY") or _has("RELEASE_ED25519_PUBLIC_KEY")

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
        "linux_gpg_signing_configured": bool(linux_gpg or linux_gpg_pub),
        "linux_modes": {
            "gpg_private_key": linux_gpg,
            "gpg_public_key": linux_gpg_pub,
            "ed25519_manifest": bool(ed25519_priv or ed25519_pub),
        },
        "manifest_ed25519_configured": bool(ed25519_priv or ed25519_pub),
    }


def _resolve_ed25519_key_bytes(value: str | None) -> bytes | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "-----BEGIN" in raw:
        return raw.encode("utf-8")
    cand = Path(raw)
    if os.path.lexists(str(cand)) and _is_reparse_or_link(cand):
        raise ValueError(f"Refusing symlinked Ed25519 key path: {raw}")
    if cand.is_file():
        return cand.read_bytes()
    if "/" in raw or "\\" in raw or raw.lower().endswith((".pem", ".pub", ".key")):
        raise FileNotFoundError(f"Ed25519 key file not found: {raw}")
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid Ed25519 key material: {exc}") from exc


def verify_dist_directory(
    dist_dir: Path,
    *,
    require_trusted: bool = True,
    public_key_pem: bytes | None = None,
    require_manifest_sig: bool = False,
) -> dict[str, Any]:
    if os.path.lexists(str(dist_dir)) and _is_reparse_or_link(dist_dir):
        return {
            "dist_dir": str(dist_dir),
            "require_trusted": require_trusted,
            "checked_count": 1,
            "all_ok": False,
            "credentials_readiness": inspect_signing_readiness_env(),
            "artifacts": [{"path": str(dist_dir), "platform": "dist", "ok": False, "reason": "symlink_or_reparse_rejected"}],
        }
    results: list[dict[str, Any]] = []
    if dist_dir.is_dir():
        for entry in sorted(dist_dir.iterdir()):
            if _is_reparse_or_link(entry):
                results.append({"path": str(entry), "platform": "dist", "ok": False, "reason": "symlink_or_reparse_rejected"})
                continue
            if not entry.is_file():
                continue
            low = entry.suffix.lower()
            if low in {".exe", ".msi"} and os.name == "nt":
                results.append(verify_windows_authenticode(entry, require_trusted_ca=require_trusted))
            elif low == ".dmg" and sys.platform == "darwin":
                results.append(verify_macos_notarization(entry, require_notarized=require_trusted))
            elif low in {".appimage", ".deb"}:
                results.append(
                    verify_linux_artifact(
                        entry,
                        require_signature=require_trusted,
                        public_key_pem=public_key_pem,
                    )
                )
        if os.name == "nt":
            inner_win_exe = dist_dir / "win-unpacked" / "Second Brain.exe"
            if os.path.lexists(str(inner_win_exe)) and _is_reparse_or_link(inner_win_exe):
                results.append({"path": str(inner_win_exe), "platform": "windows", "ok": False, "reason": "symlink_or_reparse_rejected"})
            elif inner_win_exe.is_file():
                results.append(verify_windows_authenticode(inner_win_exe, require_trusted_ca=require_trusted))
        mac_apps = sorted(dist_dir.glob("mac*/Second Brain.app"))
        if sys.platform == "darwin":
            for app in mac_apps:
                if _is_reparse_or_link(app):
                    results.append({"path": str(app), "platform": "macos", "ok": False, "reason": "symlink_or_reparse_rejected"})
                else:
                    results.append(verify_macos_notarization(app, require_notarized=require_trusted))
        sums_path = dist_dir / "SHA256SUMS.txt"
        sig_path = dist_dir / "SHA256SUMS.txt.sig"
        if public_key_pem is not None or require_manifest_sig:
            if _is_reparse_or_link(sums_path) or _is_reparse_or_link(sig_path):
                results.append({"path": str(sums_path), "platform": "manifest", "ok": False, "reason": "symlink_or_reparse_rejected"})
            elif not sums_path.is_file():
                results.append({"path": str(sums_path), "platform": "manifest", "ok": False, "reason": "missing_sha256sums"})
            elif public_key_pem is None:
                results.append({"path": str(sums_path), "platform": "manifest", "ok": False, "reason": "missing_ed25519_public_key"})
            else:
                m_res = verify_manifest_ed25519(sums_path, public_key_pem, sig_path=sig_path)
                results.append({"platform": "manifest", **m_res})
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
    parser.add_argument("--sign-manifest", default="", help="PEM path or base64/PEM private key env var name to sign dist/SHA256SUMS.txt.")
    parser.add_argument("--verify-manifest", default="", help="PEM path or base64/PEM public key to verify dist/SHA256SUMS.txt.sig.")
    parser.add_argument("--require-manifest-sig", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.check_env_only:
        env_rep = inspect_signing_readiness_env()
        print(json.dumps(env_rep, indent=2))
        return 0 if (env_rep["windows_trusted_signing_configured"] and env_rep["macos_notarization_configured"]) else 1

    dist_path = Path(args.dist_dir)
    if args.sign_manifest:
        priv_bytes = _resolve_ed25519_key_bytes(os.environ.get(args.sign_manifest) or args.sign_manifest)
        if not priv_bytes:
            raise SystemExit("Unable to resolve Ed25519 private key material for --sign-manifest")
        sig_out = sign_manifest_ed25519(dist_path / "SHA256SUMS.txt", priv_bytes)
        print(json.dumps({"signed_manifest": str(sig_out)}, indent=2))
        return 0

    pub_pem: bytes | None = None
    pub_raw = args.verify_manifest or os.environ.get("SECOND_BRAIN_RELEASE_ED25519_PUBLIC_KEY") or os.environ.get("RELEASE_ED25519_PUBLIC_KEY")
    if pub_raw:
        pub_pem = _resolve_ed25519_key_bytes(pub_raw)

    report = verify_dist_directory(
        dist_path,
        require_trusted=not args.allow_self_signed,
        public_key_pem=pub_pem,
        require_manifest_sig=bool(args.require_manifest_sig),
    )
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
