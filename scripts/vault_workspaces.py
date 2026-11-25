# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Multi-Vault Workspaces & Access Profiles for Second Brain (v0.3.0 Milestone 3).

Security & isolation invariants:
1. Named vault registry persists at `SecondBrain/security/vaults.json` (`0600`/NTFS DACL)
   with atomic writes and strict symlink/reparse rejection.
2. Switching vaults atomically updates `vaults.json` AND `SecondBrain/security/vault-path.txt`
   so CLI, MCP, Dashboard, Desktop, and VS Code stay 100% synchronized.
3. Strict isolation by default: each vault maintains its own `writer.lock`,
   `decision-record.jsonl`, `vault-ledger.jsonl`, `notes/`, and `2-atoms/`.
   Vaults are never nested inside one another, and optional per-vault bearer tokens
   (`vb_*` stored under `SecondBrain/security/vault-tokens/<id>.token`) cannot access
   any other vault.
4. Opt-in read-only federated search: every vault defaults to `federated_searchable: false`.
   Cross-vault search requires explicit owner invocation AND skips (or rejects if
   explicitly targeted) any vault that has not been opted into federated search, while
   preserving every vault's independent approval, source-anchor, and preference filters.
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
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import threading


from scripts.atom_mutator import review_lock  # noqa: E402
from scripts.config import (  # noqa: E402
    _is_immutable_darwin_root_symlink,
    _is_reparse_or_link,
    ensure_dirs,
    is_content_markdown,
    resolve_active_brain_dir,
    security_state_dir,
    vault_paths_for,
)
from scripts.host_adapters import restrict_security_file_permissions  # noqa: E402
from scripts.cross_encoder import score_query_atom_pairs  # noqa: E402
from scripts.lexical_store import lexical_index_is_current, rebuild_lexical_index  # noqa: E402
from scripts.memory_format import split_frontmatter  # noqa: E402
from scripts.memory_review import parse_frontmatter_values  # noqa: E402
from scripts.recall import RecallOptions, search_memory  # noqa: E402


VAULTS_REGISTRY_FILENAME = "vaults.json"
VAULT_TOKENS_SUBDIR = "vault-tokens"
VAULT_TOKEN_PREFIX = "vb_"
ALLOWED_HOST_POLICIES = frozenset({"local_only", "online_opt_in"})
MAX_VAULTS = 32
MAX_REGISTRY_BYTES = 262_144

_VAULT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_VAULTS_LOCK = threading.Lock()


class VaultWorkspaceError(ValueError):
    """Raised when a vault workspace operation violates schema or business rules."""


