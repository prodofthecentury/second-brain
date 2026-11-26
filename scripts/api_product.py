# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Product routes for the loopback Memory API: notes, streaming chat, one-click review, import, governance.

Two credentials, two capability tiers:

* **Agent bearer token** (``Authorization: Bearer …``), held by IDE plugins and agents:
  read notes, recall, stream chat, and ``/v1/remember`` (risk-scanned agent capture).
* **Owner token** (``X-Owner-Token``), held only by the desktop app or CLI on this
  machine: create, edit, and delete notes, one-click verify, approve/reject/edit/merge atoms,
  triage the inbox, manage playbooks/hosts/snapshots, and import. An agent holding only the
  bearer token cannot grant trust, which preserves the rule that no model or agent can approve memory.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
import hmac
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote


from scripts.atom_mutator import (  # noqa: E402
    ReviewConflictError,
    ReviewTransitionError,
    apply_atom_edit,
    apply_atom_merge,
    apply_review_transition,
    approved_revision_is_current,
    archive_atom_to_cold,
    atomic_write_bytes,
    content_sha256,
    restore_cold_archived_atom,
    set_atom_temporal_bounds,
)
import scripts.config as _config  # noqa: E402
from scripts.config import (  # noqa: E402
    _is_reparse_or_link,
    is_content_markdown,
    normalize_markdown_filename,
    safe_file_in_dir,
)
from scripts.host_adapters import (  # noqa: E402
    HostConsentError,
    read_bounded_security_file,
    restrict_security_file_permissions,
)
from scripts.git_distiller import (  # noqa: E402
    GitDistillerError,
    GitDistillerSecurityError,
    distill_git_commits,
    get_git_hook_status,
    install_git_post_commit_hook,
    uninstall_git_post_commit_hook,
)
from scripts.inbox_drop import (  # noqa: E402
    InboxDropError,
    InboxDropSecurityError,
    create_mobile_drop_pairing,
    list_mobile_drop_pairings,
    resolve_scoped_inbox_drop_s3_credentials,
    revoke_mobile_drop_pairing,
)
from scripts.lexical_store import LexicalIndexUnavailable  # noqa: E402
from scripts.memory_format import (  # noqa: E402
    VALID_EPISTEMIC_LABELS,
    VALID_MEMORY_TYPES,
    evaluate_temporal_validity,
    frontmatter_has_key,
    frontmatter_value,
    normalize_temporal_bound_str,
    remove_frontmatter_fields,
    render_memory_document,
    set_scalar_field,
    set_string_list_field,
    split_frontmatter,
)
from scripts.memory_review import (  # noqa: E402
    list_review_queue,
    normalize_status,
    parse_atom_record,
    parse_frontmatter_values,
    parse_scalar_list,
    section_text,
)
from scripts.notes_store import (  # noqa: E402
    MAX_NOTE_BYTES,
    NoteError,
    NotePaths,
    backlinks,
    create_note,
    delete_note,
    get_note,
    list_notes,
    update_note,
    verify_note_excerpt,
)
from scripts.onnx_models import (  # noqa: E402
    OnnxModelError,
    OnnxModelSecurityError,
    get_onnx_models_status,
    pull_onnx_models,
    remove_onnx_models,
)
from scripts.recall import MemoryRecallUnavailable, normalize_project_tag, project_tags_match  # noqa: E402
from scripts.skill_playbooks import PlaybookValidationError  # noqa: E402
from scripts.vault_backup import (  # noqa: E402
    VaultBackupError,
    VaultBackupSecurityError,
    create_vault_backup,
    restore_vault_backup,
    verify_vault_backup,
)
from scripts.vault_sync import (  # noqa: E402
    SyncConflictError,
    SyncError,
    SyncSecurityError,
    configure_auto_sync,
    get_auto_sync_status,
    get_sync_conflict_detail,
    init_sync,
    join_sync,
    list_sync_conflicts,
    migrate_sync_key_to_keychain,
    pull_mobile_inbox_drops,
    resolve_sync_conflict,
    run_auto_sync_cycle,
    sync_gc,
    sync_pull,
    sync_push,
    sync_status,
    sync_verify,
)
from scripts.vault_workspaces import (  # noqa: E402
    VaultWorkspaceError,
    VaultWorkspaceSecurityError,
    create_vault,
    federated_recall,
    list_vaults,
    remove_vault,
    switch_vault,
    update_vault_policy,
)
from scripts.web_clipper import (  # noqa: E402
    ClipperError,
    ClipperSecurityError,
    create_clipper_pairing,
    create_clipper_pairing_code,
    ingest_clipper_media_bytes,
    ingest_web_clip,
    list_clipper_pairings,
    redeem_clipper_pairing_code,
    revoke_clipper_pairing,
)

