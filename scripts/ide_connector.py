# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""One-click IDE / AI-host connector for the Second Brain MCP server.

``second-brain connect <host>`` merges a Second Brain entry into the host's MCP config:

* existing servers and settings are preserved, and the original file is backed up
  (``.bak-<timestamp>``) before an atomic write;
* the MCP token is generated if missing, and the session uses trusted-stdio auth,
  so no manual ``_meta`` or token editing is needed;
* ``disconnect`` removes only the Second Brain entry.

Privacy: a host whose model runs in the cloud (Claude, Cursor, Windsurf, Copilot,
Antigravity) will send recalled verified memories and notes to its provider when it
uses these tools. ``connect`` states this and requires ``--yes`` or interactive
confirmation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import _is_reparse_or_link  # noqa: E402
from host_adapters import restrict_security_file_permissions  # noqa: E402

SERVER_KEY = "second-brain"
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class HostTarget:
    host_id: str
    label: str
    config_path: Path
    servers_key: str  # "mcpServers" or "servers" (VS Code)
    cloud_model: bool = True
    extra_entry: dict | None = None


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def host_targets(home: Path | None = None) -> dict[str, HostTarget]:
    home = home or Path.home()
    if os.name == "nt":
        claude_desktop = _appdata() / "Claude" / "claude_desktop_config.json"
        vscode = _appdata() / "Code" / "User" / "mcp.json"
    elif sys.platform == "darwin":
        claude_desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        vscode = home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    else:
        claude_desktop = home / ".config" / "Claude" / "claude_desktop_config.json"
        vscode = home / ".config" / "Code" / "User" / "mcp.json"
    return {
        "claude-code": HostTarget("claude-code", "Claude Code (user scope)", home / ".claude.json", "mcpServers"),
        "claude-desktop": HostTarget("claude-desktop", "Claude Desktop", claude_desktop, "mcpServers"),
        "cursor": HostTarget("cursor", "Cursor", home / ".cursor" / "mcp.json", "mcpServers"),
        "windsurf": HostTarget("windsurf", "Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers"),
        "vscode": HostTarget("vscode", "VS Code (Copilot agent mode)", vscode, "servers", extra_entry={"type": "stdio"}),
        "antigravity": HostTarget(
            "antigravity",
            "Antigravity (plugin bundle)",
            home / ".gemini" / "config" / "plugins" / "second-brain" / "mcp_config.json",
            "mcpServers",
        ),
    }


def server_entry(token: str, *, extra: dict | None = None) -> dict:
    if (REPO_ROOT / "pyproject.toml").exists():
        uv = shutil.which("uv") or "uv"
        command, args = uv, ["--directory", str(REPO_ROOT), "run", "--frozen", "python", "scripts/mcp_server.py"]
    else:
        command, args = sys.executable, ["-m", "scripts.mcp_server"]
    entry = {
        **(extra or {}),
        "command": command,
        "args": args,
        "env": {
            "BRAIN_ENABLE_LOCAL_MCP": "1",
            "BRAIN_MCP_TRUSTED_STDIO": "1",
            "BRAIN_MCP_CLIENT_TOKEN": token,
            **({"BRAIN_VAULT_DIR": os.environ["BRAIN_VAULT_DIR"]} if os.environ.get("BRAIN_VAULT_DIR") else {}),
        },
    }
    return entry


def ensure_mcp_token() -> str:
    from mcp_server import generate_bearer_token, get_configured_bearer_token, save_bearer_token

    token = get_configured_bearer_token()
    if not token:
        token = generate_bearer_token()
        save_bearer_token(token)
    return token


def _load_json(path: Path) -> dict:
    if os.path.lexists(path) and _is_reparse_or_link(str(path)):
        raise PermissionError(f"Refusing to read config through symlink or junction: {path}")
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix or move it before connecting.") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def _atomic_write_json(path: Path, data: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(path.parent)) or (os.path.lexists(path) and _is_reparse_or_link(str(path))):
        raise PermissionError(f"Refusing to write config through symlink or junction: {path}")
    backup = None
    if path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        if os.path.lexists(backup) and _is_reparse_or_link(str(backup)):
            raise PermissionError(f"Refusing to write backup through symlink or junction: {backup}")
        shutil.copy2(path, backup)
        restrict_security_file_permissions(backup)
    tmp = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(tmp) and _is_reparse_or_link(str(tmp)):
        raise PermissionError(f"Refusing to write temp file through symlink or junction: {tmp}")
    raw = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    restrict_security_file_permissions(path)
    return backup