class VaultWorkspaceSecurityError(PermissionError):
    """Raised when a vault isolation, symlink, or token security invariant is violated."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sha256_str(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sec_dir(security_dir: str | Path | None = None) -> Path:
    return Path(security_dir).expanduser().resolve() if security_dir is not None else security_state_dir()


def vaults_registry_path(security_dir: str | Path | None = None) -> Path:
    return _sec_dir(security_dir) / VAULTS_REGISTRY_FILENAME


def _vault_path_file(security_dir: str | Path | None = None) -> Path:
    return _sec_dir(security_dir) / "vault-path.txt"


def _vault_tokens_dir(security_dir: str | Path | None = None) -> Path:
    return _sec_dir(security_dir) / VAULT_TOKENS_SUBDIR


def _slugify_vault_id(raw: str) -> str:
    slug = _SLUG_RE.sub("-", str(raw or "").strip().lower()).strip("-")[:48]
    if not slug or not _VAULT_ID_RE.fullmatch(slug):
        raise VaultWorkspaceError(
            f"Invalid vault_id {raw!r}; use 1-48 lowercase letters, digits, hyphens, or underscores."
        )
    return slug


def _assert_no_symlink_ancestors(target: str) -> None:
    cur = target
    visited: set[str] = set()
    while cur and cur not in visited:
        visited.add(cur)
        if os.path.lexists(cur) and _is_reparse_or_link(cur):
            if not _is_immutable_darwin_root_symlink(cur):
                raise VaultWorkspaceSecurityError(f"Refusing to use vault path with symlinked or junctioned component: {cur}")
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent


def _norm_abs_path(path: str | Path) -> str:
    raw = str(path or "").strip()
    if not raw or "\x00" in raw or "\r" in raw or "\n" in raw:
        raise VaultWorkspaceError("Vault path must be a non-empty single-line filesystem path.")
    resolved = os.path.abspath(os.path.expanduser(raw))
    _assert_no_symlink_ancestors(resolved)
    return resolved


def _paths_overlap(path_a: str, path_b: str) -> bool:
    a = os.path.normcase(os.path.realpath(os.path.abspath(path_a)))
    b = os.path.normcase(os.path.realpath(os.path.abspath(path_b)))
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        return False
    return common == a or common == b


def _atomic_write_secure_file(target: Path, content: bytes, prefix: str) -> None:
    if os.path.lexists(str(target.parent)) and _is_reparse_or_link(str(target.parent)):
        raise VaultWorkspaceSecurityError(f"Refusing to write under symlinked directory: {target.parent}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_or_link(str(target.parent)) or (os.path.lexists(str(target)) and _is_reparse_or_link(str(target))):
        raise VaultWorkspaceSecurityError(f"Refusing to write through a symlink or reparse point: {target}")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=prefix, suffix=".tmp")
    fd_closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        if os.write(fd, content) != len(content):
            raise OSError(f"Incomplete write to {target.name}.")
        os.fsync(fd)
        os.close(fd)
        fd_closed = True
        os.replace(tmp_name, target)
        restrict_security_file_permissions(target)
    finally:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


def _write_active_vault_pointer(vault_dir: str, *, security_dir: str | Path | None = None) -> Path:
    vp_file = _vault_path_file(security_dir)
    _atomic_write_secure_file(vp_file, (os.path.abspath(vault_dir) + "\n").encode("utf-8"), ".vault-path-")
    return vp_file


def _default_bootstrap_registry(active_path: str | None = None) -> dict[str, object]:
    base_dir = os.path.abspath(active_path) if active_path else resolve_active_brain_dir()
    return {
        "schema_version": 1,
        "active_vault_id": "default",
        "vaults": [
            {
                "vault_id": "default",
                "label": "Default Vault",
                "path": base_dir,
                "created_at": _now_iso(),
                "default_host_policy": "local_only",
                "federated_searchable": False,
                "isolated_token_sha256": None,
                "isolated_token_file": None,
            }
        ],
    }


def _load_registry_unlocked(
    *,
    security_dir: str | Path | None = None,
    default_active_path: str | None = None,
) -> dict[str, object]:
    reg_path = vaults_registry_path(security_dir)
    if os.path.lexists(str(reg_path.parent)) and _is_reparse_or_link(str(reg_path.parent)):
        raise VaultWorkspaceSecurityError(f"Refusing to read vaults registry under symlinked dir: {reg_path.parent}")
    if not os.path.lexists(str(reg_path)):
        return _default_bootstrap_registry(default_active_path)
    if _is_reparse_or_link(str(reg_path)):
        raise VaultWorkspaceSecurityError(f"Refusing to read symlinked vaults registry: {reg_path}")
    st = os.lstat(str(reg_path))
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REGISTRY_BYTES:
        raise VaultWorkspaceSecurityError("Vaults registry is irregular or exceeds maximum allowed size.")
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("vaults"), list):
        raise VaultWorkspaceSecurityError("Malformed vaults registry.")
    return data


def _save_registry_unlocked(data: dict[str, object], *, security_dir: str | Path | None = None) -> None:
    reg_path = vaults_registry_path(security_dir)
    encoded = (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_secure_file(reg_path, encoded, ".vaults-")


def _vault_quick_stats(vault_path: str) -> dict[str, object]:
    exists = os.path.isdir(vault_path) and not _is_reparse_or_link(vault_path)
    if not exists:
        return {"exists": False, "atom_count": 0, "note_count": 0}
    vp = vault_paths_for(vault_path)
    atoms_dir = vp["atoms_dir"]
    notes_dir = vp["notes_dir"]
    atom_count = (
        sum(1 for f in os.listdir(atoms_dir) if is_content_markdown(f))
        if (os.path.isdir(atoms_dir) and not _is_reparse_or_link(atoms_dir))
        else 0
    )
    note_count = (
        sum(1 for f in os.listdir(notes_dir) if is_content_markdown(f))
        if (os.path.isdir(notes_dir) and not _is_reparse_or_link(notes_dir))
        else 0
    )
    return {"exists": True, "atom_count": atom_count, "note_count": note_count}


def _public_vault_view(item: dict[str, object], active_id: str) -> dict[str, object]:
    vid = str(item.get("vault_id") or "")
    vpath = str(item.get("path") or "")
    stats = _vault_quick_stats(vpath)
    return {
        "vault_id": vid,
        "label": str(item.get("label") or vid),
        "path": vpath,
        "created_at": str(item.get("created_at") or ""),
        "default_host_policy": str(item.get("default_host_policy") or "local_only"),
        "federated_searchable": bool(item.get("federated_searchable", False)),
        "has_isolated_token": bool(item.get("isolated_token_sha256")),
        "isolated_token_file": str(item["isolated_token_file"]) if item.get("isolated_token_file") else None,
        "active": vid == active_id,
        **stats,
    }


def list_vaults(
    *,
    security_dir: str | Path | None = None,
    current_brain_dir: str | None = None,
) -> dict[str, object]:
    """List all registered vaults and identify the currently active vault."""
    with _VAULTS_LOCK:
        data = _load_registry_unlocked(security_dir=security_dir, default_active_path=current_brain_dir)
        active_id = str(data.get("active_vault_id") or "default")
        raw_vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]
        # If current_brain_dir matches another registered vault, reflect live active truth.
        if current_brain_dir:
            norm_cur = os.path.normcase(os.path.abspath(current_brain_dir))
            for item in raw_vaults:
                if os.path.normcase(os.path.abspath(str(item.get("path") or ""))) == norm_cur:
                    active_id = str(item.get("vault_id") or active_id)
                    break
        views = [_public_vault_view(v, active_id) for v in raw_vaults]
        active_entry = next((v for v in views if v["active"]), views[0] if views else None)
    return {
        "active_vault_id": active_entry["vault_id"] if active_entry else active_id,
        "active_vault_path": active_entry["path"] if active_entry else (current_brain_dir or resolve_active_brain_dir()),
        "count": len(views),
        "vaults": views,
    }


def create_vault(
    label: str,
    path: str | Path,
    *,
    vault_id: str | None = None,
    isolated_token: bool = False,
    default_host_policy: str = "local_only",
    federated_searchable: bool = False,
    activate: bool = False,
    security_dir: str | Path | None = None,
    current_brain_dir: str | None = None,
) -> dict[str, object]:
    """Create and initialize a new isolated vault workspace."""
    clean_label = re.sub(r"\s+", " ", str(label or "")).strip()[:80]
    if not clean_label:
        raise VaultWorkspaceError("Vault label is required.")
    policy = str(default_host_policy or "local_only").strip().lower()
    if policy not in ALLOWED_HOST_POLICIES:
        raise VaultWorkspaceError(f"default_host_policy must be one of {sorted(ALLOWED_HOST_POLICIES)}.")
    vid = _slugify_vault_id(vault_id if vault_id else clean_label)
    abs_path = _norm_abs_path(path)

    sec_root = str(_sec_dir(security_dir))
    if _paths_overlap(abs_path, sec_root):
        raise VaultWorkspaceSecurityError("Vault directory must not overlap the SecondBrain security state directory.")

    with _VAULTS_LOCK:
        data = _load_registry_unlocked(security_dir=security_dir, default_active_path=current_brain_dir)
        vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]
        if len(vaults) >= MAX_VAULTS:
            raise VaultWorkspaceError(f"Maximum vault workspaces ({MAX_VAULTS}) reached.")
        for existing in vaults:
            if str(existing.get("vault_id") or "") == vid:
                raise VaultWorkspaceError(f"Vault ID '{vid}' already exists.")
            ex_path = str(existing.get("path") or "")
            if ex_path and _paths_overlap(abs_path, ex_path):
                raise VaultWorkspaceError(
                    f"Vault path '{abs_path}' overlaps existing vault '{existing.get('vault_id')}' ({ex_path}); vaults must not nest."
                )

        # Initialize directory structure and empty FTS5 index so the vault is immediately usable.
        ensure_dirs(abs_path)
        vp = vault_paths_for(abs_path)
        os.makedirs(vp["sidecar_dir"], exist_ok=True)
        with review_lock(os.path.join(vp["sidecar_dir"], "writer.lock")):
            if not lexical_index_is_current(vp["atoms_dir"], vp["lexical_index_path"]):
                rebuild_lexical_index(vp["atoms_dir"], vp["lexical_index_path"])

        raw_vault_token: str | None = None
        token_sha: str | None = None
        token_file_str: str | None = None
        if isolated_token:
            raw_vault_token = VAULT_TOKEN_PREFIX + secrets.token_urlsafe(32)
            token_sha = _sha256_str(raw_vault_token)
            tok_file = _vault_tokens_dir(security_dir) / f"{vid}.token"
            _atomic_write_secure_file(tok_file, (raw_vault_token + "\n").encode("utf-8"), f".vtok-{vid}-")
            token_file_str = str(tok_file)

        entry: dict[str, object] = {
            "vault_id": vid,
            "label": clean_label,
            "path": abs_path,
            "created_at": _now_iso(),
            "default_host_policy": policy,
            "federated_searchable": bool(federated_searchable),
            "isolated_token_sha256": token_sha,
            "isolated_token_file": token_file_str,
        }
        vaults.append(entry)
        active_id = vid if activate else str(data.get("active_vault_id") or "default")
        data["vaults"] = vaults
        data["active_vault_id"] = active_id
        _save_registry_unlocked(data, security_dir=security_dir)
        if activate:
            _write_active_vault_pointer(abs_path, security_dir=security_dir)

    result = _public_vault_view(entry, active_id)
    if raw_vault_token is not None:
        result["vault_token"] = raw_vault_token
    return result


def switch_vault(
    vault_id: str,
    *,
    security_dir: str | Path | None = None,
    current_brain_dir: str | None = None,
) -> dict[str, object]:
    """Switch the active vault atomically and synchronize vault-path.txt."""
    vid = _slugify_vault_id(vault_id)
    with _VAULTS_LOCK:
        data = _load_registry_unlocked(security_dir=security_dir, default_active_path=current_brain_dir)
        vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]
        target = next((v for v in vaults if str(v.get("vault_id") or "") == vid), None)
        if target is None:
            raise FileNotFoundError(f"Vault not found: {vid}")
        target_path = _norm_abs_path(str(target.get("path") or ""))
        ensure_dirs(target_path)
        vp = vault_paths_for(target_path)
        os.makedirs(vp["sidecar_dir"], exist_ok=True)
        with review_lock(os.path.join(vp["sidecar_dir"], "writer.lock")):
            if not lexical_index_is_current(vp["atoms_dir"], vp["lexical_index_path"]):
                rebuild_lexical_index(vp["atoms_dir"], vp["lexical_index_path"])
        data["active_vault_id"] = vid
        _save_registry_unlocked(data, security_dir=security_dir)
        pointer_path = _write_active_vault_pointer(target_path, security_dir=security_dir)
        view = _public_vault_view(target, vid)
    return {
        "status": "switched",
        "active_vault_id": vid,
        "active_vault_path": target_path,
        "vault_path_file": str(pointer_path),
        "vault": view,
    }


def update_vault_policy(
    vault_id: str,
    *,
    label: str | None = None,
    default_host_policy: str | None = None,
    federated_searchable: bool | None = None,
    security_dir: str | Path | None = None,
    current_brain_dir: str | None = None,
) -> dict[str, object]:
    """Update label, host policy, or opt-in federated search setting for a registered vault."""
    vid = _slugify_vault_id(vault_id)
    with _VAULTS_LOCK:
        data = _load_registry_unlocked(security_dir=security_dir, default_active_path=current_brain_dir)
        vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]
        target = next((v for v in vaults if str(v.get("vault_id") or "") == vid), None)
        if target is None:
            raise FileNotFoundError(f"Vault not found: {vid}")
        if label is not None:
            clean_label = re.sub(r"\s+", " ", str(label)).strip()[:80]
            if not clean_label:
                raise VaultWorkspaceError("Vault label cannot be empty.")
            target["label"] = clean_label
        if default_host_policy is not None:
            pol = str(default_host_policy).strip().lower()
            if pol not in ALLOWED_HOST_POLICIES:
                raise VaultWorkspaceError(f"default_host_policy must be one of {sorted(ALLOWED_HOST_POLICIES)}.")
            target["default_host_policy"] = pol
        if federated_searchable is not None:
            target["federated_searchable"] = bool(federated_searchable)
        _save_registry_unlocked(data, security_dir=security_dir)
        active_id = str(data.get("active_vault_id") or "default")
        return _public_vault_view(target, active_id)


def remove_vault(
    vault_id: str,
    *,
    security_dir: str | Path | None = None,
    current_brain_dir: str | None = None,
) -> dict[str, object]:
    """Non-destructively unregister a vault workspace (never deletes Markdown files on disk)."""
    vid = _slugify_vault_id(vault_id)
    with _VAULTS_LOCK:
        data = _load_registry_unlocked(security_dir=security_dir, default_active_path=current_brain_dir)
        active_id = str(data.get("active_vault_id") or "default")
        if vid == active_id:
            raise VaultWorkspaceError("Cannot remove the currently active vault; switch to another vault first.")
        vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]
        target = next((v for v in vaults if str(v.get("vault_id") or "") == vid), None)
        if target is None:
            raise FileNotFoundError(f"Vault not found: {vid}")
        remaining = [v for v in vaults if str(v.get("vault_id") or "") != vid]
        data["vaults"] = remaining
        _save_registry_unlocked(data, security_dir=security_dir)
        tok_file = target.get("isolated_token_file")
        if isinstance(tok_file, str) and tok_file and os.path.isfile(tok_file) and not _is_reparse_or_link(tok_file):
            try:
                os.remove(tok_file)
            except OSError:
                pass
    return {"status": "removed", "vault_id": vid, "files_deleted": False}


def verify_per_vault_token(
    auth_header: str | None,
    *,
    active_vault_path: str,
    security_dir: str | Path | None = None,
) -> tuple[bool, str | None, str | None]:
    """Validate a per-vault `vb_*` Bearer token strictly against active_vault_path.

    Returns (is_vault_token_format, matched_vault_id_if_valid, error_message_if_rejected).
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return False, None, None
    supplied = auth_header[7:].strip()
    if not supplied.startswith(VAULT_TOKEN_PREFIX):
        return False, None, None
    if len(supplied) < 32:
        return True, None, "403 Forbidden: malformed vault-scoped bearer token."

    supplied_sha = _sha256_str(supplied)
    norm_active = os.path.normcase(os.path.abspath(active_vault_path))
    try:
        with _VAULTS_LOCK:
            data = _load_registry_unlocked(security_dir=security_dir, default_active_path=active_vault_path)
            vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]
    except (OSError, ValueError, VaultWorkspaceSecurityError):
        return True, None, "403 Forbidden: vault token verification unavailable."

    owner_entry: dict[str, object] | None = None
    for item in vaults:
        expected_sha = str(item.get("isolated_token_sha256") or "")
        if len(expected_sha) == 64 and hmac.compare_digest(supplied_sha, expected_sha):
            owner_entry = item
    if owner_entry is None:
        return True, None, "Unauthorized: unrecognized vault-scoped bearer token."
    entry_path = os.path.normcase(os.path.abspath(str(owner_entry.get("path") or "")))
    if entry_path != norm_active:
        return (
            True,
            None,
            f"403 Forbidden: vault-scoped token for '{owner_entry.get('vault_id')}' cannot access a different active vault.",
        )
    return True, str(owner_entry.get("vault_id") or ""), None


