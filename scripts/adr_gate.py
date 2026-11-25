# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Owner Architecture Decision Record (ADR) Fail-Closed Gate (Epic 2D.7).

Experimental features excluded by ``docs/cognitive-core.md`` guardrails
(``ADR-LORA-EXPERIMENT`` and ``ADR-NOTES-CRDT``) are STRICTLY DISABLED by
default and raise ``ADRRequiredError`` unless explicitly authorized by an
owner-signed record in ``brain/0-raw/archive/owner-adrs.jsonl``.
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
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

if __name__ in ("adr_gate", "scripts.adr_gate"):
    sys.modules.setdefault("adr_gate", sys.modules[__name__])
    sys.modules.setdefault("scripts.adr_gate", sys.modules[__name__])

from scripts.atom_mutator import atomic_write_bytes, validate_actor_name, vault_writer_lock  # noqa: E402
from scripts.config import _is_reparse_or_link, resolve_module_atoms_and_brain, snapshot_module_defaults  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

KNOWN_EXPERIMENTAL_ADRS = frozenset({"ADR-LORA-EXPERIMENT", "ADR-NOTES-CRDT"})
GENESIS_HASH = "0" * 64


class ADRRequiredError(PermissionError):
    """Raised whenever an experimental capability lacks an active owner ADR."""


def adr_log_path(brain_dir: str | Path | None = None) -> Path:
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    return Path(resolved_brain) / "0-raw" / "archive" / "owner-adrs.jsonl"


def _canonical_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _read_adr_log(log_p: Path) -> tuple[bool, list[dict[str, Any]], str]:
    if not log_p.exists():
        return True, [], GENESIS_HASH
    if _is_reparse_or_link(str(log_p)) or _is_reparse_or_link(str(log_p.parent)):
        return False, [], ""
    try:
        lines = [ln for ln in log_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError):
        return False, [], ""
    records: list[dict[str, Any]] = []
    prev_h = GENESIS_HASH
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            return False, [], ""
        if not isinstance(obj, dict):
            return False, [], ""
        rec_h = str(obj.get("entry_sha256") or "")
        body = {k: v for k, v in obj.items() if k != "entry_sha256"}
        if str(body.get("prev_entry_sha256") or "") != prev_h:
            return False, [], ""
        if hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest() != rec_h:
            return False, [], ""
        records.append(obj)
        prev_h = rec_h
    return True, records, prev_h


def set_adr_status(
    adr_id: str,
    *,
    enabled: bool,
    actor: str,
    rationale: str,
    expires_on: str | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    clean_id = (adr_id or "").strip().upper()
    if clean_id not in KNOWN_EXPERIMENTAL_ADRS:
        raise ADRRequiredError(f"Unknown experimental ADR id {adr_id!r}; expected {sorted(KNOWN_EXPERIMENTAL_ADRS)}.")
    clean_actor = validate_actor_name(actor)
    clean_rationale = (rationale or "").strip()[:400]
    if not clean_rationale:
        raise ValueError("A non-empty rationale is required to record an owner ADR.")
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    log_p = adr_log_path(resolved_brain)

    with vault_writer_lock(resolved_brain):
        log_p.parent.mkdir(parents=True, exist_ok=True)
        ok, records, prev_h = _read_adr_log(log_p)
        if not ok:
            raise ADRRequiredError("owner-adrs.jsonl failed hash-chain verification (fail-closed).")
        body = {
            "seq": len(records) + 1,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "adr_id": clean_id,
            "status": "enabled" if enabled else "disabled",
            "actor": clean_actor,
            "rationale": clean_rationale,
            "expires_on": (expires_on or "").strip()[:10] or None,
            "prev_entry_sha256": prev_h,
        }
        entry_sha = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
        rec = {**body, "entry_sha256": entry_sha}
        existing = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        atomic_write_bytes(str(log_p), (existing + json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    return rec


def list_adr_states(*, brain_dir: str | Path | None = None) -> dict[str, dict[str, Any]]:
    log_p = adr_log_path(brain_dir)
    ok, records, _ = _read_adr_log(log_p)
    states: dict[str, dict[str, Any]] = {
        adr: {"adr_id": adr, "enabled": False, "status": "disabled", "chain_valid": ok}
        for adr in sorted(KNOWN_EXPERIMENTAL_ADRS)
    }
    if not ok:
        return states
    today_iso = datetime.date.today().isoformat()
    for r in records:
        adr = str(r.get("adr_id") or "").upper()
        if adr not in states:
            continue
        exp = str(r.get("expires_on") or "").strip()
        is_expired = bool(exp and exp < today_iso)
        st = str(r.get("status") or "disabled")
        active = st == "enabled" and not is_expired
        states[adr] = {
            "adr_id": adr,
            "enabled": active,
            "status": "expired" if (st == "enabled" and is_expired) else st,
            "actor": r.get("actor"),
            "timestamp": r.get("timestamp"),
            "expires_on": exp or None,
            "rationale": r.get("rationale"),
            "chain_valid": True,
        }
    return states


def require_adr_active(adr_id: str, *, brain_dir: str | Path | None = None) -> dict[str, Any]:
    """Fail closed with ADRRequiredError unless adr_id is explicitly active and hash-valid."""
    clean_id = (adr_id or "").strip().upper()
    states = list_adr_states(brain_dir=brain_dir)
    info = states.get(clean_id)
    if not info or not info.get("chain_valid") or not info.get("enabled"):
        reason = info.get("status", "disabled") if info else "unknown"
        raise ADRRequiredError(
            f"Experimental feature '{clean_id}' is blocked (status={reason}). "
            "Explicit owner ADR authorization is required."
        )
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Owner Experimental ADR Gate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_s = sub.add_parser("status", help="Show experimental ADR states")
    p_s.add_argument("--json", action="store_true")

    p_e = sub.add_parser("enable", help="Enable an experimental ADR (owner only)")
    p_e.add_argument("adr_id", choices=sorted(KNOWN_EXPERIMENTAL_ADRS))
    p_e.add_argument("--actor", required=True)
    p_e.add_argument("--rationale", required=True)
    p_e.add_argument("--expires-on", default=None)

    p_d = sub.add_parser("disable", help="Disable/revoke an experimental ADR")
    p_d.add_argument("adr_id", choices=sorted(KNOWN_EXPERIMENTAL_ADRS))
    p_d.add_argument("--actor", required=True)
    p_d.add_argument("--rationale", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "status":
        st = list_adr_states()
        print(json.dumps(st, indent=2) if args.json else str(st))
        return 0
    rec = set_adr_status(
        args.adr_id,
        enabled=(args.cmd == "enable"),
        actor=args.actor,
        rationale=args.rationale,
        expires_on=getattr(args, "expires_on", None),
    )
    print(json.dumps(rec, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
