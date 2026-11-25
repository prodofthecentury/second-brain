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
  9. GitHub Release asset verification & upload (`GITHUB_TOKEN` or `GH_TOKEN`)

Security invariant: never prints, logs, or echoes secret values.
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
import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
import urllib.error
import urllib.request
import zipfile

try:
    import httpx  # type: ignore[import-not-found]
except ImportError:
    httpx = None  # type: ignore[assignment]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _CaseInsensitiveHeaders(dict):
    """Minimal case-insensitive HTTP header mapping matching httpx.Headers.get()."""

    def __init__(self, items: dict[str, str] | None = None) -> None:
        super().__init__()
        self._lower_map: dict[str, str] = {}
        for k, v in (items or {}).items():
            sk, sv = str(k), str(v)
            super().__setitem__(sk, sv)
            self._lower_map[sk.lower()] = sv

    def get(self, key: str, default: Any = None) -> Any:
        return self._lower_map.get(str(key).lower(), default)

    def __getitem__(self, key: str) -> str:
        lk = str(key).lower()
        if lk in self._lower_map:
            return self._lower_map[lk]
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._lower_map


class _StdlibHttpResponse:
    """Minimal httpx.Response-compatible wrapper backed by urllib.request."""

    def __init__(self, status_code: int, headers: dict[str, str], content: bytes) -> None:
        self.status_code = int(status_code)
        self.headers = _CaseInsensitiveHeaders(headers)
        self.content = bytes(content)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class _StdlibHttpClient:
    """Zero-dependency httpx.Client-compatible fallback for minimal `--no-dev` CI runs."""

    def __init__(self, *, timeout: float = 60.0) -> None:
        self._timeout = float(timeout)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def __enter__(self) -> _StdlibHttpClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def _encode_multipart(
        self,
        data: dict[str, Any] | None,
        files: dict[str, tuple[str, bytes, str]],
    ) -> tuple[bytes, str]:
        boundary = f"----SecondBrainPublish{secrets.token_hex(12)}"
        chunks: list[bytes] = []
        if isinstance(data, dict):
            for key, value in data.items():
                chunks.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n".encode("utf-8")
                )
        for field_name, file_spec in files.items():
            filename, payload, content_type = file_spec
            chunks.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
            )
            chunks.append(bytes(payload))
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        content: bytes | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> _StdlibHttpResponse:
        target_url = url
        if params:
            sep = "&" if "?" in target_url else "?"
            target_url = f"{target_url}{sep}{urlencode(params)}"
        req_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        body_bytes: bytes | None = None
        if files is not None:
            body_bytes, content_type = self._encode_multipart(data if isinstance(data, dict) else None, files)
            req_headers["Content-Type"] = content_type
        elif json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif content is not None:
            body_bytes = bytes(content)
        elif data is not None:
            if isinstance(data, (bytes, bytearray)):
                body_bytes = bytes(data)
            elif isinstance(data, dict):
                body_bytes = urlencode(data).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            else:
                body_bytes = str(data).encode("utf-8")

        req = urllib.request.Request(target_url, data=body_bytes, headers=req_headers, method=method.upper())
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return _StdlibHttpResponse(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as exc:
            err_headers = dict(exc.headers.items()) if exc.headers else {}
            err_body = exc.read() if exc.fp is not None else b""
            return _StdlibHttpResponse(exc.code, err_headers, err_body)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _StdlibHttpResponse:
        return self.request("GET", url, headers=headers, params=params)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        content: bytes | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> _StdlibHttpResponse:
        return self.request(
            "POST",
            url,
            headers=headers,
            params=params,
            json_body=json,
            data=data,
            content=content,
            files=files,
        )

    def put(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        content: bytes | None = None,
    ) -> _StdlibHttpResponse:
        return self.request(
            "PUT",
            url,
            headers=headers,
            params=params,
            json_body=json,
            data=data,
            content=content,
        )

    def delete(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _StdlibHttpResponse:
        return self.request("DELETE", url, headers=headers, params=params)


def _open_http_client(*, timeout: float = 60.0, transport: Any = None) -> Any:
    """Use httpx.Client when installed (supporting mock transports), else stdlib urllib."""
    if httpx is not None:
        return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport)
    if transport is not None:
        raise RegistryPublishError("Custom httpx transport requires the optional 'httpx' package.")
    return _StdlibHttpClient(timeout=timeout)

ROOT = Path(__file__).resolve().parents[1]

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_AMO_UPLOAD_UUID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_STORE_ID_RE = re.compile(r"^[A-Za-z0-9._:@{}-]+$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA256SUMS_LINE_RE = re.compile(r"^([0-9a-fA-F]{64}) [ *]([^\r\n/\\]+)$")
_ZERO_SHA256 = "0" * 64
_SAFE_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_BOGUS_RELEASE_ASSETS: frozenset[str] = frozenset({".gitignore", "default.gitignore", "builder-debug.yml", "builder-effective-config.yaml"})
_ALLOWED_UNHASHED_DIST_FILES: frozenset[str] = frozenset({"SHA256SUMS.txt", "SHA256SUMS.txt.sig", ".gitignore"})
_RELEASE_ASSET_SUFFIXES: tuple[str, ...] = (
    ".exe",
    ".msi",
    ".whl",
    ".tar.gz",
    ".vsix",
    ".zip",
    ".xpi",
    ".dmg",
    ".appimage",
    ".deb",
    ".blockmap",
    ".rb",
    ".yaml",
    ".yml",
)
_FORBIDDEN_WHEEL_TEST_BASENAMES: frozenset[str] = frozenset(
    {
        "_hatch_build.py",
        "test_all.py",
        "smoke_test.py",
        "run_desktop_tests.py",
        "run_vscode_tests.py",
    }
)
_FORBIDDEN_UNPACKED_SCRIPT_BASENAMES: frozenset[str] = frozenset(
    {
        "test_all.py",
        "smoke_test.py",
        "run_desktop_tests.py",
        "run_vscode_tests.py",
    }
)
_FORBIDDEN_ARCHIVE_SECRET_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".jks",
    ".keystore",
    ".p8",
    ".sbvault",
)


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


class RegistryPublishError(RuntimeError):
    """Raised when a registry publish step fails or required credentials are missing."""


def _has_env(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _validate_store_id(value: str, *, label: str) -> str:
    clean = str(value or "").strip()
    if not clean or ".." in clean or "/" in clean or "\\" in clean or not _SAFE_STORE_ID_RE.fullmatch(clean):
        raise RegistryPublishError(f"Invalid {label}: {value!r}")
    return clean


def _validate_zip_member_safety(zf: zipfile.ZipFile, *, pkg_name: str, label: str) -> set[str]:
    names: set[str] = set()
    for info in zf.infolist():
        mname = str(info.filename or "")
        norm = mname.replace("\\", "/")
        if (
            mname.startswith(("/", "\\"))
            or "\\" in mname
            or (len(mname) >= 2 and mname[1] == ":" and mname[0].isalpha())
            or ".." in norm.split("/")
        ):
            raise RegistryPublishError(f"Unsafe path entry inside {label} package {pkg_name}: {mname!r}")
        if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
            raise RegistryPublishError(f"Refusing symlink inside {label} package {pkg_name}: {mname!r}")
        names.add(mname)
    return names


def _validate_repo_slug(slug: str, *, label: str = "repository") -> str:
    clean = str(slug or "").strip()
    if not _REPO_SLUG_RE.fullmatch(clean) or ".." in clean:
        raise RegistryPublishError(f"Invalid GitHub {label} slug: {slug!r}")
    owner, repo = clean.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."}:
        raise RegistryPublishError(f"Invalid GitHub {label} slug: {slug!r}")
    return clean


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SecondBrain-ReleasePublisher/1.0",
    }


def check_registry_credentials() -> dict[str, dict[str, Any]]:
    """Return boolean credential readiness per public registry (never exposes values)."""
    oidc_ready = _has_env("ACTIONS_ID_TOKEN_REQUEST_URL") and _has_env("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    return {
        "github_release": {
            "ready": _has_env("GITHUB_TOKEN") or _has_env("GH_TOKEN"),
            "required_env": ["GITHUB_TOKEN (or GH_TOKEN)"],
        },
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
            "required_env": [
                "CWS_CLIENT_ID",
                "CWS_CLIENT_SECRET",
                "CWS_REFRESH_TOKEN",
                "CWS_EXTENSION_ID",
                "CWS_PUBLISHER_ID (recommended for CWS API v2)",
            ],
        },
        "edge_addons": {
            "ready": all(_has_env(k) for k in ("EDGE_PRODUCT_ID", "EDGE_CLIENT_ID", "EDGE_API_KEY")),
            "required_env": ["EDGE_PRODUCT_ID", "EDGE_CLIENT_ID", "EDGE_API_KEY"],
        },
        "firefox_amo": {
            "ready": all(_has_env(k) for k in ("AMO_JWT_ISSUER", "AMO_JWT_SECRET")),
            "required_env": ["AMO_JWT_ISSUER", "AMO_JWT_SECRET", "AMO_ADDON_SLUG (recommended for version creation)"],
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


def _discover_dist_artifacts(dist_dir: Path) -> dict[str, list[str]]:
    """Inventory release artifacts inside `dist_dir` by package ecosystem."""
    found: dict[str, list[str]] = {
        "python_wheels": [],
        "python_sdists": [],
        "vscode_vsix": [],
        "windows_installers": [],
        "macos_installers": [],
        "linux_appimages": [],
        "linux_debs": [],
        "browser_extensions": [],
        "winget_manifests": [],
        "homebrew_recipes": [],
    }
    if not dist_dir.is_dir():
        return found
    for entry in sorted(dist_dir.iterdir()):
        if not entry.is_file():
            continue
        name_low = entry.name.lower()
        if name_low.endswith(".whl"):
            found["python_wheels"].append(entry.name)
        elif name_low.endswith(".tar.gz"):
            found["python_sdists"].append(entry.name)
        elif name_low.endswith(".vsix"):
            found["vscode_vsix"].append(entry.name)
        elif name_low.endswith((".exe", ".msi")):
            found["windows_installers"].append(entry.name)
        elif name_low.endswith(".dmg"):
            found["macos_installers"].append(entry.name)
        elif name_low.endswith(".appimage"):
            found["linux_appimages"].append(entry.name)
        elif name_low.endswith(".deb"):
            found["linux_debs"].append(entry.name)
        elif name_low.endswith(".xpi") or ("clipper" in name_low and name_low.endswith(".zip")):
            found["browser_extensions"].append(entry.name)
        elif name_low.endswith(".rb"):
            found["homebrew_recipes"].append(entry.name)
        elif name_low == "secondbrain.secondbrain.yaml" or name_low.endswith(".installer.yaml"):
            found["winget_manifests"].append(entry.name)
    for wg_file in sorted(dist_dir.glob("manifests/**/SecondBrain.SecondBrain*.yaml")):
        rel = str(wg_file.relative_to(dist_dir)).replace("\\", "/")
        if rel not in found["winget_manifests"]:
            found["winget_manifests"].append(rel)
    return found


def get_registry_publish_status(dist_dir: Path = ROOT / "dist") -> dict[str, Any]:
    """Return combined credential readiness and discovered `dist/` artifacts across all registries."""
    creds = check_registry_credentials()
    artifacts = _discover_dist_artifacts(dist_dir)
    artifact_map: dict[str, list[str]] = {
        "github_release": [
            *artifacts["windows_installers"],
            *artifacts["macos_installers"],
            *artifacts["linux_appimages"],
            *artifacts["linux_debs"],
            *artifacts["python_wheels"],
            *artifacts["python_sdists"],
        ],
        "pypi": [*artifacts["python_wheels"], *artifacts["python_sdists"]],
        "vscode_marketplace": list(artifacts["vscode_vsix"]),
        "open_vsx": list(artifacts["vscode_vsix"]),
        "chrome_web_store": [a for a in artifacts["browser_extensions"] if not a.lower().endswith(".xpi")],
        "edge_addons": [a for a in artifacts["browser_extensions"] if not a.lower().endswith(".xpi")],
        "firefox_amo": [a for a in artifacts["browser_extensions"] if a.lower().endswith(".xpi")],
        "homebrew_tap": list(artifacts["homebrew_recipes"]),
        "winget_pkgs": list(artifacts["winget_manifests"]),
    }
    registries: dict[str, dict[str, Any]] = {}
    for reg_name, cred_info in creds.items():
        reg_artifacts = artifact_map.get(reg_name, [])
        registries[reg_name] = {
            **cred_info,
            "artifacts": reg_artifacts,
            "artifact_count": len(reg_artifacts),
            "publishable": bool(cred_info["ready"] and reg_artifacts),
        }
    return {
        "dist_dir": str(dist_dir),
        "dist_exists": dist_dir.is_dir(),
        "registries": registries,
        "artifacts": artifacts,
        "ready_count": sum(1 for v in creds.values() if v["ready"]),
        "total_registries": len(creds),
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


def _assert_zip_contains_manifest(pkg_path: Path, *, required_member: str = "manifest.json", label: str = "archive") -> None:
    try:
        with zipfile.ZipFile(pkg_path, "r") as zf:
            names = _validate_zip_member_safety(zf, pkg_name=pkg_path.name, label=label)
    except (zipfile.BadZipFile, OSError) as exc:
        raise RegistryPublishError(f"Invalid {label} package {pkg_path}: {exc}") from exc
    if required_member not in names:
        raise RegistryPublishError(f"{label} package {pkg_path.name} missing {required_member}")


def _extract_amo_slug_from_xpi(xpi_path: Path) -> str:
    """Read Gecko add-on ID from `.xpi` manifest.json for AMO `/versions/` creation."""
    try:
        with zipfile.ZipFile(xpi_path, "r") as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile):
        return ""
    if not isinstance(manifest, dict):
        return ""
    for root_key in ("browser_specific_settings", "applications"):
        section = manifest.get(root_key)
        if isinstance(section, dict):
            gecko = section.get("gecko")
            if isinstance(gecko, dict):
                addon_id = str(gecko.get("id") or "").strip()
                if addon_id:
                    return addon_id
    return ""


def publish_chrome_web_store(
    zip_path: Path,
    *,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not zip_path.is_file():
        raise RegistryPublishError(f"Chromium clipper zip not found: {zip_path}")
    _assert_zip_contains_manifest(zip_path, required_member="manifest.json", label="Chromium clipper")
    creds = check_registry_credentials()["chrome_web_store"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing Chrome Web Store credentials: {', '.join(creds['required_env'])}")

    client_id = os.environ["CWS_CLIENT_ID"].strip()
    client_secret = os.environ["CWS_CLIENT_SECRET"].strip()
    refresh_token = os.environ["CWS_REFRESH_TOKEN"].strip()
    ext_id = _validate_store_id(os.environ["CWS_EXTENSION_ID"], label="CWS_EXTENSION_ID")
    raw_pub = (os.environ.get("CWS_PUBLISHER_ID") or "").strip()
    publisher_id = _validate_store_id(raw_pub, label="CWS_PUBLISHER_ID") if raw_pub else ""
    q_ext = quote(ext_id, safe="")
    q_pub = quote(publisher_id, safe="") if publisher_id else ""
    api_version = "v2" if publisher_id else "v1.1"

    if dry_run:
        return {
            "registry": "chrome_web_store",
            "dry_run": True,
            "api_version": api_version,
            "publisher_id": publisher_id or None,
            "extension_id": ext_id,
            "package": zip_path.name,
        }

    with _open_http_client(timeout=60.0, transport=transport) as client:
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

        raw_zip = zip_path.read_bytes()
        if publisher_id:
            bearer_headers = {"Authorization": f"Bearer {access_token}"}
            up_resp = client.post(
                f"https://chromewebstore.googleapis.com/upload/v2/publishers/{q_pub}/items/{q_ext}:upload",
                headers={**bearer_headers, "Content-Type": "application/zip"},
                content=raw_zip,
            )
            if up_resp.status_code not in (200, 201):
                raise RegistryPublishError(f"Chrome Web Store v2 upload failed (HTTP {up_resp.status_code}).")
            up_json = up_resp.json()
            if str(up_json.get("uploadState") or "").upper() in ("FAILURE", "FAILED"):
                raise RegistryPublishError(f"Chrome Web Store v2 upload rejected: {up_json}")

            pub_resp = client.post(
                f"https://chromewebstore.googleapis.com/v2/publishers/{q_pub}/items/{q_ext}:publish",
                headers={**bearer_headers, "Content-Type": "application/json"},
                json={},
            )
            if pub_resp.status_code not in (200, 201):
                raise RegistryPublishError(f"Chrome Web Store v2 publish failed (HTTP {pub_resp.status_code}).")
            pub_json = pub_resp.json()
            return {
                "registry": "chrome_web_store",
                "api_version": "v2",
                "publisher_id": publisher_id,
                "extension_id": ext_id,
                "status": pub_json.get("state") or pub_json.get("status"),
            }

        headers = {"Authorization": f"Bearer {access_token}", "x-goog-api-version": "2"}
        up_resp = client.put(
            f"https://www.googleapis.com/upload/chromewebstore/v1.1/items/{q_ext}",
            headers={**headers, "Content-Type": "application/zip"},
            content=raw_zip,
        )
        if up_resp.status_code not in (200, 201):
            raise RegistryPublishError(f"Chrome Web Store upload failed (HTTP {up_resp.status_code}).")
        up_json = up_resp.json()
        if str(up_json.get("uploadState") or "").upper() == "FAILURE":
            raise RegistryPublishError(f"Chrome Web Store upload rejected: {up_json.get('itemError')}")

        pub_resp = client.post(
            f"https://www.googleapis.com/chromewebstore/v1.1/items/{q_ext}/publish",
            headers=headers,
        )
        if pub_resp.status_code not in (200, 201):
            raise RegistryPublishError(f"Chrome Web Store publish failed (HTTP {pub_resp.status_code}).")
        return {
            "registry": "chrome_web_store",
            "api_version": "v1.1",
            "extension_id": ext_id,
            "status": pub_resp.json().get("status"),
        }


def _poll_edge_operation(
    client: httpx.Client,
    location: str,
    headers: dict[str, str],
    *,
    poll_interval: float = 2.0,
    max_polls: int = 15,
    sleep_fn: Any = time.sleep,
) -> str:
    if not location:
        return "Accepted"
    parsed = urlsplit(location)
    try:
        loc_port = parsed.port
    except ValueError as exc:
        raise RegistryPublishError(f"Untrusted Edge Add-ons operation Location URL port: {location!r}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.addons.microsoftedge.microsoft.com"
        or loc_port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RegistryPublishError(f"Untrusted Edge Add-ons operation Location URL: {location!r}")
    for idx in range(max_polls):
        if idx > 0 and poll_interval > 0:
            sleep_fn(poll_interval)
        resp = client.get(location, headers=headers)
        if resp.status_code not in (200, 202):
            raise RegistryPublishError(f"Edge Add-ons operation poll failed (HTTP {resp.status_code}).")
        data = resp.json() if resp.content else {}
        status = str(data.get("status") or "").strip()
        if status in ("Succeeded", "Failed"):
            if status == "Failed":
                raise RegistryPublishError(f"Edge Add-ons operation failed: {data}")
            return status
    raise RegistryPublishError(f"Edge Add-ons operation timed out after {max_polls} polls: {location}")


def publish_edge_addons(
    zip_path: Path,
    *,
    dry_run: bool = False,
    poll_interval: float = 2.0,
    max_polls: int = 15,
    sleep_fn: Any = time.sleep,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not zip_path.is_file():
        raise RegistryPublishError(f"Edge clipper zip not found: {zip_path}")
    _assert_zip_contains_manifest(zip_path, required_member="manifest.json", label="Edge clipper")
    creds = check_registry_credentials()["edge_addons"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing Edge Add-ons credentials: {', '.join(creds['required_env'])}")

    product_id = _validate_store_id(os.environ["EDGE_PRODUCT_ID"], label="EDGE_PRODUCT_ID")
    client_id = os.environ["EDGE_CLIENT_ID"].strip()
    api_key = os.environ["EDGE_API_KEY"].strip()
    if dry_run:
        return {
            "registry": "edge_addons",
            "dry_run": True,
            "product_id": product_id,
            "package": zip_path.name,
        }

    headers = {
        "Authorization": f"ApiKey {api_key}",
        "X-ClientID": client_id,
    }
    base = f"https://api.addons.microsoftedge.microsoft.com/v1/products/{quote(product_id, safe='')}/submissions"
    with _open_http_client(timeout=60.0, transport=transport) as client:
        up_resp = client.post(
            f"{base}/draft/package",
            headers={**headers, "Content-Type": "application/zip"},
            content=zip_path.read_bytes(),
        )
        if up_resp.status_code not in (200, 202):
            raise RegistryPublishError(f"Edge Add-ons upload failed (HTTP {up_resp.status_code}).")
        up_loc = up_resp.headers.get("Location", "")
        _poll_edge_operation(client, up_loc, headers, poll_interval=poll_interval, max_polls=max_polls, sleep_fn=sleep_fn)

        pub_resp = client.post(
            base,
            headers={**headers, "Content-Type": "application/json"},
            json={"notes": "Automated Second Brain release submission."},
        )
        if pub_resp.status_code not in (200, 202):
            raise RegistryPublishError(f"Edge Add-ons publish submission failed (HTTP {pub_resp.status_code}).")
        pub_loc = pub_resp.headers.get("Location", "")
        pub_status = _poll_edge_operation(
            client, pub_loc, headers, poll_interval=poll_interval, max_polls=max_polls, sleep_fn=sleep_fn
        )
        return {
            "registry": "edge_addons",
            "product_id": product_id,
            "operation_location": pub_loc,
            "publish_status": pub_status,
        }


def publish_firefox_amo(
    xpi_path: Path,
    *,
    channel: str = "listed",
    addon_slug: str | None = None,
    dry_run: bool = False,
    poll_interval: float = 2.0,
    max_polls: int = 20,
    sleep_fn: Any = time.sleep,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not xpi_path.is_file():
        raise RegistryPublishError(f"Firefox AMO .xpi package not found: {xpi_path}")
    _assert_zip_contains_manifest(xpi_path, required_member="manifest.json", label="Firefox AMO .xpi")
    creds = check_registry_credentials()["firefox_amo"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing Firefox AMO credentials: {', '.join(creds['required_env'])}")

    raw_slug = (addon_slug or os.environ.get("AMO_ADDON_SLUG") or _extract_amo_slug_from_xpi(xpi_path) or "").strip()
    if not raw_slug:
        raise RegistryPublishError(
            "Firefox AMO addon slug is required via --addon-slug, AMO_ADDON_SLUG, or XPI manifest gecko.id."
        )
    resolved_slug = _validate_store_id(raw_slug, label="Firefox AMO addon slug")
    if dry_run:
        return {
            "registry": "firefox_amo",
            "dry_run": True,
            "channel": channel,
            "addon_slug": resolved_slug,
            "package": xpi_path.name,
        }

    jwt_tok = build_amo_jwt(os.environ["AMO_JWT_ISSUER"], os.environ["AMO_JWT_SECRET"])
    headers = {"Authorization": f"JWT {jwt_tok}"}
    with _open_http_client(timeout=60.0, transport=transport) as client:
        up_resp = client.post(
            "https://addons.mozilla.org/api/v5/addons/upload/",
            headers=headers,
            data={"channel": channel},
            files={"upload": (xpi_path.name, xpi_path.read_bytes(), "application/x-xpinstall")},
        )
        if up_resp.status_code not in (200, 201, 202):
            raise RegistryPublishError(f"Firefox AMO upload failed (HTTP {up_resp.status_code}).")
        up_json = up_resp.json()
        upload_uuid = str(up_json.get("uuid") or "").strip()
        if not upload_uuid or not _AMO_UPLOAD_UUID_RE.fullmatch(upload_uuid):
            raise RegistryPublishError(f"Firefox AMO upload response returned invalid upload UUID: {upload_uuid!r}")

        validated = bool(up_json.get("processed") and up_json.get("valid"))
        if not up_json.get("processed"):
            for idx in range(max_polls):
                if idx > 0 and poll_interval > 0:
                    sleep_fn(poll_interval)
                fresh_tok = build_amo_jwt(os.environ["AMO_JWT_ISSUER"], os.environ["AMO_JWT_SECRET"])
                poll_resp = client.get(
                    f"https://addons.mozilla.org/api/v5/addons/upload/{quote(upload_uuid, safe='')}/",
                    headers={"Authorization": f"JWT {fresh_tok}"},
                )
                if poll_resp.status_code != 200:
                    raise RegistryPublishError(
                        f"Firefox AMO upload status poll failed (HTTP {poll_resp.status_code})."
                    )
                poll_json = poll_resp.json()
                if poll_json.get("processed"):
                    if not poll_json.get("valid"):
                        raise RegistryPublishError(
                            f"Firefox AMO upload failed validation: {poll_json.get('validation') or poll_json}"
                        )
                    validated = True
                    break
            if not validated:
                raise RegistryPublishError(f"Firefox AMO upload {upload_uuid} timed out waiting for validation.")
        elif not up_json.get("valid"):
            raise RegistryPublishError(f"Firefox AMO upload failed validation: {up_json.get('validation') or up_json}")

        version_id = None
        if resolved_slug:
            fresh_tok = build_amo_jwt(os.environ["AMO_JWT_ISSUER"], os.environ["AMO_JWT_SECRET"])
            ver_resp = client.post(
                f"https://addons.mozilla.org/api/v5/addons/addon/{quote(resolved_slug, safe='')}/versions/",
                headers={"Authorization": f"JWT {fresh_tok}", "Content-Type": "application/json"},
                json={"upload": upload_uuid},
            )
            if ver_resp.status_code not in (200, 201, 202):
                raise RegistryPublishError(
                    f"Firefox AMO version creation failed for {resolved_slug!r} (HTTP {ver_resp.status_code})."
                )
            version_id = ver_resp.json().get("id")

        return {
            "registry": "firefox_amo",
            "upload_uuid": upload_uuid,
            "channel": channel,
            "validated": validated,
            "addon_slug": resolved_slug or None,
            "version_id": version_id,
        }


def publish_homebrew_tap_via_github_api(
    *,
    tap_repo: str,
    cask_path: Path | None = None,
    formula_path: Path | None = None,
    readme_path: Path | None = None,
    token: str | None = None,
    version: str = "1.0.0",
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Commit Casks/second-brain.rb and/or Formula/second-brain.rb (plus optional README) to a GitHub Homebrew Tap."""
    clean_tap_repo = _validate_repo_slug(tap_repo, label="Homebrew Tap repository")
    files_to_push: list[tuple[str, bytes]] = []
    if cask_path is not None:
        if not cask_path.is_file():
            raise RegistryPublishError(f"Homebrew Cask file not found: {cask_path}")
        files_to_push.append(("Casks/second-brain.rb", cask_path.read_bytes()))
    if formula_path is not None:
        if not formula_path.is_file():
            raise RegistryPublishError(f"Homebrew Formula file not found: {formula_path}")
        files_to_push.append(("Formula/second-brain.rb", formula_path.read_bytes()))
    if not files_to_push:
        raise RegistryPublishError("At least one of --cask or --formula must be provided for Homebrew Tap publish.")
    if readme_path is not None and readme_path.is_file():
        files_to_push.append(("README.md", readme_path.read_bytes()))

    if dry_run:
        return {
            "registry": "homebrew_tap",
            "dry_run": True,
            "tap_repo": clean_tap_repo,
            "version": version,
            "planned_files": [p for p, _ in files_to_push],
        }

    resolved_token = (token or os.environ.get("HOMEBREW_TAP_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not resolved_token:
        raise RegistryPublishError("Missing HOMEBREW_TAP_TOKEN or GITHUB_TOKEN for Homebrew Tap publish.")

    headers = _github_headers(resolved_token)
    updated: list[str] = []
    with _open_http_client(timeout=30.0, transport=transport) as client:
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
                    f"Failed to update {repo_rel} in Homebrew Tap {clean_tap_repo} (HTTP {put_resp.status_code})."
                )
            updated.append(repo_rel)

    return {"registry": "homebrew_tap", "tap_repo": clean_tap_repo, "updated_files": updated}


def _resolve_winget_version_dir(manifest_dir: Path, version: str) -> Path:
    nested = manifest_dir / "manifests" / "s" / "SecondBrain" / "SecondBrain" / version
    if nested.is_dir():
        return nested
    if manifest_dir.is_dir() and (manifest_dir / "SecondBrain.SecondBrain.installer.yaml").is_file():
        return manifest_dir
    raise RegistryPublishError(
        f"Multi-file WinGet manifests for version {version} not found under {manifest_dir} "
        f"(expected {nested})."
    )


def publish_winget_pkgs_pr_via_github_api(
    *,
    manifest_dir: Path,
    version: str,
    winget_repo: str = "microsoft/winget-pkgs",
    token: str | None = None,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Fork/branch `winget-pkgs`, commit the 3 multi-file 1.6.0 WinGet manifests, and open a PR."""
    clean_ver = str(version or "").strip().lstrip("v")
    if not _SEMVER_RE.fullmatch(clean_ver):
        raise RegistryPublishError(f"Invalid semantic version for WinGet submission: {version!r}")
    clean_repo = _validate_repo_slug(winget_repo, label="WinGet repository")

    ver_dir = _resolve_winget_version_dir(manifest_dir, clean_ver)
    required_files = (
        "SecondBrain.SecondBrain.yaml",
        "SecondBrain.SecondBrain.installer.yaml",
        "SecondBrain.SecondBrain.locale.en-US.yaml",
    )
    payloads: list[tuple[str, bytes]] = []
    import yaml

    for fname in required_files:
        fpath = ver_dir / fname
        if _is_reparse_or_link(fpath) or not fpath.is_file():
            raise RegistryPublishError(f"Missing or symlinked required multi-file WinGet manifest: {fpath}")
        raw_bytes = fpath.read_bytes()
        try:
            doc = yaml.safe_load(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise RegistryPublishError(f"Invalid WinGet YAML manifest {fpath}: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("PackageIdentifier") != "SecondBrain.SecondBrain" or str(doc.get("PackageVersion") or "") != clean_ver:
            raise RegistryPublishError(f"WinGet manifest {fpath.name} has mismatched PackageIdentifier/PackageVersion.")
        repo_rel = f"manifests/s/SecondBrain/SecondBrain/{clean_ver}/{fname}"
        payloads.append((repo_rel, raw_bytes))

    wg_check = validate_winget_manifests_locally(ver_dir)
    if wg_check.get("reason") != "winget_cli_not_on_host" and not (wg_check.get("validated") or wg_check.get("ok")):
        raise RegistryPublishError(
            f"Local WinGet manifest validation failed for {ver_dir}: {wg_check.get('output') or wg_check}"
        )

    headers_branch = f"second-brain-{clean_ver}"
    if dry_run:
        return {
            "registry": "winget_pkgs",
            "dry_run": True,
            "winget_repo": clean_repo,
            "branch": headers_branch,
            "planned_files": [p for p, _ in payloads],
        }

    resolved_token = (token or os.environ.get("WINGET_GITHUB_TOKEN") or "").strip()
    if not resolved_token:
        raise RegistryPublishError("Missing WINGET_GITHUB_TOKEN for WinGet Community Repository submission.")

    headers = _github_headers(resolved_token)
    branch_name = f"second-brain-{clean_ver}"

    with _open_http_client(timeout=30.0, transport=transport) as client:
        user_resp = client.get("https://api.github.com/user", headers=headers)
        if user_resp.status_code != 200:
            raise RegistryPublishError(f"Failed to resolve GitHub identity for WINGET_GITHUB_TOKEN (HTTP {user_resp.status_code}).")
        login = str(user_resp.json().get("login") or "").strip()
        if not login or not _GITHUB_LOGIN_RE.fullmatch(login):
            raise RegistryPublishError(f"GitHub /user response returned invalid login: {login!r}")

        repo_resp = client.get(f"https://api.github.com/repos/{clean_repo}", headers=headers)
        if repo_resp.status_code != 200:
            raise RegistryPublishError(f"Failed to query {clean_repo} (HTTP {repo_resp.status_code}).")
        default_branch = str(repo_resp.json().get("default_branch") or "master").strip()
        if not default_branch or ".." in default_branch or any(ch.isspace() or ch in "\x00?#" for ch in default_branch):
            raise RegistryPublishError(f"GitHub repository {clean_repo} returned invalid default_branch: {default_branch!r}")

        fork_resp = client.post(f"https://api.github.com/repos/{clean_repo}/forks", headers=headers, json={})
        if fork_resp.status_code not in (200, 201, 202):
            raise RegistryPublishError(f"Failed to ensure fork of {clean_repo} (HTTP {fork_resp.status_code}).")
        raw_fork_repo = str(fork_resp.json().get("full_name") or f"{login}/{clean_repo.split('/', 1)[1]}").strip()
        fork_repo = _validate_repo_slug(raw_fork_repo, label="WinGet fork repository")

        encoded_default_branch = quote(default_branch, safe="/")
        base_ref_resp = client.get(
            f"https://api.github.com/repos/{clean_repo}/git/ref/heads/{encoded_default_branch}",
            headers=headers,
        )
        if base_ref_resp.status_code != 200:
            raise RegistryPublishError(f"Failed to read {clean_repo} base branch ref (HTTP {base_ref_resp.status_code}).")
        base_sha = str((base_ref_resp.json().get("object") or {}).get("sha") or "").strip()
        if not base_sha:
            raise RegistryPublishError(f"Base branch {default_branch} ref missing commit SHA.")

        branch_check = client.get(
            f"https://api.github.com/repos/{fork_repo}/git/ref/heads/{branch_name}",
            headers=headers,
        )
        if branch_check.status_code == 404:
            create_ref = client.post(
                f"https://api.github.com/repos/{fork_repo}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            )
            if create_ref.status_code not in (200, 201):
                raise RegistryPublishError(
                    f"Failed to create branch {branch_name} on {fork_repo} (HTTP {create_ref.status_code})."
                )

        committed: list[str] = []
        for repo_rel, raw_bytes in payloads:
            api_url = f"https://api.github.com/repos/{fork_repo}/contents/{repo_rel}"
            existing_sha: str | None = None
            get_resp = client.get(api_url, headers=headers, params={"ref": branch_name})
            if get_resp.status_code == 200:
                existing_sha = get_resp.json().get("sha")
            body: dict[str, Any] = {
                "message": f"chore(winget): add SecondBrain.SecondBrain {clean_ver} ({Path(repo_rel).name})",
                "content": base64.b64encode(raw_bytes).decode("ascii"),
                "branch": branch_name,
            }
            if existing_sha:
                body["sha"] = existing_sha
            put_resp = client.put(api_url, headers=headers, json=body)
            if put_resp.status_code not in (200, 201):
                raise RegistryPublishError(
                    f"Failed to commit {repo_rel} to {fork_repo}@{branch_name} (HTTP {put_resp.status_code})."
                )
            committed.append(repo_rel)

        pr_resp = client.post(
            f"https://api.github.com/repos/{clean_repo}/pulls",
            headers=headers,
            json={
                "title": f"New version: SecondBrain.SecondBrain version {clean_ver}",
                "head": f"{login}:{branch_name}",
                "base": default_branch,
                "body": f"Automated WinGet 1.6.0 multi-file manifest submission for Second Brain `v{clean_ver}`.",
            },
        )
        if pr_resp.status_code not in (200, 201):
            raise RegistryPublishError(f"Failed to open WinGet PR against {clean_repo} (HTTP {pr_resp.status_code}).")
        pr_json = pr_resp.json()
        return {
            "registry": "winget_pkgs",
            "winget_repo": clean_repo,
            "fork_repo": fork_repo,
            "branch": branch_name,
            "committed_files": committed,
            "pr_number": pr_json.get("number"),
            "pr_url": pr_json.get("html_url"),
        }


def _resolve_installer_sha256(installer_url: str, installer_sha256: str | None) -> str:
    clean_sha = str(installer_sha256 or "").strip().lower()
    if clean_sha:
        if not _SHA256_HEX_RE.fullmatch(clean_sha):
            raise RegistryPublishError(f"Invalid installer SHA-256 hex digest: {installer_sha256!r}")
        return clean_sha
    sums_path = ROOT / "dist" / "SHA256SUMS.txt"
    url_name = Path(urlsplit(installer_url).path).name
    if sums_path.is_file() and url_name:
        for line in sums_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == url_name and _SHA256_HEX_RE.fullmatch(parts[0]):
                return parts[0].lower()
    raise RegistryPublishError(
        "installer_sha256 is required when generating WinGet manifests from --installer-url "
        "(or ensure matching digest exists in dist/SHA256SUMS.txt)."
    )


def publish_winget_pkgs(
    *,
    manifest_dir: Path | None = None,
    version: str,
    winget_repo: str = "microsoft/winget-pkgs",
    installer_url: str | None = None,
    installer_sha256: str | None = None,
    token: str | None = None,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Publish WinGet 1.6.0 manifests to `microsoft/winget-pkgs` (from directory or installer URL)."""
    clean_ver = str(version or "").strip().lstrip("v")
    if not _SEMVER_RE.fullmatch(clean_ver):
        raise RegistryPublishError(f"Invalid semantic version for WinGet submission: {version!r}")

    resolved_url = str(installer_url or "").strip()
    if resolved_url and (manifest_dir is None or not manifest_dir.is_dir()):
        parsed = urlsplit(resolved_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RegistryPublishError(f"WinGet installer URL must be HTTPS: {resolved_url!r}")
        sha_hex = _resolve_installer_sha256(resolved_url, installer_sha256)
        import yaml

        ver_doc = {
            "PackageIdentifier": "SecondBrain.SecondBrain",
            "PackageVersion": clean_ver,
            "DefaultLocale": "en-US",
            "ManifestType": "version",
            "ManifestVersion": "1.6.0",
        }
        inst_doc = {
            "PackageIdentifier": "SecondBrain.SecondBrain",
            "PackageVersion": clean_ver,
            "Commands": ["second-brain"],
            "Installers": [
                {
                    "Architecture": "x64",
                    "InstallerType": "nullsoft",
                    "InstallerUrl": resolved_url,
                    "InstallerSha256": sha_hex.upper(),
                }
            ],
            "ManifestType": "installer",
            "ManifestVersion": "1.6.0",
        }
        loc_doc = {
            "PackageIdentifier": "SecondBrain.SecondBrain",
            "PackageVersion": clean_ver,
            "PackageLocale": "en-US",
            "Publisher": "Second Brain",
            "PackageName": "Second Brain",
            "License": "MIT",
            "ShortDescription": "Local-first verifiable AI memory platform with cryptographic byte-offset provenance.",
            "ManifestType": "defaultLocale",
            "ManifestVersion": "1.6.0",
        }
        with tempfile.TemporaryDirectory(prefix="sb_winget_gen_") as td:
            ver_dir = Path(td) / "manifests" / "s" / "SecondBrain" / "SecondBrain" / clean_ver
            ver_dir.mkdir(parents=True, exist_ok=True)
            (ver_dir / "SecondBrain.SecondBrain.yaml").write_text(
                "# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.1.6.0.schema.json\n"
                + yaml.safe_dump(ver_doc, sort_keys=False),
                encoding="utf-8",
            )
            (ver_dir / "SecondBrain.SecondBrain.installer.yaml").write_text(
                "# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.1.6.0.schema.json\n"
                + yaml.safe_dump(inst_doc, sort_keys=False),
                encoding="utf-8",
            )
            (ver_dir / "SecondBrain.SecondBrain.locale.en-US.yaml").write_text(
                "# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.1.6.0.schema.json\n"
                + yaml.safe_dump(loc_doc, sort_keys=False),
                encoding="utf-8",
            )
            return publish_winget_pkgs_pr_via_github_api(
                manifest_dir=Path(td),
                version=clean_ver,
                winget_repo=winget_repo,
                token=token,
                dry_run=dry_run,
                transport=transport,
            )

    candidate_dir = manifest_dir
    if candidate_dir is None:
        preferred = ROOT / "dist" / "manifests" / "winget-pkgs"
        candidate_dir = preferred if preferred.is_dir() else (ROOT / "dist" / "winget")
    return publish_winget_pkgs_pr_via_github_api(
        manifest_dir=candidate_dir,
        version=clean_ver,
        winget_repo=winget_repo,
        token=token,
        dry_run=dry_run,
        transport=transport,
    )


def _is_release_asset_name(name: str) -> bool:
    if name in _BOGUS_RELEASE_ASSETS or name == "SHA256SUMS.txt" or name.startswith("."):
        return False
    low = name.lower()
    return any(low.endswith(sfx) for sfx in _RELEASE_ASSET_SUFFIXES)


def _scan_archive_member_for_forbidden_content(member_name: str, *, is_wheel: bool) -> str | None:
    norm = member_name.replace("\\", "/").lstrip("/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return None
    low_parts = [p.lower() for p in parts]
    fname = parts[-1]
    fname_low = fname.lower()
    if "node_modules" in low_parts:
        return f"node_modules leak: {member_name}"
    if low_parts[0] == "brain" or (len(low_parts) >= 2 and low_parts[1] == "brain"):
        return f"private vault brain/ leak: {member_name}"
    if fname_low == ".env" or fname_low.startswith(".env.") or fname_low in {"local.properties", "google-services.json", "play-key.json", "mcp_config.json"}:
        return f"secret/local-config leak: {member_name}"
    if fname_low.endswith(_FORBIDDEN_ARCHIVE_SECRET_SUFFIXES) or ".bak-" in fname_low or fname_low.endswith(".bak"):
        return f"credential/key/backup leak: {member_name}"
    if is_wheel:
        if fname in _FORBIDDEN_WHEEL_TEST_BASENAMES or fname.endswith(("_tests.py", "_canary.py")):
            return f"test/canary script leak: {member_name}"
        if "mobile" in low_parts and any(p in {"test", "tests", "androidtest", "fastlane"} for p in low_parts):
            return f"mobile test/fastlane leak: {member_name}"
    return None


def verify_release_assets(
    dist_dir: Path,
    *,
    repo_root: Path = ROOT,
    skip_git_timestamp_check: bool = False,
) -> dict[str, Any]:
    """Fail-closed release hygiene, SHA256SUMS, freshness, wheel-debloat, and wheelhouse check."""
    dist_path = Path(dist_dir)
    if os.path.lexists(str(dist_path)) and _is_reparse_or_link(dist_path):
        raise RegistryPublishError(f"Refusing symlinked dist directory: {dist_path}")
    dist_root = dist_path.resolve()
    if not dist_root.is_dir():
        raise RegistryPublishError(f"Dist directory not found: {dist_root}")

    sums_path = dist_root / "SHA256SUMS.txt"
    if _is_reparse_or_link(sums_path) or not sums_path.is_file():
        raise RegistryPublishError(f"Missing or symlinked SHA256SUMS.txt in {dist_root}")

    checksums: dict[str, str] = {}
    for raw_line in sums_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        m = _SHA256SUMS_LINE_RE.fullmatch(raw_line)
        if not m:
            raise RegistryPublishError(f"Malformed SHA256SUMS.txt line: {raw_line!r}")
        digest = m.group(1).lower()
        fname = m.group(2)
        if digest == _ZERO_SHA256:
            raise RegistryPublishError(f"Placeholder all-zero SHA-256 forbidden in SHA256SUMS.txt: {raw_line!r}")
        if (
            fname.startswith(".")
            or ".." in fname
            or fname in _BOGUS_RELEASE_ASSETS
            or not _SAFE_ASSET_NAME_RE.fullmatch(fname)
        ):
            raise RegistryPublishError(f"Forbidden or unsafe asset entry in SHA256SUMS.txt: {fname!r}")
        if fname in checksums:
            raise RegistryPublishError(f"Duplicate asset entry in SHA256SUMS.txt: {fname!r}")
        checksums[fname] = digest

    if not checksums:
        raise RegistryPublishError(f"SHA256SUMS.txt in {dist_root} contains zero artifact entries.")

    disk_assets: list[str] = []
    unexpected_files: list[str] = []
    for entry in sorted(dist_root.iterdir(), key=lambda p: p.name):
        if _is_reparse_or_link(entry):
            raise RegistryPublishError(f"Refusing symlink/reparse point inside release dist/: {entry.name!r}")
        if not entry.is_file():
            continue
        if _is_release_asset_name(entry.name):
            disk_assets.append(entry.name)
        elif entry.name not in _ALLOWED_UNHASHED_DIST_FILES:
            unexpected_files.append(entry.name)

    if unexpected_files:
        raise RegistryPublishError(
            f"Unexpected unverified file(s) in release dist/: {', '.join(unexpected_files)}"
        )

    missing_from_sums = sorted(set(disk_assets) - set(checksums))
    if missing_from_sums:
        raise RegistryPublishError(f"Release artifact(s) omitted from SHA256SUMS.txt: {', '.join(missing_from_sums)}")

    missing_on_disk = sorted(set(checksums) - set(disk_assets))
    if missing_on_disk:
        raise RegistryPublishError(f"SHA256SUMS.txt references missing artifact(s): {', '.join(missing_on_disk)}")

    for fname in disk_assets:
        actual = hashlib.sha256((dist_root / fname).read_bytes()).hexdigest().lower()
        if actual != checksums[fname]:
            raise RegistryPublishError(f"SHA-256 mismatch for {fname}: expected {checksums[fname]}, got {actual}")

    # Load live source scripts once so wheel / sdist / unpacked bundles are checked for full script freshness
    live_scripts_dir = repo_root / "scripts"
    live_scripts_norm: dict[str, bytes] = {}
    if live_scripts_dir.is_dir():
        for sp in sorted(live_scripts_dir.glob("*.py")):
            if sp.is_file() and not _is_reparse_or_link(sp):
                live_scripts_norm[sp.name] = sp.read_bytes().replace(b"\r\n", b"\n")

    for whl_path in sorted(dist_root.glob("second_brain-*.whl")):
        with zipfile.ZipFile(whl_path, "r") as zf:
            names = zf.namelist()
            for member in names:
                violation = _scan_archive_member_for_forbidden_content(member, is_wheel=True)
                if violation:
                    raise RegistryPublishError(f"Production wheel {whl_path.name} failed hygiene check ({violation})")
            if live_scripts_norm:
                if "second_brain/scripts/publish_registries.py" not in names:
                    raise RegistryPublishError(f"Wheel {whl_path.name} missing second_brain/scripts/publish_registries.py")
                for member in names:
                    if member.startswith("second_brain/scripts/") and member.endswith(".py"):
                        script_name = member[len("second_brain/scripts/") :]
                        if "/" not in script_name and script_name in live_scripts_norm:
                            if zf.read(member).replace(b"\r\n", b"\n") != live_scripts_norm[script_name]:
                                raise RegistryPublishError(
                                    f"Stale wheel {whl_path.name}: bundled scripts/{script_name} differs from live source!"
                                )

    for sdist_path in sorted(dist_root.glob("second_brain-*.tar.gz")):
        with tarfile.open(sdist_path, "r:gz") as tf:
            members = tf.getmembers()
            saw_pub = False
            for m in members:
                if not m.isfile():
                    continue
                violation = _scan_archive_member_for_forbidden_content(m.name, is_wheel=False)
                if violation:
                    raise RegistryPublishError(f"Source distribution {sdist_path.name} failed hygiene check ({violation})")
                norm_m = m.name.replace("\\", "/")
                if "/scripts/" in norm_m and norm_m.endswith(".py"):
                    rel_after_scripts = norm_m.split("/scripts/", 1)[1]
                    if "/" not in rel_after_scripts and rel_after_scripts in live_scripts_norm:
                        if rel_after_scripts == "publish_registries.py":
                            saw_pub = True
                        fh = tf.extractfile(m)
                        assert fh is not None
                        if fh.read().replace(b"\r\n", b"\n") != live_scripts_norm[rel_after_scripts]:
                            raise RegistryPublishError(
                                f"Stale sdist {sdist_path.name}: bundled scripts/{rel_after_scripts} differs from live source!"
                            )
            if live_scripts_norm and "publish_registries.py" in live_scripts_norm and not saw_pub:
                raise RegistryPublishError(f"sdist {sdist_path.name} missing scripts/publish_registries.py")

    unpacked_script_dirs = [
        dist_root / "win-unpacked" / "resources" / "backend" / "scripts",
        dist_root / "linux-unpacked" / "resources" / "backend" / "scripts",
        *[
            p / "Second Brain.app" / "Contents" / "Resources" / "backend" / "scripts"
            for p in sorted(dist_root.glob("mac*"))
            if p.is_dir() and not _is_reparse_or_link(p)
        ],
    ]
    for unpacked_scripts in unpacked_script_dirs:
        if not unpacked_scripts.is_dir() or _is_reparse_or_link(unpacked_scripts):
            continue
        for sp in sorted(unpacked_scripts.glob("*.py")):
            if _is_reparse_or_link(sp):
                raise RegistryPublishError(f"Symlinked script in unpacked bundle: {sp}")
            if sp.name in _FORBIDDEN_UNPACKED_SCRIPT_BASENAMES or sp.name.endswith(("_tests.py", "_canary.py")):
                raise RegistryPublishError(f"Unpacked desktop bundle includes forbidden test script: {sp.name}")
            if sp.name in live_scripts_norm:
                if sp.read_bytes().replace(b"\r\n", b"\n") != live_scripts_norm[sp.name]:
                    raise RegistryPublishError(
                        f"Stale unpacked desktop bundle script {sp.relative_to(dist_root)} differs from live source!"
                    )

    wh_dir = dist_root / "wheelhouse"
    if wh_dir.is_dir():
        seen_wh: dict[tuple[str, str], list[str]] = {}
        for whl in sorted(wh_dir.glob("*.whl")):
            parts = whl.stem.split("-")
            dist_part = parts[0].lower()
            plat_part = parts[-1].lower() if len(parts) >= 5 else "any"
            arch_bucket = "arm64" if ("arm64" in plat_part or "aarch64" in plat_part) else (
                "x86_64" if ("x86_64" in plat_part or "amd64" in plat_part) else "any"
            )
            seen_wh.setdefault((dist_part, arch_bucket), []).append(whl.name)
        dup_wh = {f"{k[0]}[{k[1]}]": v for k, v in seen_wh.items() if len(v) > 1}
        if dup_wh:
            raise RegistryPublishError(f"Duplicate wheel versions in {wh_dir}: {dup_wh}")

    if not skip_git_timestamp_check and (repo_root / ".git").exists():
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip().isdigit():
            head_ts = int(proc.stdout.strip())
            stale_files = [
                fname for fname in disk_assets if (dist_root / fname).stat().st_mtime < (head_ts - 1)
            ]
            if stale_files:
                raise RegistryPublishError(
                    f"Release artifact(s) predate HEAD commit timestamp ({head_ts}): {', '.join(stale_files)}"
                )

    return {
        "verified": True,
        "dist_dir": str(dist_root),
        "asset_count": len(disk_assets),
        "assets": disk_assets,
    }


def publish_github_release_assets(
    *,
    repo_slug: str,
    tag: str,
    dist_dir: Path,
    repo_root: Path = ROOT,
    release_name: str | None = None,
    body_path: Path | None = None,
    token: str | None = None,
    skip_git_timestamp_check: bool = False,
    prune_extra: bool = True,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Verify `dist_dir`, prune stale/bogus remote assets (e.g. `default.gitignore`), and upload verified assets."""
    clean_repo = _validate_repo_slug(repo_slug, label="GitHub Release repository")
    clean_tag = str(tag or "").strip()
    if not clean_tag or not re.fullmatch(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$", clean_tag):
        raise RegistryPublishError(f"Invalid GitHub Release tag: {tag!r}")

    verified = verify_release_assets(
        dist_dir,
        repo_root=repo_root,
        skip_git_timestamp_check=skip_git_timestamp_check,
    )
    dist_root = Path(verified["dist_dir"])
    upload_filenames = [*verified["assets"], "SHA256SUMS.txt"]
    sig_path = dist_root / "SHA256SUMS.txt.sig"
    if sig_path.is_file() and not _is_reparse_or_link(sig_path):
        upload_filenames.append("SHA256SUMS.txt.sig")
    if dry_run:
        return {
            "registry": "github_release",
            "dry_run": True,
            "repo_slug": clean_repo,
            "tag": clean_tag,
            "planned_assets": upload_filenames,
        }

    resolved_token = (token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not resolved_token:
        raise RegistryPublishError("Missing GITHUB_TOKEN or GH_TOKEN for GitHub Release upload.")
    upload_set = set(upload_filenames)

    headers = _github_headers(resolved_token)
    with _open_http_client(timeout=180.0, transport=transport) as client:
        rel_resp = client.get(f"https://api.github.com/repos/{clean_repo}/releases/tags/{clean_tag}", headers=headers)
        if rel_resp.status_code == 404:
            body_txt = body_path.read_text(encoding="utf-8") if (body_path and body_path.is_file()) else ""
            create_resp = client.post(
                f"https://api.github.com/repos/{clean_repo}/releases",
                headers=headers,
                json={
                    "tag_name": clean_tag,
                    "name": release_name or f"Second Brain {clean_tag}",
                    "body": body_txt,
                    "draft": False,
                    "prerelease": "-" in clean_tag,
                },
            )
            if create_resp.status_code not in (200, 201):
                raise RegistryPublishError(
                    f"Failed to create GitHub Release {clean_tag} on {clean_repo} (HTTP {create_resp.status_code})."
                )
            rel_json = create_resp.json()
        elif rel_resp.status_code == 200:
            rel_json = rel_resp.json()
        else:
            raise RegistryPublishError(
                f"Failed to fetch GitHub Release {clean_tag} on {clean_repo} (HTTP {rel_resp.status_code})."
            )

        raw_upload_url = str(rel_json.get("upload_url") or "").split("{", 1)[0].strip()
        parsed_up = urlsplit(raw_upload_url)
        try:
            up_port = parsed_up.port
        except ValueError as exc:
            raise RegistryPublishError(f"Unexpected GitHub Release upload_url port: {raw_upload_url!r}") from exc
        if (
            parsed_up.scheme != "https"
            or parsed_up.hostname != "uploads.github.com"
            or up_port not in (None, 443)
            or parsed_up.username is not None
            or parsed_up.password is not None
            or parsed_up.query
            or parsed_up.fragment
            or not parsed_up.path.startswith(f"/repos/{clean_repo}/releases/")
        ):
            raise RegistryPublishError(f"Unexpected GitHub Release upload_url: {raw_upload_url!r}")

        deleted_bogus: list[str] = []
        replaced_assets: list[str] = []
        pruned_extra: list[str] = []
        for existing in rel_json.get("assets") or []:
            if not isinstance(existing, dict):
                continue
            ename = str(existing.get("name") or "")
            eid = existing.get("id")
            if not isinstance(eid, int) or isinstance(eid, bool) or eid <= 0:
                continue
            should_delete = ename in _BOGUS_RELEASE_ASSETS or ename in upload_set or bool(prune_extra)
            if should_delete:
                del_resp = client.delete(
                    f"https://api.github.com/repos/{clean_repo}/releases/assets/{eid}",
                    headers=headers,
                )
                if del_resp.status_code not in (200, 204):
                    raise RegistryPublishError(
                        f"Failed to delete existing release asset {ename!r} (HTTP {del_resp.status_code})."
                    )
                if ename in _BOGUS_RELEASE_ASSETS:
                    deleted_bogus.append(ename)
                elif ename in upload_set:
                    replaced_assets.append(ename)
                else:
                    pruned_extra.append(ename)

        uploaded: list[str] = []
        for fname in upload_filenames:
            fpath = dist_root / fname
            if _is_reparse_or_link(fpath) or not fpath.is_file():
                raise RegistryPublishError(f"Refusing to upload missing or symlinked release asset: {fname!r}")
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            up_resp = client.post(
                f"{raw_upload_url}?name={quote(fname)}",
                headers={**headers, "Content-Type": ctype},
                content=fpath.read_bytes(),
            )
            if up_resp.status_code not in (200, 201):
                raise RegistryPublishError(f"Failed to upload release asset {fname!r} (HTTP {up_resp.status_code}).")
            uploaded.append(fname)

    return {
        "registry": "github_release",
        "repo_slug": clean_repo,
        "tag": clean_tag,
        "uploaded_assets": uploaded,
        "replaced_assets": replaced_assets,
        "deleted_bogus_assets": deleted_bogus,
        "pruned_extra_assets": pruned_extra,
    }


def _validate_wheel_cleanliness(whl_path: Path) -> None:
    with zipfile.ZipFile(whl_path, "r") as zf:
        names = zf.namelist()
    for member in names:
        violation = _scan_archive_member_for_forbidden_content(member, is_wheel=True)
        if violation:
            raise RegistryPublishError(f"Production wheel {whl_path.name} failed hygiene check ({violation})")


def _validate_sdist_cleanliness(sdist_path: Path) -> None:
    with tarfile.open(sdist_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            violation = _scan_archive_member_for_forbidden_content(member.name, is_wheel=False)
            if violation:
                raise RegistryPublishError(f"Source distribution {sdist_path.name} failed hygiene check ({violation})")


def publish_pypi(
    dist_dir: Path,
    *,
    dry_run: bool = False,
    repository_url: str | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Validate built wheels/sdists in `dist_dir` and upload via `uv publish` (OIDC or `PYPI_API_TOKEN`)."""
    dist_root = Path(dist_dir).resolve()
    if not dist_root.is_dir():
        raise RegistryPublishError(f"PyPI dist directory not found: {dist_root}")

    packages = sorted(
        [*dist_root.glob("second_brain-*.whl"), *dist_root.glob("second_brain-*.tar.gz")],
        key=lambda p: p.name,
    )
    if not packages:
        raise RegistryPublishError(f"No second_brain wheel or sdist packages found in {dist_root}")
    for pkg in packages:
        if pkg.suffix == ".whl":
            _validate_wheel_cleanliness(pkg)
        elif pkg.name.endswith(".tar.gz"):
            _validate_sdist_cleanliness(pkg)

    resolved_repo_url = (repository_url or os.environ.get("UV_PUBLISH_URL") or "").strip()
    if resolved_repo_url:
        parsed_url = urlsplit(resolved_repo_url)
        host_low = (parsed_url.hostname or "").strip().lower()
        is_loopback = host_low in {"127.0.0.1", "::1", "localhost"}
        if (
            not host_low
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.scheme not in {"https", "http"}
            or (parsed_url.scheme == "http" and not is_loopback)
        ):
            raise RegistryPublishError(
                f"PyPI repository URL must use HTTPS (or loopback HTTP for local testing): {resolved_repo_url!r}"
            )

    creds = check_registry_credentials()["pypi"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing PyPI credentials: {', '.join(creds['required_env'])}")

    package_names = [p.name for p in packages]
    if dry_run:
        res: dict[str, Any] = {
            "registry": "pypi",
            "dry_run": True,
            "dist_dir": str(dist_root),
            "packages": package_names,
            "auth_mode": "token" if _has_env("PYPI_API_TOKEN") else "oidc",
        }
        if resolved_repo_url:
            res["repository_url"] = resolved_repo_url
        return res

    uv_bin = shutil.which("uv") or "uv"
    cmd = [uv_bin, "publish", *[str(p) for p in packages]]
    env = dict(os.environ)
    token = (env.get("PYPI_API_TOKEN") or "").strip()
    if token:
        env["UV_PUBLISH_TOKEN"] = token
    if resolved_repo_url:
        env["UV_PUBLISH_URL"] = resolved_repo_url

    proc = runner(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise RegistryPublishError(
            f"PyPI upload failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:800]}"
        )
    out: dict[str, Any] = {
        "registry": "pypi",
        "packages": package_names,
        "returncode": proc.returncode,
    }
    if resolved_repo_url:
        out["repository_url"] = resolved_repo_url
    return out


def _resolve_vsix_path(vsix: str | Path | None = None, dist_dir: str | Path = ROOT / "dist") -> Path:
    explicit = str(vsix or "").strip()
    if explicit:
        return Path(explicit)
    dist_root = Path(dist_dir)
    if dist_root.is_dir():
        matches = sorted(dist_root.glob("*.vsix"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        if matches:
            return matches[0]
    raise RegistryPublishError(f"No .vsix package found in {dist_root}; pass --vsix explicitly.")


def _validate_vsix_package(vsix_path: Path) -> None:
    if _is_reparse_or_link(vsix_path) or not vsix_path.is_file():
        raise RegistryPublishError(f"VSIX package not found or symlinked: {vsix_path}")
    try:
        with zipfile.ZipFile(vsix_path, "r") as zf:
            names = _validate_zip_member_safety(zf, pkg_name=vsix_path.name, label="VSIX")
    except (zipfile.BadZipFile, OSError) as exc:
        raise RegistryPublishError(f"Invalid VSIX package {vsix_path}: {exc}") from exc
    for member in sorted(names):
        violation = _scan_archive_member_for_forbidden_content(member, is_wheel=False)
        if violation:
            raise RegistryPublishError(f"VSIX {vsix_path.name} failed hygiene check ({violation})")
    if "extension/package.json" not in names:
        raise RegistryPublishError(f"VSIX {vsix_path.name} missing extension/package.json")


def publish_vscode_marketplace(
    vsix_path: Path,
    *,
    dry_run: bool = False,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Publish a verified `.vsix` to Visual Studio Marketplace using `VSCE_PAT` env."""
    _validate_vsix_package(vsix_path)
    creds = check_registry_credentials()["vscode_marketplace"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing VS Code Marketplace credentials: {', '.join(creds['required_env'])}")
    if dry_run:
        return {"registry": "vscode_marketplace", "dry_run": True, "package": vsix_path.name}

    npx_bin = shutil.which("npx") or "npx"
    cmd = [npx_bin, "--yes", "@vscode/vsce@3.3.2", "publish", "--packagePath", str(vsix_path)]
    env = dict(os.environ)
    env["VSCE_PAT"] = os.environ["VSCE_PAT"].strip()
    proc = runner(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise RegistryPublishError(
            f"VS Code Marketplace publish failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:800]}"
        )
    return {"registry": "vscode_marketplace", "package": vsix_path.name, "returncode": proc.returncode}


def publish_open_vsx(
    vsix_path: Path,
    *,
    dry_run: bool = False,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Publish a verified `.vsix` to Eclipse Open VSX Registry using `OVSX_PAT` env."""
    _validate_vsix_package(vsix_path)
    creds = check_registry_credentials()["open_vsx"]
    if not creds["ready"]:
        raise RegistryPublishError(f"Missing Open VSX credentials: {', '.join(creds['required_env'])}")
    if dry_run:
        return {"registry": "open_vsx", "dry_run": True, "package": vsix_path.name}

    npx_bin = shutil.which("npx") or "npx"
    cmd = [npx_bin, "--yes", "ovsx@0.10.1", "publish", str(vsix_path)]
    env = dict(os.environ)
    env["OVSX_PAT"] = os.environ["OVSX_PAT"].strip()
    proc = runner(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise RegistryPublishError(
            f"Open VSX publish failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:800]}"
        )
    return {"registry": "open_vsx", "package": vsix_path.name, "returncode": proc.returncode}


def format_setup_wizard() -> dict[str, Any]:
    """Return actionable credential provisioning instructions and `gh secret set` commands."""
    readiness = check_registry_credentials()
    registries = [
        {
            "id": "pypi",
            "ready": readiness["pypi"]["ready"],
            "secrets": ["PYPI_API_TOKEN"],
            "steps": "Create a PyPI API token (or configure Trusted Publishing for .github/workflows/release.yml).",
            "gh_commands": ["gh secret set PYPI_API_TOKEN"],
        },
        {
            "id": "vscode_marketplace",
            "ready": readiness["vscode_marketplace"]["ready"],
            "secrets": ["VSCE_PAT"],
            "steps": "Create Azure DevOps PAT with Marketplace (Publish) scope for publisher 'second-brain'.",
            "gh_commands": ["gh secret set VSCE_PAT"],
        },
        {
            "id": "open_vsx",
            "ready": readiness["open_vsx"]["ready"],
            "secrets": ["OVSX_PAT"],
            "steps": "Create Open VSX access token at https://open-vsx.org/user-settings/tokens.",
            "gh_commands": ["gh secret set OVSX_PAT"],
        },
        {
            "id": "chrome_web_store",
            "ready": readiness["chrome_web_store"]["ready"],
            "secrets": [
                "CWS_CLIENT_ID",
                "CWS_CLIENT_SECRET",
                "CWS_REFRESH_TOKEN",
                "CWS_EXTENSION_ID",
                "CWS_PUBLISHER_ID",
            ],
            "steps": "Enable Chrome Web Store API v2 in Google Cloud Console and capture publisher + extension IDs.",
            "gh_commands": [
                "gh secret set CWS_CLIENT_ID",
                "gh secret set CWS_CLIENT_SECRET",
                "gh secret set CWS_REFRESH_TOKEN",
                "gh secret set CWS_EXTENSION_ID",
                "gh secret set CWS_PUBLISHER_ID",
            ],
        },
        {
            "id": "edge_addons",
            "ready": readiness["edge_addons"]["ready"],
            "secrets": ["EDGE_PRODUCT_ID", "EDGE_CLIENT_ID", "EDGE_API_KEY"],
            "steps": "Generate Partner Center API key under Edge Add-ons Publish API settings.",
            "gh_commands": [
                "gh secret set EDGE_PRODUCT_ID",
                "gh secret set EDGE_CLIENT_ID",
                "gh secret set EDGE_API_KEY",
            ],
        },
        {
            "id": "firefox_amo",
            "ready": readiness["firefox_amo"]["ready"],
            "secrets": ["AMO_JWT_ISSUER", "AMO_JWT_SECRET", "AMO_ADDON_SLUG"],
            "steps": "Create API credentials on https://addons.mozilla.org/developers/addon/api/key/.",
            "gh_commands": [
                "gh secret set AMO_JWT_ISSUER",
                "gh secret set AMO_JWT_SECRET",
                "gh secret set AMO_ADDON_SLUG",
            ],
        },
        {
            "id": "homebrew_tap",
            "ready": readiness["homebrew_tap"]["ready"],
            "secrets": ["HOMEBREW_TAP_TOKEN"],
            "steps": "Fine-grained PAT with Contents Read/Write on <owner>/homebrew-second-brain.",
            "gh_commands": ["gh secret set HOMEBREW_TAP_TOKEN"],
        },
        {
            "id": "winget_pkgs",
            "ready": readiness["winget_pkgs"]["ready"],
            "secrets": ["WINGET_GITHUB_TOKEN"],
            "steps": "Fine-grained or classic PAT permitted to fork and open PRs on microsoft/winget-pkgs.",
            "gh_commands": ["gh secret set WINGET_GITHUB_TOKEN"],
        },
        {
            "id": "desktop_code_signing",
            "ready": any(_has_env(k) for k in ("WINDOWS_PFX_BASE64", "SECOND_BRAIN_AZURE_CODE_SIGNING_ACCOUNT", "CSC_LINK", "MAC_CSC_LINK")),
            "secrets": [
                "WINDOWS_PFX_BASE64",
                "WINDOWS_PFX_PASSWORD",
                "SECOND_BRAIN_AZURE_TRUSTED_SIGNING_ENDPOINT",
                "SECOND_BRAIN_AZURE_CODE_SIGNING_ACCOUNT",
                "SECOND_BRAIN_AZURE_CERT_PROFILE",
                "MAC_CSC_LINK",
                "MAC_CSC_KEY_PASSWORD",
                "APPLE_ID",
                "APPLE_APP_SPECIFIC_PASSWORD",
                "APPLE_TEAM_ID",
                "RELEASE_ED25519_PRIVATE_KEY",
            ],
            "steps": "Provision Windows OV/EV PFX or Azure Trusted Signing + Apple Developer ID Application P12 + Notary credentials.",
            "gh_commands": [
                "gh secret set WINDOWS_PFX_BASE64",
                "gh secret set MAC_CSC_LINK",
                "gh secret set RELEASE_ED25519_PRIVATE_KEY",
            ],
        },
        {
            "id": "mobile_stores",
            "ready": _has_env("GOOGLE_PLAY_JSON_KEY_BASE64") or _has_env("APP_STORE_CONNECT_API_KEY_BASE64"),
            "secrets": [
                "ANDROID_KEYSTORE_BASE64",
                "ANDROID_KEYSTORE_PASSWORD",
                "ANDROID_KEY_ALIAS",
                "ANDROID_KEY_PASSWORD",
                "GOOGLE_PLAY_JSON_KEY_BASE64",
                "APP_STORE_CONNECT_API_KEY_ID",
                "APP_STORE_CONNECT_ISSUER_ID",
                "APP_STORE_CONNECT_API_KEY_BASE64",
                "MATCH_PASSWORD",
            ],
            "steps": "Provision Android upload keystore + Play Service Account JSON and App Store Connect API .p8 + Fastlane Match.",
            "gh_commands": [
                "gh secret set GOOGLE_PLAY_JSON_KEY_BASE64",
                "gh secret set APP_STORE_CONNECT_API_KEY_BASE64",
            ],
        },
    ]
    return {"registries": registries}


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

    p_status = sub.add_parser("status", help="Report credential readiness and discovered dist/ artifacts for all registries.")
    p_status.add_argument("--dist-dir", default=str(ROOT / "dist"))
    p_status.add_argument("--json", action="store_true")

    p_wizard = sub.add_parser("setup-wizard", help="Print credential provisioning guide and gh secret commands.")
    p_wizard.add_argument("--json", action="store_true")

    p_winget = sub.add_parser("validate-winget", help="Validate WinGet manifest directory with local winget CLI.")
    p_winget.add_argument("manifest_dir")
    p_winget.add_argument("--json", action="store_true")

    p_pypi = sub.add_parser("publish-pypi", help="Validate and upload Python wheels/sdists to PyPI.")
    p_pypi.add_argument("--dist-dir", default=str(ROOT / "dist"))
    p_pypi.add_argument("--repository-url", default="", help="Custom PyPI / TestPyPI upload URL (sets UV_PUBLISH_URL).")
    p_pypi.add_argument("--dry-run", action="store_true")

    p_vsix = sub.add_parser("publish-vsix", help="Publish VSIX to VS Code Marketplace and/or Open VSX.")
    p_vsix.add_argument("--vsix", required=True)
    p_vsix.add_argument("--target", choices=("all", "vscode", "ovsx"), default="all")
    p_vsix.add_argument("--dry-run", action="store_true")

    p_vscode = sub.add_parser("publish-vscode", help="Publish VSIX to Visual Studio Code Marketplace.")
    p_vscode.add_argument("--vsix", default="", help="Path to .vsix (auto-discovers from --dist-dir if omitted).")
    p_vscode.add_argument("--dist-dir", default=str(ROOT / "dist"))
    p_vscode.add_argument("--dry-run", action="store_true")

    p_ovsx = sub.add_parser("publish-openvsx", help="Publish VSIX to Eclipse Open VSX Registry.")
    p_ovsx.add_argument("--vsix", default="", help="Path to .vsix (auto-discovers from --dist-dir if omitted).")
    p_ovsx.add_argument("--dist-dir", default=str(ROOT / "dist"))
    p_ovsx.add_argument("--dry-run", action="store_true")

    p_tap = sub.add_parser("publish-homebrew-tap", help="Publish Casks/second-brain.rb and/or Formula/second-brain.rb to a live GitHub Homebrew Tap.")
    p_tap.add_argument("--tap-repo", required=True, help="owner/homebrew-second-brain")
    p_tap.add_argument("--cask", default="")
    p_tap.add_argument("--formula", default="")
    p_tap.add_argument("--readme", default="")
    p_tap.add_argument("--version", default="1.0.0")
    p_tap.add_argument("--dry-run", action="store_true")

    p_wg_pr = sub.add_parser("publish-winget-pkgs", help="Submit multi-file 1.6.0 WinGet manifests PR to microsoft/winget-pkgs.")
    p_wg_pr.add_argument("--manifest-dir", required=True)
    p_wg_pr.add_argument("--version", required=True)
    p_wg_pr.add_argument("--winget-repo", default="microsoft/winget-pkgs")
    p_wg_pr.add_argument("--dry-run", action="store_true")

    p_wg_alias = sub.add_parser("publish-winget", help="Submit WinGet 1.6.0 manifests PR to microsoft/winget-pkgs.")
    p_wg_alias.add_argument("--manifest-dir", default="", help="Path to generated WinGet manifests directory.")
    p_wg_alias.add_argument("--version", required=True)
    p_wg_alias.add_argument("--winget-repo", default="microsoft/winget-pkgs")
    p_wg_alias.add_argument("--installer-url", default="", help="HTTPS installer URL to synthesize manifests when --manifest-dir is omitted.")
    p_wg_alias.add_argument("--installer-sha256", default="", help="SHA-256 hex digest matching --installer-url.")
    p_wg_alias.add_argument("--dry-run", action="store_true")

    p_verify = sub.add_parser("verify-release-assets", help="Verify dist/ SHA256SUMS, freshness, wheel debloat, and wheelhouse cleanliness.")
    p_verify.add_argument("--dist-dir", default=str(ROOT / "dist"))
    p_verify.add_argument("--skip-git-timestamp-check", action="store_true")
    p_verify.add_argument("--json", action="store_true")

    p_ghrel = sub.add_parser("publish-github-release", help="Verify dist/ assets and upload to a GitHub Release (pruning stale/bogus assets).")
    p_ghrel.add_argument("--repo", required=True, help="owner/repo")
    p_ghrel.add_argument("--tag", required=True, help="Release tag, e.g. v1.0.0")
    p_ghrel.add_argument("--dist-dir", default=str(ROOT / "dist"))
    p_ghrel.add_argument("--body-path", default="")
    p_ghrel.add_argument("--skip-git-timestamp-check", action="store_true")
    p_ghrel.add_argument("--dry-run", action="store_true")

    p_stores = sub.add_parser("publish-browser-stores", help="Publish Web Clipper to Chrome, Edge, and Firefox stores.")
    p_stores.add_argument("--chromium-zip", required=True)
    p_stores.add_argument("--firefox-xpi", required=True)
    p_stores.add_argument("--skip-missing-creds", action="store_true")
    p_stores.add_argument("--dry-run", action="store_true")

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

    if args.cmd == "status":
        st_rep = get_registry_publish_status(Path(args.dist_dir))
        if args.json:
            print(json.dumps(st_rep, indent=2))
        else:
            print(f"Registry status ({st_rep['ready_count']}/{st_rep['total_registries']} credentials configured):")
            for reg, info in st_rep["registries"].items():
                st_label = "READY" if info["ready"] else "MISSING_CREDENTIALS"
                print(f"[{st_label:19}] {reg:20} artifacts={info['artifact_count']}")
        return 0

    if args.cmd == "setup-wizard":
        wiz = format_setup_wizard()
        if args.json:
            print(json.dumps(wiz, indent=2))
        else:
            for item in wiz["registries"]:
                st = "READY" if item["ready"] else "TODO"
                print(f"[{st}] {item['id']}: {item['steps']}")
                for cmd_line in item["gh_commands"]:
                    print(f"      {cmd_line}")
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

    if args.cmd == "publish-pypi":
        print(
            json.dumps(
                publish_pypi(
                    Path(args.dist_dir),
                    dry_run=bool(args.dry_run),
                    repository_url=(args.repository_url or None),
                ),
                indent=2,
            )
        )
        return 0

    if args.cmd == "publish-vsix":
        vsix_res: dict[str, Any] = {}
        if args.target in ("all", "vscode"):
            vsix_res["vscode_marketplace"] = publish_vscode_marketplace(Path(args.vsix), dry_run=bool(args.dry_run))
        if args.target in ("all", "ovsx"):
            vsix_res["open_vsx"] = publish_open_vsx(Path(args.vsix), dry_run=bool(args.dry_run))
        print(json.dumps(vsix_res, indent=2))
        return 0

    if args.cmd == "publish-vscode":
        vsix_target = _resolve_vsix_path(args.vsix, args.dist_dir)
        print(json.dumps(publish_vscode_marketplace(vsix_target, dry_run=bool(args.dry_run)), indent=2))
        return 0

    if args.cmd == "publish-openvsx":
        vsix_target = _resolve_vsix_path(args.vsix, args.dist_dir)
        print(json.dumps(publish_open_vsx(vsix_target, dry_run=bool(args.dry_run)), indent=2))
        return 0

    if args.cmd == "publish-homebrew-tap":
        res = publish_homebrew_tap_via_github_api(
            tap_repo=args.tap_repo,
            cask_path=Path(args.cask) if args.cask else None,
            formula_path=Path(args.formula) if args.formula else None,
            readme_path=Path(args.readme) if args.readme else None,
            version=args.version,
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "publish-winget-pkgs":
        res = publish_winget_pkgs_pr_via_github_api(
            manifest_dir=Path(args.manifest_dir),
            version=args.version,
            winget_repo=args.winget_repo,
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "publish-winget":
        res = publish_winget_pkgs(
            manifest_dir=Path(args.manifest_dir) if args.manifest_dir else None,
            version=args.version,
            winget_repo=args.winget_repo,
            installer_url=args.installer_url or None,
            installer_sha256=args.installer_sha256 or None,
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "verify-release-assets":
        res = verify_release_assets(
            Path(args.dist_dir),
            skip_git_timestamp_check=bool(args.skip_git_timestamp_check),
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Verified {res['asset_count']} release asset(s) in {res['dist_dir']}.")
        return 0

    if args.cmd == "publish-github-release":
        res = publish_github_release_assets(
            repo_slug=args.repo,
            tag=args.tag,
            dist_dir=Path(args.dist_dir),
            body_path=Path(args.body_path) if args.body_path else None,
            skip_git_timestamp_check=bool(args.skip_git_timestamp_check),
            dry_run=bool(args.dry_run),
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
            results[key] = fn(path_arg, dry_run=bool(args.dry_run))
        print(json.dumps(results, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
