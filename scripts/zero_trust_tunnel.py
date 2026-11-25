# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Opt-In Zero-Trust Remote Access Tunnel Supervisor (Tailscale / Cloudflare Tunnel).

``memory_api.py`` ALWAYS remains strictly bound to ``127.0.0.1``. This supervisor
ships **disabled by default** and enforces a strict read/capture-only remote allowlist
(`/v1/health`, `/v1/recall`, `/v1/chat/stream`, `/v1/inbox/drop`), hard-blocking all
mutating, host-policy, backup, or skill-execution paths (`403 Forbidden`) with sliding-window
rate limiting and append-only audit logs.
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
from collections import deque
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

if __name__ in ("zero_trust_tunnel", "scripts.zero_trust_tunnel"):
    sys.modules.setdefault("zero_trust_tunnel", sys.modules[__name__])
    sys.modules.setdefault("scripts.zero_trust_tunnel", sys.modules[__name__])

from scripts.atom_mutator import atomic_write_bytes  # noqa: E402
from scripts.config import BRAIN_DIR, _is_reparse_or_link, resolve_module_vault_path, security_state_dir  # noqa: E402
from scripts.security_fs import restrict_security_file_permissions  # noqa: E402

TUNNEL_CONFIG_FILENAME = "zero-trust-tunnel.json"
DEFAULT_LOCAL_API_PORT = 8765
DEFAULT_GATEWAY_PORT = 8790
MAX_RATE_BUCKETS = 512
_TUNNEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
ALLOWED_TUNNEL_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/v1/health"): "health_probe",
    ("POST", "/v1/recall"): "readonly_recall",
    ("POST", "/v1/chat/stream"): "grounded_chat",
    ("POST", "/v1/inbox/drop"): "quarantined_inbox_drop",
}
_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: dict[str, deque[float]] = {}