def _is_preference_atom(content: str) -> bool:
    fm_split = split_frontmatter(content or "")
    if not fm_split:
        return False
    fm_vals = parse_frontmatter_values(fm_split[0])
    label = str(fm_vals.get("epistemic_label") or "claim").strip().lower()
    return label == "preference" or bool(fm_vals.get("epistemic_contract"))


def get_registered_vault_host_policy(
    brain_dir: str | Path,
    *,
    security_dir: str | Path | None = None,
    policy_path: str | Path | None = None,
) -> tuple[str | None, str | None]:
    """Return (vault_id, default_host_policy) if brain_dir is registered in an existing vaults.json."""
    candidate_sec = security_dir
    if candidate_sec is None and policy_path is not None:
        pol_parent = Path(policy_path).expanduser().resolve().parent
        if os.path.lexists(str(pol_parent / VAULTS_REGISTRY_FILENAME)):
            candidate_sec = pol_parent
    reg_path = vaults_registry_path(candidate_sec)
    if not os.path.lexists(str(reg_path)):
        return None, None
    norm_brain = os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(brain_dir)))))
    try:
        with _VAULTS_LOCK:
            data = _load_registry_unlocked(security_dir=candidate_sec, default_active_path=str(brain_dir))
            vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]
    except (OSError, ValueError, VaultWorkspaceSecurityError):
        return "unknown", "local_only"
    for item in vaults:
        raw_p = str(item.get("path") or "")
        if not raw_p:
            continue
        if os.path.normcase(os.path.realpath(os.path.abspath(raw_p))) == norm_brain:
            vid = str(item.get("vault_id") or "default")
            pol = str(item.get("default_host_policy") or "local_only").strip().lower()
            return vid, pol
    return None, None


