# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Where this machine sends Ollama requests.

``OLLAMA_HOST`` stays loopback-only. ``BRAIN_OLLAMA_HOST`` is the explicit
opt-in for a private LAN address. Public hosts are rejected. Keep-alive
defaults to 30 minutes and is sent on chat and embed calls.
"""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

_KEEP_ALIVE_RE = re.compile(r"^(?:-1|\d+[smh])$")


def ollama_keep_alive() -> str:
    raw = (os.environ.get("BRAIN_OLLAMA_KEEP_ALIVE") or os.environ.get("OLLAMA_KEEP_ALIVE") or "30m").strip()
    return raw if _KEEP_ALIVE_RE.fullmatch(raw) else "30m"


def _split_host(raw: str) -> tuple[str, str, int] | None:
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return None
    candidate = text if "://" in text else f"http://{text}"
    if not candidate.lower().startswith("http://"):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port or 11434
    except ValueError:
        return None
    hostname = (parsed.hostname or "").strip().lower()
    if (
        parsed.scheme != "http"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not (1 <= int(port) <= 65535)
    ):
        return None
    return hostname, candidate, int(port)


def host_reach_class(hostname: str) -> str:
    """Return loopback, private, or public. DNS names that are not localhost are public."""
    name = (hostname or "").strip().lower()
    if name == "localhost":
        return "loopback"
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return "public"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private and not ip.is_link_local and not ip.is_multicast and not ip.is_reserved:
        return "private"
    return "public"


def private_or_loopback_ollama_url(raw: str) -> str | None:
    parsed = _split_host(raw)
    if parsed is None:
        return None
    hostname, _candidate, port = parsed
    kind = host_reach_class(hostname)
    if kind == "public":
        return None
    literal = "[::1]" if hostname == "::1" else hostname
    return f"http://{literal}:{port}"


def resolved_ollama_base_url() -> tuple[str | None, str]:
    """Return ``(base_url, source)``.

    ``BRAIN_OLLAMA_HOST`` may be loopback or a private LAN address.
    ``OLLAMA_HOST`` remains loopback-only and is used when the new setting is unset.
    """
    remote = (os.environ.get("BRAIN_OLLAMA_HOST") or "").strip()
    if remote:
        base = private_or_loopback_ollama_url(remote)
        return base, "BRAIN_OLLAMA_HOST"
    from scripts.hardware_profile import loopback_ollama_base_url

    return loopback_ollama_base_url(None), "OLLAMA_HOST"


def ollama_timeout_seconds() -> float:
    raw = (os.environ.get("BRAIN_OLLAMA_TIMEOUT") or "120").strip()
    try:
        value = float(raw)
    except ValueError:
        return 120.0
    if value < 1:
        return 1.0
    if value > 600:
        return 600.0
    return value


def fetch_ollama_tags(base: str, *, timeout: float = 3.0) -> dict | None:
    """GET /api/tags only after the URL has been restricted to loopback or a private LAN."""
    import json
    import urllib.error
    import urllib.request

    allowed = private_or_loopback_ollama_url(base)
    if not allowed:
        return None
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(allowed + "/api/tags", timeout=timeout) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def ollama_host_warning() -> str | None:
    remote = (os.environ.get("BRAIN_OLLAMA_HOST") or "").strip()
    if not remote:
        return None
    parsed = _split_host(remote)
    if parsed is None:
        return f"BRAIN_OLLAMA_HOST is malformed: {remote!r}"
    kind = host_reach_class(parsed[0])
    if kind == "public":
        return f"BRAIN_OLLAMA_HOST is not loopback or private LAN: {remote!r}. Refusing it."
    if kind == "private":
        return f"BRAIN_OLLAMA_HOST points at another machine on the LAN ({remote}). Vault text will be sent there."
    return None