from scripts.api_owner_auth import (
    CAPTURE_MAX_WRITES_PER_MIN,
    CAPTURE_WINDOW_SECONDS,
    MAX_SSE_SECONDS,
    MIN_OWNER_TOKEN_LEN,
    OWNER_HEADER,
    OWNER_LOCKOUT_SECONDS,
    OWNER_MAX_AUTH_FAILURES,
    _ATOM_SUMMARY_BY_SHA,
    _ATOM_SUMMARY_CACHE_LOCK,
    _ATOM_SUMMARY_CACHE_MAX,
    _ENV_VAR_NAME_RE,
    _JOBS,
    _JOBS_LOCK,
    _start_operator_job,
    check_capture_rate_limit,
    default_owner_token_path,
    ensure_owner_token,
    evaluate_request_owner_auth,
    get_owner_token,
    owner_authorized,
    owner_bearer_attestation,
    reset_owner_rate_limiter,
    verify_owner_token,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _paths(rt) -> NotePaths:
    return NotePaths.for_brain(rt.brain_dir)


def _tags_strictly_in_project_scope(tags: list[str] | tuple[str, ...], project: str | None) -> bool:
    """Enforce strict normalized multi-project containment (excluding unscoped & mixed-project items)."""
    if not project or not str(project).strip():
        return True
    scope_tag = normalize_project_tag(str(project))
    if not scope_tag:
        return True
    proj_tags = [str(t).strip() for t in (tags or ()) if str(t).strip().lower().startswith("project:")]
    return bool(proj_tags) and all(bool(project_tags_match((t,), (scope_tag,))) for t in proj_tags)


class _BadRequestError(ValueError):
    """Validation error mapped to HTTP 400 Bad Request inside api_product.dispatch()."""


_PROJECT_SCOPE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _optional_project_scope(val: object) -> str | None:
    """Validate and normalize an optional project:<slug> scope parameter.

    Returns None for None or blank input; raises _BadRequestError (a ValueError subclass
    mapped to HTTP 400) for malformed or path-traversal project scopes.
    """
    if val is None:
        return None
    if not isinstance(val, str):
        raise _BadRequestError("Invalid 'project' scope: must be a string.")
    raw = val.strip()
    if not raw:
        return None
    if len(raw) > 120 or "\x00" in raw or "\\" in raw or ".." in raw:
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    body = raw[len("project:") :] if raw.lower().startswith("project:") else raw
    if not body or body.startswith("/") or body.endswith("/") or "//" in body:
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    segments = body.split("/")
    if any(seg in {".", ".."} or not _PROJECT_SCOPE_SEGMENT_RE.fullmatch(seg) for seg in segments):
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    normalized = normalize_project_tag(raw)
    if not normalized or normalized == "project:":
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    return normalized


def _parse_bounded_int(
    q: dict[str, list[str]],
    key: str,
    *,
    default: int,
    min_val: int,
    max_val: int,
) -> int:
    vals = q.get(key)
    if not vals or not str(vals[0]).strip():
        return default
    raw = str(vals[0]).strip()
    if len(raw) > 12:
        raise ValueError(f"Invalid integer query parameter '{key}'.")
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer query parameter '{key}'.") from exc
    return max(min_val, min(parsed, max_val))


def _parse_json_int(
    body: dict,
    key: str,
    *,
    default: int | None = None,
    min_val: int | None = None,
    max_val: int | None = None,
    clamp: bool = False,
    allow_none: bool = False,
    error_cls: type[Exception] = ValueError,
) -> int | None:
    if key not in body:
        return default
    raw = body.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if allow_none:
            return None
        raise error_cls(f"'{key}' must be an integer.")
    if isinstance(raw, bool):
        raise error_cls(f"'{key}' must be an integer, not a boolean.")
    if isinstance(raw, int):
        parsed = int(raw)
    elif isinstance(raw, str):
        cleaned = raw.strip()
        if len(cleaned) > 18:
            raise error_cls(f"'{key}' must be a valid integer.")
        try:
            parsed = int(cleaned)
        except ValueError as exc:
            raise error_cls(f"'{key}' must be a valid integer.") from exc
    else:
        raise error_cls(f"'{key}' must be an integer.")
    if clamp:
        if min_val is not None:
            parsed = max(min_val, parsed)
        if max_val is not None:
            parsed = min(max_val, parsed)
    else:
        if min_val is not None and parsed < min_val:
            raise error_cls(f"'{key}' must be >= {min_val}.")
        if max_val is not None and parsed > max_val:
            raise error_cls(f"'{key}' must be <= {max_val}.")
    return parsed


def _parse_json_float(
    body: dict,
    key: str,
    *,
    default: float,
    error_cls: type[Exception] = ValueError,
) -> float:
    if key not in body:
        return default
    raw = body.get(key)
    if raw is None or isinstance(raw, bool):
        raise error_cls(f"'{key}' must be a finite number.")
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned or len(cleaned) > 32:
            raise error_cls(f"'{key}' must be a finite number.")
        try:
            val = float(cleaned)
        except ValueError as exc:
            raise error_cls(f"'{key}' must be a finite number.") from exc
    else:
        raise error_cls(f"'{key}' must be a finite number.")
    if not math.isfinite(val):
        raise error_cls(f"'{key}' must be a finite number.")
    return val


def _default_mobile_export_dir(rt: object) -> str:
    """Return a user-writable export directory outside read-only application bundles."""
    sec_dir = getattr(rt, "security_dir", None) or getattr(rt, "sync_state_dir", None)
    if sec_dir is not None and str(sec_dir).strip():
        base_state = Path(str(sec_dir)).expanduser().resolve()
    else:
        base_state = _config.security_state_dir().resolve()
    return str(base_state.parent / "exports" / "mobile-vault-client")


def _str(body: dict, key: str, *, max_len: int, required: bool = True, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string.")
    if required and not value.strip():
        raise ValueError(f"'{key}' is required.")
    if len(value) > max_len:
        raise ValueError(f"'{key}' exceeds {max_len} characters.")
    return value


def _tags(body: dict) -> list[str]:
    tags = body.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 32 or any(not isinstance(t, str) or len(t) > 64 for t in tags):
        raise ValueError("'tags' must be a list of up to 32 short strings.")
    return tags


def _str_list(body: dict, key: str, *, max_items: int = 32, max_item_len: int = 1000, required: bool = False) -> list[str]:
    val = body.get(key, [])
    if val is None:
        val = []
    if not isinstance(val, list) or len(val) > max_items:
        raise ValueError(f"'{key}' must be a list of up to {max_items} strings.")
    out: list[str] = []
    for item in val:
        if not isinstance(item, str) or len(item) > max_item_len:
            raise ValueError(f"Each entry in '{key}' must be a string up to {max_item_len} characters.")
        if item.strip():
            out.append(item.strip())
    if required and not out:
        raise ValueError(f"'{key}' requires at least one non-empty item.")
    return out


def is_preference_atom_content(content: str) -> bool:
    parsed = split_frontmatter(content)
    if not parsed:
        return False
    fm_vals = parse_frontmatter_values(parsed[0])
    label = str(fm_vals.get("epistemic_label") or "").strip().lower()
    contract = str(fm_vals.get("epistemic_contract") or "").strip()
    return label == "preference" or bool(contract)


def _is_preference_atom_file(path: str) -> bool:
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return is_preference_atom_content(raw_text)


def _atom_card(path: str, rt, *, as_of: str | None = None) -> dict[str, object]:
    rec = parse_atom_record(path)
    raw = Path(path).read_bytes()
    raw_text = raw.decode("utf-8")
    parsed = split_frontmatter(raw_text)
    fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
    evidence = section_text("\n".join(parsed[1]), "Evidence") if parsed else ""
    epistemic_label = str(fm_vals.get("epistemic_label") or rec.get("epistemic_label") or "claim").strip() or "claim"
    epistemic_contract = str(fm_vals.get("epistemic_contract") or rec.get("epistemic_contract") or "").strip()
    valid_from = str(rec.get("valid_from") or fm_vals.get("valid_from") or "").strip()
    valid_until = str(rec.get("valid_until") or fm_vals.get("valid_until") or "").strip()
    tv = evaluate_temporal_validity(valid_from, valid_until, as_of=as_of)
    return {
        "filename": rec["filename"],
        "claim": rec["claim"],
        "evidence": evidence,
        "review_status": rec["review_status"],
        "status": rec["review_status"],
        "memory_type": rec["memory_type"],
        "epistemic_label": "preference" if (epistemic_label == "preference" or epistemic_contract) else epistemic_label,
        "epistemic_contract": epistemic_contract or None,
        "contexts": list(rec.get("contexts") or []),
        "origin": rec["origin"],
        "sources": rec["sources"],
        "valid_from": valid_from,
        "valid_until": valid_until,
        "temporal_status": str(tv["status"]),
        "sha256": content_sha256(raw),
        "verified": approved_revision_is_current(path, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir),
    }


def _safe_atom_card(path: str, rt, fallback: dict | None = None) -> dict[str, object] | None:
    try:
        return _atom_card(path, rt)
    except (OSError, UnicodeDecodeError, ValueError):
        if fallback is None:
            return None
        status = str(fallback.get("review_status") or "needs_edit")
        return {
            "filename": str(fallback.get("filename") or os.path.basename(path)),
            "claim": str(fallback.get("claim") or ""),
            "evidence": "",
            "review_status": status,
            "status": status,
            "memory_type": str(fallback.get("memory_type") or ""),
            "epistemic_label": str(fallback.get("epistemic_label") or "claim"),
            "epistemic_contract": fallback.get("epistemic_contract") or None,
            "contexts": list(fallback.get("contexts") or []),
            "origin": str(fallback.get("origin") or ""),
            "sources": list(fallback.get("sources") or []),
            "sha256": "",
            "verified": False,
        }


def _sync_lexical_after_atom_mutation(rt, filenames: list[str]) -> None:
    """Keep the FTS5 lexical sidecar's SHA-256 manifest synchronized after API atom mutations."""
    idx_path = getattr(rt, "lexical_index_path", None)
    if not idx_path or not os.path.isfile(idx_path):
        return
    try:
        from scripts.atom_mutator import review_lock
        from scripts.lexical_store import upsert_lexical_atoms

        lock_path = os.path.join(os.path.dirname(os.path.abspath(idx_path)), "writer.lock")
        with review_lock(lock_path):
            upsert_lexical_atoms(rt.atoms_dir, idx_path, [f for f in filenames if f])
    except Exception:
        pass


def _replace_markdown_section(body_lines: list[str], section_name: str, new_text: str, *, before_section: str | None = None) -> list[str]:
    clean_text = new_text.strip()
    if re.search(r"(?m)^##\s+", clean_text):
        raise ValueError(f"Section '{section_name}' text cannot inject secondary '##' headings.")
    header = f"## {section_name}"
    target_lower = header.lower()
    before_lower = f"## {before_section}".lower() if before_section else None

    sections: list[tuple[str | None, list[str]]] = []
    cur_header: str | None = None
    cur_lines: list[str] = []
    for line in body_lines:
        if line.strip().startswith("## "):
            sections.append((cur_header, cur_lines))
            cur_header = line.strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    sections.append((cur_header, cur_lines))

    replaced = False
    rebuilt: list[str] = []
    replacement_lines = [clean_text, ""] if clean_text else [""]

    for hdr, lines in sections:
        if hdr is not None and hdr.lower() == target_lower:
            if not replaced:
                rebuilt.append(header)
                rebuilt.extend(replacement_lines)
                replaced = True
            continue
        if not replaced and before_lower and hdr is not None and hdr.lower() == before_lower:
            rebuilt.append(header)
            rebuilt.extend(replacement_lines)
            replaced = True
        if hdr is not None:
            rebuilt.append(hdr)
        rebuilt.extend(lines)

    if not replaced:
        if rebuilt and rebuilt[-1].strip():
            rebuilt.append("")
        rebuilt.append(header)
        rebuilt.extend(replacement_lines)
    return rebuilt


def _apply_temporal_overrides_to_frontmatter(fm: list[str], body: dict) -> list[str]:
    if "valid_from" in body:
        raw_vf = _str(body, "valid_from", max_len=64, required=False).strip()
        norm_vf = normalize_temporal_bound_str(raw_vf) if raw_vf else ""
        if norm_vf:
            fm = set_scalar_field(fm, "valid_from", norm_vf, "last-reviewed")
        else:
            fm = remove_frontmatter_fields(fm, {"valid_from"})

    if "valid_until" in body:
        raw_vu = _str(body, "valid_until", max_len=64, required=False).strip()
        norm_vu = normalize_temporal_bound_str(raw_vu) if raw_vu else ""
        if norm_vu:
            after_key = "valid_from" if frontmatter_has_key(fm, "valid_from") else "last-reviewed"
            fm = set_scalar_field(fm, "valid_until", norm_vu, after_key)
        else:
            fm = remove_frontmatter_fields(fm, {"valid_until"})

    eff_vf = frontmatter_value(fm, "valid_from")
    eff_vu = frontmatter_value(fm, "valid_until")
    if eff_vf or eff_vu:
        tv = evaluate_temporal_validity(eff_vf, eff_vu)
        if tv["status"] == "invalid_window":
            raise ValueError("Invalid bi-temporal validity window: valid_from must be <= valid_until.")
    return fm


def _apply_temporal_overrides_to_markdown(raw_md: str, body: dict) -> str:
    parsed = split_frontmatter(raw_md)
    if parsed is None:
        if "valid_from" in body or "valid_until" in body:
            raise ValueError("Target atom is missing valid YAML frontmatter.")
        return raw_md
    fm, body_lines = list(parsed[0]), list(parsed[1])
    fm = _apply_temporal_overrides_to_frontmatter(fm, body)
    if "valid_from" in body or "valid_until" in body:
        return render_memory_document(fm, body_lines)
    return raw_md


def _synthesize_edited_atom_markdown(atom_path: str, body: dict) -> str:
    raw_text = Path(atom_path).read_text(encoding="utf-8")
    parsed = split_frontmatter(raw_text)
    if parsed is None:
        raise ValueError("Target atom is missing valid YAML frontmatter.")
    fm, body_lines = list(parsed[0]), list(parsed[1])

    if body.get("memory_type") is not None:
        mtype = _str(body, "memory_type", max_len=32)
        if mtype not in VALID_MEMORY_TYPES:
            raise ValueError(f"Invalid memory_type {mtype!r}; expected one of {sorted(VALID_MEMORY_TYPES)}.")
        fm = set_scalar_field(fm, "memory_type", mtype, "type")

    if body.get("epistemic_label") is not None:
        elabel = _str(body, "epistemic_label", max_len=32)
        if elabel not in VALID_EPISTEMIC_LABELS:
            raise ValueError(f"Invalid epistemic_label {elabel!r}; expected one of {sorted(VALID_EPISTEMIC_LABELS)}.")
        fm = set_scalar_field(fm, "epistemic_label", elabel, "claim_state")

    if "contexts" in body and body.get("contexts") is not None:
        ctxs = _str_list(body, "contexts", max_items=32, max_item_len=120)
        fm = set_string_list_field(fm, "contexts", ctxs, "tags")

    fm = _apply_temporal_overrides_to_frontmatter(fm, body)

    if body.get("claim") is not None:
        claim_val = _str(body, "claim", max_len=8000)
        body_lines = _replace_markdown_section(body_lines, "Claim", claim_val, before_section="Evidence")

    if body.get("why_it_matters") is not None:
        why_val = _str(body, "why_it_matters", max_len=8000, required=False)
        if why_val.strip():
            body_lines = _replace_markdown_section(body_lines, "Why It Matters", why_val, before_section="Evidence")

    return render_memory_document(fm, body_lines)


def _parse_backing_atoms_payload(raw_val: object) -> list[tuple[str, str]]:
    if not isinstance(raw_val, list) or not raw_val or len(raw_val) > 32:
        raise ValueError("'backing_atoms' must be a non-empty list of [atom_filename, expected_sha256] pairs.")
    pairs: list[tuple[str, str]] = []
    for entry in raw_val:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            fn, sha = entry[0], entry[1]
        elif isinstance(entry, dict):
            fn = entry.get("filename") or entry.get("atom_id") or entry.get("atom")
            sha = entry.get("sha256") or entry.get("expected_sha256")
        elif isinstance(entry, str) and ":" in entry:
            fn, sha = entry.split(":", 1)
        else:
            raise ValueError("Invalid 'backing_atoms' entry; expected [filename, sha256] or {filename, sha256}.")
        if not isinstance(fn, str) or not isinstance(sha, str) or not fn.strip() or not sha.strip():
            raise ValueError("Each backing atom requires non-empty filename and sha256 strings.")
        pairs.append((normalize_markdown_filename(fn.strip()), sha.strip()))
    return pairs


def _atom_summary_from_indexed_row(filename: str, content: str, sha256: str) -> dict[str, object] | None:
    """Parse static atom card metadata from FTS5 row content once per (filename, content_sha256)."""
    cache_key = f"{filename}:{sha256}"
    with _ATOM_SUMMARY_CACHE_LOCK:
        cached = _ATOM_SUMMARY_BY_SHA.get(cache_key)
    if cached is not None:
        tv_cached = evaluate_temporal_validity(
            str(cached.get("valid_from") or ""),
            str(cached.get("valid_until") or ""),
        )
        return {
            **cached,
            "temporal_status": str(tv_cached["status"]),
            "contexts": list(cached["contexts"]),  # type: ignore[arg-type]
            "sources": list(cached["sources"]),  # type: ignore[arg-type]
        }

    parsed = split_frontmatter(content)
    if parsed is None:
        return None
    fm_lines, body_lines = parsed
    fm_vals = parse_frontmatter_values(fm_lines)
    body_text = "\n".join(body_lines)

    raw_sources = fm_vals.get("sources", [])
    sources = parse_scalar_list(raw_sources) if isinstance(raw_sources, str) else list(raw_sources)
    raw_contexts = fm_vals.get("contexts", [])
    contexts = parse_scalar_list(raw_contexts) if isinstance(raw_contexts, str) else list(raw_contexts)

    review_status = normalize_status(str(fm_vals.get("review_status", "pending")))
    memory_type = str(fm_vals.get("memory_type", "semantic") or "semantic")
    origin = str(fm_vals.get("origin", "imported_unknown") or "imported_unknown")
    epistemic_label_raw = str(fm_vals.get("epistemic_label") or "claim").strip() or "claim"
    epistemic_contract = str(fm_vals.get("epistemic_contract") or "").strip()
    effective_label = (
        "preference" if (epistemic_label_raw == "preference" or epistemic_contract) else epistemic_label_raw
    )
    valid_from = str(fm_vals.get("valid_from") or "").strip()
    valid_until = str(fm_vals.get("valid_until") or "").strip()
    summary: dict[str, object] = {
        "filename": filename,
        "claim": section_text(body_text, "Claim"),
        "evidence": section_text(body_text, "Evidence"),
        "review_status": review_status,
        "status": review_status,
        "memory_type": memory_type,
        "epistemic_label": effective_label,
        "epistemic_contract": epistemic_contract or None,
        "contexts": list(contexts),
        "origin": origin,
        "sources": list(sources),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "sha256": sha256,
    }
    with _ATOM_SUMMARY_CACHE_LOCK:
        if len(_ATOM_SUMMARY_BY_SHA) >= _ATOM_SUMMARY_CACHE_MAX and cache_key not in _ATOM_SUMMARY_BY_SHA:
            _ATOM_SUMMARY_BY_SHA.pop(next(iter(_ATOM_SUMMARY_BY_SHA)), None)
        _ATOM_SUMMARY_BY_SHA[cache_key] = summary
    tv_live = evaluate_temporal_validity(valid_from, valid_until)
    return {
        **summary,
        "temporal_status": str(tv_live["status"]),
        "contexts": list(contexts),
        "sources": list(sources),
    }


def _live_approved_atom_sha(fname: object, rt) -> str | None:
    """Return live content_sha256 of rt.atoms_dir/<fname> iff current, non-symlink, non-preference, and approved."""
    raw_name = str(fname or "").strip()
    if not raw_name:
        return None
    ap = safe_file_in_dir(rt.atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
    if not ap or _is_reparse_or_link(ap) or not os.path.isfile(ap):
        return None
    try:
        raw_bytes = Path(ap).read_bytes()
        raw_text = raw_bytes.decode("utf-8")
        rec = parse_atom_record(ap)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if is_preference_atom_content(raw_text):
        return None
    if rec.get("review_status") != "accepted" or bool(rec.get("superseded_by")):
        return None
    tv_live_atom = evaluate_temporal_validity(
        str(rec.get("valid_from") or "").strip(),
        str(rec.get("valid_until") or "").strip(),
    )
    if not tv_live_atom["active"] or tv_live_atom["status"] != "active":
        return None
    if not approved_revision_is_current(ap, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir):
        return None
    return content_sha256(raw_bytes)


def _is_live_preference_atom(fname: object, rt) -> bool:
    raw_name = str(fname or "").strip()
    if not raw_name:
        return False
    ap = safe_file_in_dir(rt.atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
    if not ap or _is_reparse_or_link(ap) or not os.path.isfile(ap):
        return False
    return _is_preference_atom_file(ap)


def _live_active_playbook_report(fname: object, rt):
    """Verify live Playbook integrity and return PlaybookIntegrityReport iff valid and active."""
    from scripts.skill_playbooks import verify_playbook_integrity

    raw_name = str(fname or "").strip()
    if not raw_name:
        return None
    pb_path = safe_file_in_dir(rt.playbooks_dir, raw_name, allowed_exts={".md"}, must_exist=True)
    if not pb_path or _is_reparse_or_link(pb_path) or not os.path.isfile(pb_path):
        return None
    try:
        rep = verify_playbook_integrity(
            pb_path,
            brain_dir=rt.brain_dir,
            atoms_dir=rt.atoms_dir,
            playbooks_dir=rt.playbooks_dir,
            telemetry_db_path=rt.telemetry_db_path,
        )
    except Exception:
        return None
    if not rep.valid or rep.status != "active":
        return None
    try:
        disk_sha = content_sha256(Path(pb_path).read_bytes())
    except OSError:
        return None
    if not rep.sha256 or disk_sha != rep.sha256:
        return None
    return rep


def _prove_leaf_fallback(
    leaf_ref: str,
    *,
    brain_dir: str,
    atoms_dir: str,
    playbooks_dir: str,
) -> dict[str, object]:
    import scripts.vault_ledger as vault_ledger

    parts = [p for p in leaf_ref.split("/") if p]
    candidates: list[str] = []
    if len(parts) == 2:
        prefix, raw_name = parts[0].lower(), normalize_markdown_filename(parts[1])
        if prefix in {"2-atoms", "atoms"}:
            ap = safe_file_in_dir(atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
            if not ap:
                raise FileNotFoundError(f"Atom not found: {leaf_ref}")
            candidates.append(f"2-atoms/{os.path.basename(ap)}")
        elif prefix in {"5-playbooks", "playbooks"}:
            pp = safe_file_in_dir(playbooks_dir, raw_name, allowed_exts={".md"}, must_exist=True)
            if not pp:
                raise FileNotFoundError(f"Playbook not found: {leaf_ref}")
            candidates.append(f"5-playbooks/{os.path.basename(pp)}")
        elif prefix in {"6-skills", "skills"}:
            skills_dir = os.path.join(brain_dir, "6-skills")
            sp = safe_file_in_dir(skills_dir, raw_name, allowed_exts={".md"}, must_exist=True)
            if not sp:
                raise FileNotFoundError(f"Executable skill not found: {leaf_ref}")
            candidates.append(f"6-skills/{os.path.basename(sp)}")
        else:
            raise FileNotFoundError(f"Unknown vault leaf prefix: {leaf_ref}")
    elif len(parts) == 1:
        raw_name = normalize_markdown_filename(parts[0])
        skills_dir = os.path.join(brain_dir, "6-skills")
        ap = safe_file_in_dir(atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
        pp = safe_file_in_dir(playbooks_dir, raw_name, allowed_exts={".md"}, must_exist=True)
        sp = safe_file_in_dir(skills_dir, raw_name, allowed_exts={".md"}, must_exist=True) if os.path.isdir(skills_dir) else None
        if ap:
            candidates.append(f"2-atoms/{os.path.basename(ap)}")
        if pp:
            candidates.append(f"5-playbooks/{os.path.basename(pp)}")
        if sp:
            candidates.append(f"6-skills/{os.path.basename(sp)}")
        if not candidates:
            raise FileNotFoundError(f"Leaf not found: {leaf_ref}")
    else:
        raise FileNotFoundError(f"Leaf not found: {leaf_ref}")

    merkle = vault_ledger.compute_vault_merkle(
        brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir
    )
    leaves = merkle["leaves"]
    idx = None
    target_rel = candidates[0]
    for cand in candidates:
        found = next((i for i, leaf in enumerate(leaves) if leaf["rel_path"] == cand), None)
        if found is not None:
            idx = found
            target_rel = cand
            break
    if idx is None:
        raise PermissionError(f"Target '{leaf_ref}' is not an accepted member of the verified vault Merkle tree.")

    levels = vault_ledger._build_merkle_levels([leaf["leaf_sha256"] for leaf in leaves])
    siblings: list[dict[str, str]] = []
    cur_idx = idx
    for level in levels[:-1]:
        if cur_idx % 2 == 0:
            if cur_idx + 1 < len(level):
                siblings.append({"position": "right", "sha256": level[cur_idx + 1]})
            else:
                siblings.append({"position": "odd", "sha256": level[cur_idx]})
        else:
            siblings.append({"position": "left", "sha256": level[cur_idx - 1]})
        cur_idx //= 2

    ledger = vault_ledger.verify_vault_ledger(
        brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir
    )
    return {
        "schema_version": 1,
        "rel_path": target_rel,
        "file_sha256": leaves[idx]["file_sha256"],
        "leaf_sha256": leaves[idx]["leaf_sha256"],
        "siblings": siblings,
        "merkle_root": merkle["merkle_root"],
        "ledger_head_entry_sha256": ledger["head_entry_sha256"] if ledger["synced_with_head"] else None,
    }


from scripts.api_sse import stream_sse

import scripts.api_dispatch as _api_dispatch

_api_dispatch.install(globals())