class TunnelSecurityError(PermissionError):
    """Raised when tunnel access is disabled, disallowed, or rate-limited."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def tunnel_config_path(state_dir: str | Path | None = None) -> Path:
    raw_base = Path(os.path.expanduser(str(state_dir))) if state_dir is not None else Path(security_state_dir())
    if os.path.lexists(str(raw_base)) and _is_reparse_or_link(str(raw_base)):
        raise PermissionError(f"Refusing symlinked security state dir: {raw_base}")
    base = raw_base.resolve()
    if os.path.lexists(str(base)) and _is_reparse_or_link(str(base)):
        raise PermissionError(f"Refusing symlinked security state dir: {base}")
    base.mkdir(parents=True, exist_ok=True)
    target = base / TUNNEL_CONFIG_FILENAME
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing symlinked tunnel config file: {target}")
    return target


def load_tunnel_config(state_dir: str | Path | None = None) -> dict[str, Any]:
    cfg_p = tunnel_config_path(state_dir)
    defaults: dict[str, Any] = {
        "enabled": False,
        "provider": "tailscale",
        "target_port": DEFAULT_LOCAL_API_PORT,
        "gateway_port": DEFAULT_GATEWAY_PORT,
        "rate_limit_per_min": 30,
        "tunnel_id": None,
        "allow_quick_tunnel": False,
        "allowed_routes": [f"{m} {p}" for (m, p) in ALLOWED_TUNNEL_ROUTES],
        "updated_at": None,
    }
    if not cfg_p.is_file():
        return defaults
    try:
        loaded = json.loads(cfg_p.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw_tid = str(loaded.get("tunnel_id") or "").strip()
            clean_tid = raw_tid if (raw_tid and _TUNNEL_ID_RE.fullmatch(raw_tid)) else None
            return {
                "enabled": bool(loaded.get("enabled", False)),
                "provider": "cloudflared" if loaded.get("provider") == "cloudflared" else "tailscale",
                "target_port": max(1024, min(65535, int(loaded.get("target_port", DEFAULT_LOCAL_API_PORT)))),
                "gateway_port": max(1024, min(65535, int(loaded.get("gateway_port", DEFAULT_GATEWAY_PORT)))),
                "rate_limit_per_min": max(5, min(300, int(loaded.get("rate_limit_per_min", 30)))),
                "tunnel_id": clean_tid,
                "allow_quick_tunnel": bool(loaded.get("allow_quick_tunnel", False)),
                "allowed_routes": [f"{m} {p}" for (m, p) in ALLOWED_TUNNEL_ROUTES],
                "updated_at": loaded.get("updated_at"),
            }
    except Exception:
        pass
    return defaults


def configure_tunnel(
    *,
    enabled: bool | None = None,
    provider: str | None = None,
    target_port: int | None = None,
    gateway_port: int | None = None,
    rate_limit_per_min: int | None = None,
    tunnel_id: str | None = None,
    allow_quick_tunnel: bool | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = load_tunnel_config(state_dir)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if provider is not None:
        prov = str(provider or "").strip().lower()
        if prov not in {"tailscale", "cloudflared"}:
            raise ValueError("Tunnel provider must be 'tailscale' or 'cloudflared'.")
        cfg["provider"] = prov
    if target_port is not None:
        tp = int(target_port)
        if not (1024 <= tp <= 65535):
            raise ValueError("target_port must be in 1024..65535.")
        cfg["target_port"] = tp
    if gateway_port is not None:
        gp = int(gateway_port)
        if not (1024 <= gp <= 65535):
            raise ValueError("gateway_port must be in 1024..65535.")
        cfg["gateway_port"] = gp
    if rate_limit_per_min is not None:
        rl = int(rate_limit_per_min)
        if not (5 <= rl <= 300):
            raise ValueError("rate_limit_per_min must be in 5..300.")
        cfg["rate_limit_per_min"] = rl
    if tunnel_id is not None:
        clean_tid = str(tunnel_id or "").strip()
        if clean_tid and not _TUNNEL_ID_RE.fullmatch(clean_tid):
            raise ValueError(f"Invalid named tunnel_id: {tunnel_id!r}")
        cfg["tunnel_id"] = clean_tid or None
    if allow_quick_tunnel is not None:
        cfg["allow_quick_tunnel"] = bool(allow_quick_tunnel)
    if (
        cfg.get("enabled")
        and cfg.get("provider") == "cloudflared"
        and not cfg.get("tunnel_id")
        and not cfg.get("allow_quick_tunnel")
    ):
        raise TunnelSecurityError(
            "Enabling cloudflared requires a named tunnel_id or explicit allow_quick_tunnel=True opt-in."
        )
    cfg["updated_at"] = _now_iso()
    cfg_p = tunnel_config_path(state_dir)
    atomic_write_bytes(str(cfg_p), (json.dumps(cfg, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    restrict_security_file_permissions(cfg_p)
    return cfg


def probe_tunnel_providers() -> dict[str, str | None]:
    return {
        "tailscale": shutil.which("tailscale"),
        "cloudflared": shutil.which("cloudflared"),
    }


def build_tunnel_command(
    provider: str,
    *,
    local_port: int,
    tunnel_id: str | None = None,
    allow_quick_tunnel: bool = False,
) -> list[str]:
    clean_p = str(provider or "").strip().lower()
    if not (1024 <= int(local_port) <= 65535):
        raise ValueError("local_port must be in 1024..65535.")
    if clean_p == "tailscale":
        bin_p = shutil.which("tailscale") or "tailscale"
        return [bin_p, "serve", "--bg", f"http://127.0.0.1:{int(local_port)}"]
    if clean_p == "cloudflared":
        bin_p = shutil.which("cloudflared") or "cloudflared"
        clean_tid = str(tunnel_id or "").strip()
        if clean_tid:
            if not _TUNNEL_ID_RE.fullmatch(clean_tid):
                raise ValueError(f"Invalid named cloudflared tunnel_id: {tunnel_id!r}")
            return [bin_p, "tunnel", "run", clean_tid]
        if allow_quick_tunnel:
            return [bin_p, "tunnel", "--url", f"http://127.0.0.1:{int(local_port)}"]
        raise TunnelSecurityError(
            "Refusing unauthenticated cloudflared quick tunnel without explicit allow_quick_tunnel=True or a named tunnel_id."
        )
    raise ValueError(f"Unsupported tunnel provider: {provider!r}")


def tunnel_status(*, state_dir: str | Path | None = None) -> dict[str, Any]:
    cfg = load_tunnel_config(state_dir)
    bins = probe_tunnel_providers()
    prov = cfg["provider"]
    cmd_preview: list[str] = []
    sec_warning: str | None = None
    try:
        cmd_preview = build_tunnel_command(
            prov,
            local_port=int(cfg["gateway_port"]),
            tunnel_id=cfg.get("tunnel_id"),
            allow_quick_tunnel=bool(cfg.get("allow_quick_tunnel", False)),
        )
    except TunnelSecurityError as exc:
        sec_warning = str(exc)
    out: dict[str, Any] = {
        **cfg,
        "provider_binaries": bins,
        "provider_available": bool(bins.get(prov)),
        "command_preview": cmd_preview,
    }
    if sec_warning:
        out["security_warning"] = sec_warning
    return out


def _append_tunnel_audit(brain_dir: str | Path, rec: dict[str, Any]) -> None:
    try:
        raw_brain = Path(os.path.expanduser(str(brain_dir)))
        if os.path.lexists(str(raw_brain)) and _is_reparse_or_link(str(raw_brain)):
            return
        raw_dir = raw_brain / "0-raw"
        if os.path.lexists(str(raw_dir)) and _is_reparse_or_link(str(raw_dir)):
            return
        arch = raw_dir / "archive"
        if os.path.lexists(str(arch)) and _is_reparse_or_link(str(arch)):
            return
        arch.mkdir(parents=True, exist_ok=True)
        if _is_reparse_or_link(str(arch)):
            return
        audit_p = arch / "tunnel-audit.jsonl"
        if os.path.lexists(str(audit_p)) and _is_reparse_or_link(str(audit_p)):
            return
        existed_before = os.path.exists(str(audit_p))
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(str(audit_p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed_before:
            restrict_security_file_permissions(audit_p)
    except Exception:
        pass


def authorize_tunnel_request(
    method: str,
    path: str,
    *,
    source_id: str = "remote",
    state_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Fail-closed policy & rate-limit gate for every tunneled HTTP request."""
    eff_brain = resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    cfg = load_tunnel_config(state_dir)
    clean_m = str(method or "").upper().strip()
    clean_p = str(path or "").split("?", 1)[0].strip()

    if not cfg.get("enabled"):
        _append_tunnel_audit(eff_brain, {"ts": _now_iso(), "source": source_id, "method": clean_m, "path": clean_p, "decision": "denied_disabled"})
        raise TunnelSecurityError("Zero-Trust Tunnel is disabled by default.")

    scope = ALLOWED_TUNNEL_ROUTES.get((clean_m, clean_p))
    if not scope:
        _append_tunnel_audit(eff_brain, {"ts": _now_iso(), "source": source_id, "method": clean_m, "path": clean_p, "decision": "denied_route"})
        raise TunnelSecurityError(f"403 Forbidden: route '{clean_m} {clean_p}' is blocked over remote tunnel.")

    now = time.monotonic()
    cap = int(cfg["rate_limit_per_min"])
    bucket_key = str(source_id or "remote")[:128]
    with _RATE_LOCK:
        stale_keys: list[str] = []
        for k, bq in _RATE_WINDOWS.items():
            while bq and (now - bq[0]) > 60.0:
                bq.popleft()
            if not bq:
                stale_keys.append(k)
        for k in stale_keys:
            _RATE_WINDOWS.pop(k, None)

        if bucket_key not in _RATE_WINDOWS and len(_RATE_WINDOWS) >= MAX_RATE_BUCKETS:
            oldest_key = min(_RATE_WINDOWS, key=lambda k: _RATE_WINDOWS[k][-1] if _RATE_WINDOWS[k] else 0.0)
            _RATE_WINDOWS.pop(oldest_key, None)

        dq = _RATE_WINDOWS.setdefault(bucket_key, deque())
        while dq and (now - dq[0]) > 60.0:
            dq.popleft()
        if len(dq) >= cap:
            _append_tunnel_audit(eff_brain, {"ts": _now_iso(), "source": bucket_key, "method": clean_m, "path": clean_p, "decision": "denied_rate_limit"})
            raise TunnelSecurityError("429 Too Many Requests: tunnel rate limit exceeded.")
        dq.append(now)

    _append_tunnel_audit(eff_brain, {"ts": _now_iso(), "source": bucket_key, "method": clean_m, "path": clean_p, "decision": "allowed", "scope": scope})
    return {"allowed": True, "scope": scope, "target_port": cfg["target_port"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_en = sub.add_parser("enable")
    p_en.add_argument("--provider", default="tailscale", choices=("tailscale", "cloudflared"))
    p_en.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT)
    p_en.add_argument("--target-port", type=int, default=DEFAULT_LOCAL_API_PORT)
    p_en.add_argument("--tunnel-id", default=None, help="Named Cloudflare Zero-Trust tunnel ID/name.")
    p_en.add_argument(
        "--allow-quick-tunnel",
        action="store_true",
        help="Explicitly opt into an unauthenticated cloudflared quick tunnel URL (not recommended).",
    )
    sub.add_parser("disable")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(tunnel_status(), indent=2))
    elif args.cmd == "enable":
        cfg = configure_tunnel(
            enabled=True,
            provider=args.provider,
            gateway_port=args.gateway_port,
            target_port=args.target_port,
            tunnel_id=args.tunnel_id,
            allow_quick_tunnel=bool(args.allow_quick_tunnel),
        )
        print(json.dumps(cfg, indent=2))
    else:
        print(json.dumps(configure_tunnel(enabled=False), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
