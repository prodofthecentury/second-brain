# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Owner-ADR-Gated Experimental Notes-Only CRDT Sync (Epic 2D.7).

STRICTLY DISABLED by default unless ``ADR-NOTES-CRDT`` is active in
``brain/0-raw/archive/owner-adrs.jsonl``.
CRITICAL INVARIANT: CRDT merge operations are strictly confined to working
scratchpads inside ``brain/notes/``. Any path under ``2-atoms/``, ``5-playbooks/``,
``6-skills/``, ``7-principles/``, ``8-mental-models/``, or ``0-raw/`` raises
``PermissionError`` immediately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("experimental_crdt_notes", "scripts.experimental_crdt_notes"):
    sys.modules.setdefault("experimental_crdt_notes", sys.modules[__name__])
    sys.modules.setdefault("scripts.experimental_crdt_notes", sys.modules[__name__])

from adr_gate import require_adr_active  # noqa: E402
from atom_mutator import atomic_write_bytes  # noqa: E402
from config import _is_reparse_or_link, resolve_module_atoms_and_brain, snapshot_module_defaults  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

_FORBIDDEN_CANONICAL_DIRS = frozenset(
    {"0-raw", "1-desk", "2-atoms", "3-threads", "4-experiences", "5-playbooks", "6-skills", "7-principles", "8-mental-models"}
)


def _assert_notes_only_target(rel_or_path: str | Path, brain_dir: str) -> Path:
    raw_brain = Path(os.path.expanduser(str(brain_dir)))
    if os.path.lexists(str(raw_brain)) and _is_reparse_or_link(str(raw_brain)):
        raise PermissionError(f"Refusing symlinked brain dir: {raw_brain}")
    bpath = raw_brain.resolve()

    raw_notes_dir = bpath / "notes"
    if os.path.lexists(str(raw_notes_dir)) and _is_reparse_or_link(str(raw_notes_dir)):
        raise PermissionError(f"Refusing symlinked notes dir: {raw_notes_dir}")
    notes_root = raw_notes_dir.resolve()
    if notes_root != raw_notes_dir:
        raise PermissionError(f"Refusing notes dir resolving outside vault notes root: {notes_root}")

    raw_str = str(rel_or_path or "").strip()
    if not raw_str or "\x00" in raw_str:
        raise PermissionError("Invalid CRDT note path.")
    norm = raw_str.replace("\\", "/")
    first_seg = norm.lstrip("/").split("/", 1)[0].lower()
    if first_seg in _FORBIDDEN_CANONICAL_DIRS or any(f"/{f}/" in f"/{norm.lower()}/" for f in _FORBIDDEN_CANONICAL_DIRS):
        raise PermissionError(
            f"Notes-Only CRDT strictly forbids canonical stores ({raw_str!r}); only brain/notes/*.md allowed."
        )

    raw_rel_path = Path(raw_str)
    if ".." in raw_rel_path.parts:
        raise PermissionError(f"CRDT target must not traverse parent directories: {raw_str!r}")

    if os.path.isabs(raw_str):
        cand = Path(os.path.expanduser(raw_str))
        if os.path.lexists(str(cand)) and _is_reparse_or_link(str(cand)):
            raise PermissionError(f"Refusing symlinked note path: {cand}")
        # Check every raw ancestor between raw_notes_dir / raw_brain and cand before resolving
        cur_check = cand
        while True:
            if os.path.lexists(str(cur_check)) and _is_reparse_or_link(str(cur_check)):
                raise PermissionError(f"Refusing symlinked path segment in CRDT target: {cur_check}")
            if cur_check in {raw_notes_dir, bpath, raw_brain} or cur_check.parent == cur_check:
                break
            cur_check = cur_check.parent
    else:
        cand = raw_notes_dir / raw_rel_path
        # Walk every raw intermediate segment under raw_notes_dir BEFORE .resolve()
        cur = raw_notes_dir
        for part in raw_rel_path.parts:
            if part in ("", ".", ".."):
                if part == "..":
                    raise PermissionError(f"CRDT target must not traverse parent directories: {raw_str!r}")
                continue
            cur = cur / part
            if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
                raise PermissionError(f"Refusing symlinked path segment in CRDT target: {cur}")

    if os.path.lexists(str(cand)) and _is_reparse_or_link(str(cand)):
        raise PermissionError(f"Refusing symlinked note file: {cand}")

    resolved = cand.resolve()
    try:
        rel_resolved = resolved.relative_to(notes_root)
    except ValueError as exc:
        raise PermissionError(f"CRDT target must stay strictly inside {notes_root}: {resolved}") from exc
    if resolved.suffix.lower() != ".md" or ".." in resolved.parts:
        raise PermissionError(f"CRDT target must be a safe .md note inside brain/notes/: {resolved}")
    # Verify every segment under notes_root on the final path as well
    cur_res = notes_root
    for part in rel_resolved.parts:
        cur_res = cur_res / part
        if os.path.lexists(str(cur_res)) and _is_reparse_or_link(str(cur_res)):
            raise PermissionError(f"Refusing symlinked note path: {cur_res}")
    return resolved