def federated_recall(
    query: str,
    *,
    vault_ids: list[str] | tuple[str, ...] | None = None,
    project_scope: str | None = None,
    retrieval_mode: str = "lexical",
    limit: int = 5,
    include_expired: bool = False,
    as_of: str | None = None,
    security_dir: str | Path | None = None,
    current_brain_dir: str | None = None,
) -> dict[str, object]:
    """Strictly opt-in, read-only cross-vault recall across owner-approved atoms only.

    Never reads vaults with `federated_searchable=False`. If caller explicitly requests
    a non-opted-in vault_id, fails closed with VaultWorkspaceSecurityError.
    """
    clean_query = str(query or "").strip()
    if not clean_query or len(clean_query) > 2000:
        raise VaultWorkspaceError("Query must be 1..2000 characters.")
    clean_mode = str(retrieval_mode or "lexical").strip().lower()
    if clean_mode not in {"lexical", "rrf", "dense", "auto"}:
        raise VaultWorkspaceError("retrieval_mode must be one of ('lexical', 'rrf', 'dense', 'auto').")
    top_k = max(1, min(int(limit), 25))
    projects = (str(project_scope).strip(),) if project_scope and str(project_scope).strip() else ()

    with _VAULTS_LOCK:
        data = _load_registry_unlocked(security_dir=security_dir, default_active_path=current_brain_dir)
        vaults = [v for v in data.get("vaults", []) if isinstance(v, dict)]

    by_id = {str(v.get("vault_id") or ""): v for v in vaults if v.get("vault_id")}
    selected: list[dict[str, object]] = []
    skipped_non_opted: list[str] = []

    if vault_ids is not None:
        requested = [_slugify_vault_id(v) for v in vault_ids if str(v or "").strip()]
        if not requested:
            raise VaultWorkspaceError("Provide at least one vault_id or omit vault_ids to query all opted-in vaults.")
        for vid in requested:
            if vid not in by_id:
                raise FileNotFoundError(f"Vault not found: {vid}")
            entry = by_id[vid]
            if not bool(entry.get("federated_searchable", False)):
                raise VaultWorkspaceSecurityError(
                    f"403 Forbidden: vault '{vid}' has not opted into federated cross-vault search (federated_searchable=false)."
                )
            selected.append(entry)
    else:
        for entry in vaults:
            vid = str(entry.get("vault_id") or "")
            if bool(entry.get("federated_searchable", False)):
                selected.append(entry)
            elif vid:
                skipped_non_opted.append(vid)

    merged: list[dict[str, object]] = []
    searched_ids: list[str] = []
    for entry in selected:
        vid = str(entry.get("vault_id") or "")
        vlabel = str(entry.get("label") or vid)
        vpath = str(entry.get("path") or "")
        if not os.path.isdir(vpath) or _is_reparse_or_link(vpath):
            continue
        vp = vault_paths_for(vpath)
        try:
            if not lexical_index_is_current(vp["atoms_dir"], vp["lexical_index_path"]):
                os.makedirs(vp["sidecar_dir"], exist_ok=True)
                os.makedirs(vp["atoms_dir"], exist_ok=True)
                with review_lock(os.path.join(vp["sidecar_dir"], "writer.lock")):
                    if not lexical_index_is_current(vp["atoms_dir"], vp["lexical_index_path"]):
                        rebuild_lexical_index(vp["atoms_dir"], vp["lexical_index_path"])
            direct, _ = search_memory(
                clean_query,
                RecallOptions(
                    n_results=top_k,
                    follow_links=False,
                    include_statuses={"accepted"},
                    projects=projects,
                    retrieval_mode=clean_mode,
                    enable_reranker=True,
                    include_expired=bool(include_expired),
                    as_of=as_of,
                ),
                atoms_dir=vp["atoms_dir"],
                index_path=vp["lexical_index_path"],
                chroma_path=vp["chroma_path"],
            )
        except Exception:
            continue
        searched_ids.append(vid)
        for hit in direct:
            # Exclude subjective preferences in cross-vault federation by default.
            if _is_preference_atom(hit.content):
                continue
            item = hit.as_dict()
            item["vault_id"] = vid
            item["vault_label"] = vlabel
            merged.append(item)

    if len(merged) > 1:
        ce_scores, _ = score_query_atom_pairs(clean_query, [str(r.get("content") or "") for r in merged])
        for item, ce in zip(merged, ce_scores):
            base_s = float(item.get("score") or 0.0)
            # Normalize RRF vs [0,1] scores onto comparable cross-vault ranking scale
            norm_base = min(1.0, base_s * 20.0) if str(item.get("source")) == "rrf_hybrid" else base_s
            item["cross_vault_score"] = round(0.75 * norm_base + 0.25 * ce, 6)
        merged.sort(
            key=lambda r: (
                -float(r.get("cross_vault_score", r.get("score") or 0.0)),
                str(r.get("vault_id") or ""),
                str(r.get("filename") or ""),
            )
        )
    else:
        merged.sort(key=lambda r: (-float(r.get("score") or 0.0), str(r.get("vault_id") or ""), str(r.get("filename") or "")))
    return {
        "query": clean_query,
        "retrieval_mode": clean_mode,
        "searched_vaults": searched_ids,
        "skipped_non_opted_in": skipped_non_opted,
        "count": len(merged[:top_k]),
        "results": merged[:top_k],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="second-brain vault",
        description="Manage isolated Multi-Vault Workspaces and opt-in cross-vault recall.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List all registered vaults.")
    p_list.add_argument("--json", action="store_true")

    p_create = sub.add_parser("create", help="Create and initialize a new isolated vault workspace.")
    p_create.add_argument("label", help="Human-friendly workspace label (e.g. 'Work').")
    p_create.add_argument("path", help="Directory path for the new vault.")
    p_create.add_argument("--id", dest="vault_id", default=None, help="Explicit vault_id slug.")
    p_create.add_argument("--isolated-token", action="store_true", help="Mint an isolated per-vault bearer token.")
    p_create.add_argument("--host-policy", choices=["local_only", "online_opt_in"], default="local_only")
    p_create.add_argument("--federated-searchable", action="store_true", help="Opt this vault into read-only federated search.")
    p_create.add_argument("--activate", action="store_true", help="Switch active vault to the new workspace immediately.")
    p_create.add_argument("--json", action="store_true")

    p_switch = sub.add_parser("switch", help="Switch active vault across CLI, MCP, Dashboard, and Desktop.")
    p_switch.add_argument("vault_id", help="Registered vault_id.")
    p_switch.add_argument("--json", action="store_true")

    p_pol = sub.add_parser("policy", help="Update access profile / federated search opt-in for a vault.")
    p_pol.add_argument("vault_id", help="Registered vault_id.")
    p_pol.add_argument("--label", default=None)
    p_pol.add_argument("--host-policy", choices=["local_only", "online_opt_in"], default=None)
    fed_grp = p_pol.add_mutually_exclusive_group()
    fed_grp.add_argument("--federated-searchable", dest="federated", action="store_true", default=None)
    fed_grp.add_argument("--no-federated-searchable", dest="federated", action="store_false")
    p_pol.add_argument("--json", action="store_true")

    p_rem = sub.add_parser("remove", help="Non-destructively unregister a vault workspace (keeps files on disk).")
    p_rem.add_argument("vault_id", help="Registered vault_id.")
    p_rem.add_argument("--json", action="store_true")

    p_search = sub.add_parser("search", help="Run opt-in read-only federated recall across opted-in vaults.")
    p_search.add_argument("query", help="Search query.")
    p_search.add_argument("--vaults", default="", help="Comma-separated vault_ids (must each be federated_searchable).")
    p_search.add_argument("--project", default=None, help="Optional project:<id> scope.")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            res = list_vaults()
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                for v in res["vaults"]:
                    mark = "*" if v["active"] else " "
                    fed = "federated" if v["federated_searchable"] else "isolated"
                    print(f" {mark} {v['vault_id']:16}  {v['label']:20}  [{fed}]  {v['path']} ({v['atom_count']} atoms, {v['note_count']} notes)")
            return 0
        if args.cmd == "create":
            res = create_vault(
                args.label,
                args.path,
                vault_id=args.vault_id,
                isolated_token=args.isolated_token,
                default_host_policy=args.host_policy,
                federated_searchable=args.federated_searchable,
                activate=args.activate,
            )
            print(json.dumps(res, indent=2) if args.json else f"Created vault '{res['vault_id']}' at {res['path']}")
            return 0
        if args.cmd == "switch":
            res = switch_vault(args.vault_id)
            print(json.dumps(res, indent=2) if args.json else f"Switched active vault to '{res['active_vault_id']}' ({res['active_vault_path']})")
            return 0
        if args.cmd == "policy":
            res = update_vault_policy(
                args.vault_id,
                label=args.label,
                default_host_policy=args.host_policy,
                federated_searchable=args.federated,
            )
            print(json.dumps(res, indent=2) if args.json else f"Updated vault '{res['vault_id']}' (federated_searchable={res['federated_searchable']})")
            return 0
        if args.cmd == "remove":
            res = remove_vault(args.vault_id)
            print(json.dumps(res, indent=2) if args.json else f"Unregistered vault '{res['vault_id']}' (files preserved on disk)")
            return 0
        if args.cmd == "search":
            vids = [v.strip() for v in args.vaults.split(",") if v.strip()] if args.vaults else None
            res = federated_recall(args.query, vault_ids=vids, project_scope=args.project, limit=args.limit)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Searched vaults: {', '.join(res['searched_vaults']) or '(none opted-in)'}")
                for hit in res["results"]:
                    print(f"- [{hit['vault_id']}] {hit['filename']} (score={hit['score']})")
            return 0
    except (VaultWorkspaceError, VaultWorkspaceSecurityError, FileNotFoundError, PermissionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