def _write_antigravity_bundle(plugin_dir: Path) -> None:
    """Antigravity plugin = plugin.json + mcp_config.json + skill + rules (copied from repo template)."""
    template = REPO_ROOT / "plugins" / "antigravity" / "second-brain"
    if not template.is_dir():
        raise FileNotFoundError(f"Antigravity plugin template missing: {template}")
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(plugin_dir)):
        raise PermissionError(f"Refusing to write Antigravity plugin bundle through symlink or junction: {plugin_dir}")
    for src in template.rglob("*"):
        if src.is_symlink() or _is_reparse_or_link(str(src)):
            continue
        rel = src.relative_to(template)
        if rel.name == "mcp_config.json":
            continue  # generated with the live token below
        dest = plugin_dir / rel
        if os.path.lexists(dest) and _is_reparse_or_link(str(dest)):
            raise PermissionError(f"Refusing to overwrite symlinked Antigravity bundle file: {dest}")
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def connect(host_id: str, *, dry_run: bool = False, home: Path | None = None) -> dict:
    targets = host_targets(home)
    if host_id not in targets:
        raise ValueError(f"Unknown host '{host_id}'. Choose one of: {', '.join(sorted(targets))}")
    target = targets[host_id]
    token = "<generated-on-connect>" if dry_run else ensure_mcp_token()
    data = _load_json(target.config_path)
    servers = data.get(target.servers_key)
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise ValueError(f"'{target.servers_key}' in {target.config_path} is not an object.")
    servers[SERVER_KEY] = server_entry(token, extra=target.extra_entry)
    data[target.servers_key] = servers
    if dry_run:
        redacted = json.loads(json.dumps(servers[SERVER_KEY]))
        return {"host": host_id, "config_path": str(target.config_path), "entry": redacted, "dry_run": True}
    if host_id == "antigravity":
        _write_antigravity_bundle(target.config_path.parent)
    backup = _atomic_write_json(target.config_path, data)
    return {
        "host": host_id,
        "label": target.label,
        "config_path": str(target.config_path),
        "backup": str(backup) if backup else None,
        "status": "connected",
        "next_step": "Restart or reload the host so it picks up the new MCP server.",
    }


def disconnect(host_id: str, *, home: Path | None = None) -> dict:
    target = host_targets(home)[host_id]
    data = _load_json(target.config_path)
    servers = data.get(target.servers_key)
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return {"host": host_id, "status": "not_connected"}
    servers.pop(SERVER_KEY)
    backup = _atomic_write_json(target.config_path, data)
    return {"host": host_id, "status": "disconnected", "backup": str(backup) if backup else None}


def status(home: Path | None = None) -> list[dict]:
    rows = []
    for host_id, target in host_targets(home).items():
        try:
            data = _load_json(target.config_path)
            connected = SERVER_KEY in (data.get(target.servers_key) or {})
        except ValueError:
            connected = False
        rows.append({
            "host": host_id,
            "label": target.label,
            "connected": connected,
            "installed": target.config_path.parent.exists(),
            "config_path": str(target.config_path),
        })
    return rows


PRIVACY_NOTICE = (
    "When this host's AI uses Second Brain tools, the recalled verified memories and notes are sent to "
    "that host's model provider (for example Anthropic, OpenAI, or Google). Your vault itself stays on this "
    "machine, and agents can only add risk-scanned notes; they cannot verify or approve memories."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="second-brain connect", description="Connect AI hosts/IDEs to Second Brain.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("add", help="Connect a host.")
    c.add_argument("host", choices=sorted(host_targets()))
    c.add_argument("--yes", action="store_true", help="Accept the privacy notice non-interactively.")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    d = sub.add_parser("remove", help="Disconnect a host.")
    d.add_argument("host", choices=sorted(host_targets()))
    d.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    s = sub.add_parser("status", help="Show which hosts are connected.")
    s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.cmd == "status":
        rows = status()
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {"targets": {r["host"]: {**r, "name": r["label"]} for r in rows}, "items": rows},
                    indent=2,
                )
            )
            return 0
        for row in rows:
            mark = "✔ connected" if row["connected"] else ("· available" if row["installed"] else "  not installed")
            print(f"{mark:16} {row['label']:34} {row['config_path']}")
        return 0
    if args.cmd == "remove":
        print(json.dumps(disconnect(args.host), indent=2))
        return 0
    if not args.dry_run and not args.yes:
        print(PRIVACY_NOTICE)
        if not sys.stdin.isatty() or input("Connect? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Not connected.")
            return 1
    print(json.dumps(connect(args.host, dry_run=args.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