def crdt_merge_note_lines(
    local_state: list[dict[str, Any]],
    remote_state: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic LWW-Element-Set + causal HLC ordering over note line items.

    Each element has ``{"elem_id": str, "hlc": int, "replica": str, "text": str, "deleted": bool}``.
    Commutative, associative, idempotent merge (`A + B == B + A`).
    """
    by_id: dict[str, dict[str, Any]] = {}
    for item in [*local_state, *remote_state]:
        if not isinstance(item, dict) or not item.get("elem_id"):
            continue
        eid = str(item["elem_id"])
        cand = {
            "elem_id": eid,
            "hlc": int(item.get("hlc") or 0),
            "replica": str(item.get("replica") or "local"),
            "text": str(item.get("text") or ""),
            "deleted": bool(item.get("deleted", False)),
            "order_key": int(item.get("order_key") if item.get("order_key") is not None else int(item.get("hlc") or 0)),
        }
        cur = by_id.get(eid)
        if cur is None:
            by_id[eid] = cand
            continue
        cand_key = (cand["hlc"], cand["replica"], 1 if cand["deleted"] else 0, cand["text"])
        cur_key = (cur["hlc"], cur["replica"], 1 if cur["deleted"] else 0, cur["text"])
        if cand_key > cur_key:
            by_id[eid] = cand
    return sorted(by_id.values(), key=lambda x: (int(x["order_key"]), str(x["replica"]), str(x["elem_id"])))


def sync_crdt_note(
    note_rel_or_path: str | Path,
    *,
    local_state: list[dict[str, Any]],
    remote_state: list[dict[str, Any]],
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Require ADR-NOTES-CRDT + notes/-only containment, merge deterministically, and write note Markdown."""
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    require_adr_active("ADR-NOTES-CRDT", brain_dir=resolved_brain)
    target = _assert_notes_only_target(note_rel_or_path, resolved_brain)
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(target.parent)) or (os.path.lexists(str(target)) and _is_reparse_or_link(str(target))):
        raise PermissionError(f"Refusing to write CRDT note through symlinked path: {target}")

    merged = crdt_merge_note_lines(local_state, remote_state)
    visible_lines = [m["text"] for m in merged if not m.get("deleted")]
    content = "\n".join(visible_lines).rstrip() + "\n" if visible_lines else ""
    raw_bytes = content.encode("utf-8")
    atomic_write_bytes(str(target), raw_bytes)
    return {
        "status": "merged",
        "path": str(target),
        "element_count": len(merged),
        "visible_line_count": len(visible_lines),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "state": merged,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ADR-Gated Experimental Notes-Only CRDT Sync")
    parser.add_argument("note_name")
    parser.add_argument("--state-json", required=True)
    args = parser.parse_args(argv)
    payload = json.loads(Path(args.state_json).read_text(encoding="utf-8"))
    out = sync_crdt_note(args.note_name, local_state=payload.get("local", []), remote_state=payload.get("remote", []))
    print(json.dumps({k: v for k, v in out.items() if k != "state"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
