# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["filelock"]
# ///
"""Git Post-Commit & Coding Session Memory Distiller.

Captures local repository architectural commits and bug fixes into:
  1. Immutable episodic records in `brain/4-experiences/` (`save_experience_record`)
  2. Quarantined candidate captures in `brain/0-raw/inbox/` for 1-click owner verification

Security invariants:
  * Strict list-argv `subprocess.run(..., shell=False)` with git option-injection guards (`--` / SHA regex).
  * Symlink/reparse ancestor rejection across target repositories, `.git/hooks`, and vault directories.
  * Mandatory outbound DLP secret redaction (`inspect_and_redact_outbound`) and prompt-injection risk
    screening (`scan_inbox_payload_risks`) BEFORE writing anything to disk.
  * Non-destructive managed `post-commit` hook block (`# >>> second-brain-post-commit >>>`) that
    preserves existing user hooks and never fails `git commit`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import content_sha256, review_lock  # noqa: E402
import cognitive_core  # noqa: E402
from config import (  # noqa: E402
    _is_immutable_darwin_root_symlink,
    _is_reparse_or_link,
    resolve_active_brain_dir,
    safe_file_in_dir,
)
from host_adapters import inspect_and_redact_outbound  # noqa: E402
from inbox_workflow import scan_inbox_payload_risks  # noqa: E402


HOOK_BEGIN_MARKER = "# >>> second-brain-post-commit >>>"
HOOK_END_MARKER = "# <<< second-brain-post-commit <<<"
MAX_COMMITS_PER_RUN = 25
MAX_DISTILLED_LEDGER = 4096

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_PROJECT_SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class GitDistillerError(RuntimeError):
    """Raised on invalid git repository, hook path, or distillation usage."""


class GitDistillerSecurityError(PermissionError):
    """Raised when a symlink/reparse or path-escape boundary is violated."""


def _assert_no_symlink_ancestors(path: Path, *, stop_at: Path | None = None) -> None:
    stop_res = stop_at.resolve() if (stop_at is not None and stop_at.exists()) else None
    for cur in (path, *path.parents):
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
            if not _is_immutable_darwin_root_symlink(cur):
                raise GitDistillerSecurityError(f"Refusing to access git/vault path via symlink or reparse point: {cur}")
        if stop_res is not None and cur == stop_res:
            break


def _run_git(repo_root: Path, git_args: list[str], *, timeout: float = 12.0) -> str:
    cmd = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.file.allow=never",
        "-C",
        str(repo_root),
        *git_args,
    ]
    proc = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git command failed").strip()[:300]
        raise GitDistillerError(f"git {' '.join(git_args[:2])} failed: {err}")
    return proc.stdout


def resolve_git_repository(repo_dir: str | Path | None = None) -> Path:
    """Validate and return the canonical git working-tree root without symlink traversal."""
    raw = str(repo_dir or os.getcwd()).strip()
    if not raw or raw.startswith("-"):
        raise GitDistillerError(f"Invalid repository path: {repo_dir!r}")
    cand = Path(os.path.abspath(os.path.expanduser(raw)))
    if not os.path.lexists(str(cand)) or not cand.is_dir():
        raise GitDistillerError(f"Repository directory does not exist: {cand}")
    _assert_no_symlink_ancestors(cand)

    top = _run_git(cand, ["rev-parse", "--show-toplevel"]).strip()
    if not top:
        raise GitDistillerError(f"Not a git repository: {cand}")
    root = Path(os.path.abspath(top))
    _assert_no_symlink_ancestors(root)
    return root


def _resolve_git_hooks_dir(repo_root: Path) -> Path:
    # Reject repositories that define a local core.hooksPath escaping .git/hooks
    cfg_proc = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(repo_root), "config", "--local", "--get", "core.hooksPath"],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=8.0,
        check=False,
    )
    if cfg_proc.returncode == 0 and (cfg_proc.stdout or "").strip():
        raise GitDistillerSecurityError(
            f"Refusing to manage hooks when repository defines local core.hooksPath: {cfg_proc.stdout.strip()!r}"
        )

    raw_common = _run_git(repo_root, ["-c", "core.hooksPath=", "rev-parse", "--git-common-dir"]).strip()
    if not raw_common:
        raise GitDistillerError(f"Could not resolve .git directory for repository: {repo_root}")
    common_path = Path(raw_common) if os.path.isabs(raw_common) else (repo_root / raw_common)
    common_abs = Path(os.path.abspath(str(common_path)))
    _assert_no_symlink_ancestors(common_abs)
    if not (common_abs / "HEAD").is_file():
        raise GitDistillerSecurityError(f"Invalid or untrusted git common directory: {common_abs}")
    hooks_abs = common_abs / "hooks"
    _assert_no_symlink_ancestors(hooks_abs)
    return hooks_abs


def _derive_project_scope(repo_root: Path, explicit_project: str | None = None) -> str:
    if explicit_project and str(explicit_project).strip():
        clean = str(explicit_project).strip()
        if not clean.lower().startswith("project:"):
            clean = f"project:{clean}"
        slug = _PROJECT_SLUG_RE.sub("_", clean.split(":", 1)[1]).strip("._-")[:64] or "repo"
        return f"project:{slug}"
    slug = _PROJECT_SLUG_RE.sub("_", repo_root.name.lower()).strip("._-")[:64] or "repo"
    return f"project:{slug}"


def _distilled_ledger_path(brain_dir: Path) -> Path:
    return brain_dir / ".sidecar" / "distilled-git-commits.json"


def _load_distilled_shas(brain_dir: Path) -> list[str]:
    p = _distilled_ledger_path(brain_dir)
    if not os.path.lexists(str(p)):
        return []
    _assert_no_symlink_ancestors(p, stop_at=brain_dir)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("commit_shas"), list):
            return [str(s).lower() for s in doc["commit_shas"] if _GIT_SHA_RE.fullmatch(str(s))]
    except Exception:
        pass
    return []


def _save_distilled_shas(brain_dir: Path, shas: list[str]) -> None:
    p = _distilled_ledger_path(brain_dir)
    _assert_no_symlink_ancestors(p, stop_at=brain_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    bounded = shas[-MAX_DISTILLED_LEDGER:]
    tmp = p.parent / f".tmp-git-distill-{secrets.token_hex(6)}"
    payload = (json.dumps({"schema_version": 1, "commit_shas": bounded}, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(p))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _scrub_comments(text: str) -> str:
    return text.replace("<!--", "").replace("-->", "")


def _stage_commit_to_inbox(
    *,
    brain_dir: Path,
    commit_sha: str,
    committed_at: str,
    project_scope: str,
    repo_name: str,
    sanitized_subject: str,
    sanitized_body: str,
    changed_files: list[str],
    risk_flags: list[str],
) -> dict[str, Any]:
    inbox_dir = brain_dir / "0-raw" / "inbox"
    _assert_no_symlink_ancestors(inbox_dir, stop_at=brain_dir)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    origin = "untrusted_inbox" if risk_flags else "user_local"
    safe_subj = _scrub_comments(re.sub(r"[\r\n]+", " ", sanitized_subject)).strip()[:200] or f"Commit {commit_sha[:8]}"
    safe_body = _scrub_comments(sanitized_body).strip()[:16_000]
    safe_date = _scrub_comments(re.sub(r"[^0-9T:+-Z]", "", committed_at))[:40]
    files_preview = [
        _scrub_comments(re.sub(r"[\r\n`]+", "", f)).strip()[:180]
        for f in changed_files[:25]
        if f.strip()
    ]

    lines = [
        f"<!-- second_brain_origin: {origin} -->",
        f"<!-- git_commit_sha: {commit_sha} repo: {_scrub_comments(repo_name)[:80]} committed_at: {safe_date} -->",
    ]
    if risk_flags:
        lines.append(f"<!-- quarantine_risk_flags: {', '.join(risk_flags)} -->")
    lines.extend(
        [
            "",
            f"# [{project_scope}] {safe_subj}",
            "",
            f"- **Commit:** `{commit_sha[:12]}` ({safe_date or 'unknown'})",
            f"- **Scope:** `{project_scope}`",
        ]
    )
    if files_preview:
        lines.append(f"- **Touched Files ({len(changed_files)}):** " + ", ".join(f"`{p}`" for p in files_preview[:12]))
    lines.append("")
    if safe_body:
        lines.append(safe_body)
        lines.append("")

    raw_bytes = "\n".join(lines).encode("utf-8")
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    dest_str = ""
    for attempt in range(32):
        cand_name = (
            f"git-commit-{day}-{commit_sha[:8]}.md"
            if attempt == 0
            else f"git-commit-{day}-{commit_sha[:8]}-{attempt + 1}.md"
        )
        cand = safe_file_in_dir(str(inbox_dir), cand_name, allowed_exts={".md"}, must_exist=False)
        if not cand:
            raise GitDistillerSecurityError("Invalid inbox filename for distilled git commit.")
        try:
            fd = os.open(cand, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(raw_bytes)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                if os.path.exists(cand):
                    try:
                        os.unlink(cand)
                    except OSError:
                        pass
                raise
            dest_str = cand
            break
        except FileExistsError:
            continue
    if not dest_str:
        raise GitDistillerError("Could not allocate unique inbox file for distilled git commit.")

    return {
        "inbox_filename": os.path.basename(dest_str),
        "rel_path": f"0-raw/inbox/{os.path.basename(dest_str)}",
        "sha256": content_sha256(raw_bytes),
        "origin": origin,
    }


def distill_git_commits(
    *,
    repo_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
    project_scope: str | None = None,
    max_commits: int = 5,
    commit_sha: str | None = None,
    stage_inbox: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Distill recent git commits into `4-experiences/` and quarantined `0-raw/inbox/` with DLP screening."""
    repo_root = resolve_git_repository(repo_dir)
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    _assert_no_symlink_ancestors(active_brain)
    (active_brain / "4-experiences").mkdir(parents=True, exist_ok=True)
    (active_brain / "0-raw" / "inbox").mkdir(parents=True, exist_ok=True)
    (active_brain / ".sidecar").mkdir(parents=True, exist_ok=True)

    eff_project = _derive_project_scope(repo_root, project_scope)
    bounded_max = max(1, min(int(max_commits), MAX_COMMITS_PER_RUN))

    if commit_sha and str(commit_sha).strip():
        clean_sha = str(commit_sha).strip()
        if not _GIT_SHA_RE.fullmatch(clean_sha):
            raise GitDistillerError(f"Invalid git commit SHA: {commit_sha!r}")
        rev_list_out = _run_git(repo_root, ["rev-list", "-n", "1", clean_sha, "--"]).strip()
        shas = [rev_list_out] if rev_list_out else []
    else:
        try:
            rev_list_out = _run_git(repo_root, ["rev-list", "-n", str(bounded_max), "HEAD", "--"]).strip()
            shas = [line.strip() for line in rev_list_out.splitlines() if _GIT_SHA_RE.fullmatch(line.strip())]
        except GitDistillerError:
            shas = []

    if not shas:
        return {
            "repo_root": str(repo_root),
            "project_scope": eff_project,
            "distilled_count": 0,
            "skipped_duplicate_count": 0,
            "items": [],
        }

    lock_path = active_brain / ".sidecar" / "writer.lock"
    distilled_items: list[dict[str, Any]] = []
    skipped_dups = 0

    # Ordered oldest -> newest so chronological progression is preserved
    for sha in reversed(shas):
        sha_low = sha.lower()
        with review_lock(str(lock_path)):
            known_list = _load_distilled_shas(active_brain)
            if not force and sha_low in set(known_list):
                skipped_dups += 1
                continue

        meta_raw = _run_git(
            repo_root,
            ["show", "-s", "--format=%H%x1f%cI%x1f%s%x1f%b", sha, "--"],
        )
        parts = meta_raw.split("\x1f", 3)
        full_sha = (parts[0].strip() if len(parts) > 0 else sha).lower()
        committed_at = parts[1].strip() if len(parts) > 1 else ""
        subject = parts[2].strip() if len(parts) > 2 else ""
        body = parts[3].strip() if len(parts) > 3 else ""

        files_out = _run_git(
            repo_root,
            ["diff-tree", "--no-commit-id", "--name-only", "-r", sha, "--"],
        )
        changed_files = [ln.strip() for ln in files_out.splitlines() if ln.strip()]

        subj_dlp = inspect_and_redact_outbound(subject, redact_pii=False, fail_on_secret_detection=False)
        body_dlp = inspect_and_redact_outbound(body, redact_pii=False, fail_on_secret_detection=False)
        files_joined = "\n".join(changed_files[:50])
        files_dlp = inspect_and_redact_outbound(files_joined, redact_pii=False, fail_on_secret_detection=False)

        risks: list[str] = []
        for flag in scan_inbox_payload_risks(f"{subject}\n{body}\n{files_joined}"):
            if str(flag) not in risks:
                risks.append(str(flag))
        for rep in (subj_dlp, body_dlp, files_dlp):
            for hit in rep.secret_hits:
                lbl = f"dlp_secret:{hit}"
                if lbl not in risks:
                    risks.append(lbl)

        clean_subj = subj_dlp.sanitized_text or f"Commit {full_sha[:8]}"
        clean_body = body_dlp.sanitized_text
        clean_files = [ln.strip() for ln in files_dlp.sanitized_text.splitlines() if ln.strip()]

        experience_filename: str | None = None
        # Only write to 4-experiences/ when post-redaction payload (including touched filenames) has zero residual risk flags
        residual_risks = list(
            scan_inbox_payload_risks(f"{clean_subj}\n{clean_body}\n" + "\n".join(clean_files[:16]))
        )
        if not residual_risks:
            try:
                summary_parts = [f"Commit {full_sha[:12]} in {repo_root.name}: {clean_subj}"]
                if clean_body:
                    summary_parts.append(clean_body[:1500])
                exp_res = cognitive_core.save_experience_record(
                    goal=f"[{eff_project}] {clean_subj}"[:300],
                    result_summary="\n\n".join(summary_parts)[:3000],
                    success=True,
                    context=[eff_project, f"git_commit:{full_sha[:12]}"],
                    actions=[f"Modified {f}" for f in clean_files[:16]] or [f"Committed {full_sha[:12]}"],
                    tools_used=["git"],
                    lessons=[clean_subj[:300]],
                    brain_dir=str(active_brain),
                    experiences_dir=str(active_brain / "4-experiences"),
                )
                experience_filename = str(exp_res.get("filename") or "")
            except Exception:
                experience_filename = None

        inbox_info: dict[str, Any] | None = None
        if stage_inbox:
            with review_lock(str(lock_path)):
                inbox_info = _stage_commit_to_inbox(
                    brain_dir=active_brain,
                    commit_sha=full_sha,
                    committed_at=committed_at,
                    project_scope=eff_project,
                    repo_name=repo_root.name,
                    sanitized_subject=clean_subj,
                    sanitized_body=clean_body,
                    changed_files=clean_files,
                    risk_flags=risks,
                )

        with review_lock(str(lock_path)):
            cur_list = _load_distilled_shas(active_brain)
            if full_sha not in set(cur_list):
                cur_list.append(full_sha)
                _save_distilled_shas(active_brain, cur_list)

        distilled_items.append(
            {
                "commit_sha": full_sha,
                "committed_at": committed_at,
                "subject": clean_subj,
                "changed_file_count": len(clean_files),
                "experience_filename": experience_filename,
                "inbox_filename": inbox_info["inbox_filename"] if inbox_info else None,
                "risk_flags": risks,
            }
        )

    return {
        "repo_root": str(repo_root),
        "project_scope": eff_project,
        "distilled_count": len(distilled_items),
        "skipped_duplicate_count": skipped_dups,
        "items": distilled_items,
    }


def _sh_single_quote(value: str) -> str:
    """POSIX /bin/sh safe single-quoting (immune to spaces, $, backticks, and backslashes)."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _build_managed_hook_block(*, repo_root: Path, brain_dir: Path, project_scope: str) -> str:
    # Preserve .venv/bin/python symlink path on POSIX so CPython locates pyvenv.cfg (never .resolve() sys.executable)
    raw_py = Path(os.path.abspath(sys.executable)).as_posix()
    raw_script = Path(__file__).resolve().as_posix()
    raw_repo = repo_root.as_posix()
    raw_brain = brain_dir.as_posix()
    for val in (raw_py, raw_script, raw_repo, raw_brain):
        if re.search(r"[\x00-\x1f\x7f]", val) or HOOK_BEGIN_MARKER in val or HOOK_END_MARKER in val:
            raise GitDistillerSecurityError("Unsafe control characters or hook markers in repository/runtime path.")
    py_exe = _sh_single_quote(raw_py)
    script_path = _sh_single_quote(raw_script)
    repo_posix = _sh_single_quote(raw_repo)
    brain_posix = _sh_single_quote(raw_brain)
    safe_proj = _sh_single_quote(re.sub(r"[^a-zA-Z0-9_:.-]+", "", project_scope)[:80])
    common_flags = f"--repo {repo_posix} --brain-dir {brain_posix} --project {safe_proj} --max-commits 1 --quiet"
    return (
        f"{HOOK_BEGIN_MARKER}\n"
        f"({py_exe} -m second_brain hook distill {common_flags} || {py_exe} {script_path} distill {common_flags}) >/dev/null 2>&1 || true\n"
        f"{HOOK_END_MARKER}\n"
    )


def get_git_hook_status(repo_dir: str | Path | None = None) -> dict[str, Any]:
    repo_root = resolve_git_repository(repo_dir)
    hooks_dir = _resolve_git_hooks_dir(repo_root)
    hook_file = hooks_dir / "post-commit"
    installed = False
    if os.path.lexists(str(hook_file)):
        _assert_no_symlink_ancestors(hook_file)
        if hook_file.is_file():
            txt = hook_file.read_text(encoding="utf-8", errors="replace")
            installed = HOOK_BEGIN_MARKER in txt and HOOK_END_MARKER in txt
    return {
        "repo_root": str(repo_root),
        "hook_path": str(hook_file),
        "installed": installed,
    }


def install_git_post_commit_hook(
    *,
    repo_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
    project_scope: str | None = None,
) -> dict[str, Any]:
    """Install or update the non-destructive Second Brain `post-commit` hook block."""
    repo_root = resolve_git_repository(repo_dir)
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    _assert_no_symlink_ancestors(active_brain)
    hooks_dir = _resolve_git_hooks_dir(repo_root)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "post-commit"
    if os.path.lexists(str(hook_file)):
        _assert_no_symlink_ancestors(hook_file)

    eff_project = _derive_project_scope(repo_root, project_scope)
    block = _build_managed_hook_block(repo_root=repo_root, brain_dir=active_brain, project_scope=eff_project)

    existing = ""
    if hook_file.is_file():
        existing = hook_file.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")

    pattern = re.compile(
        rf"{re.escape(HOOK_BEGIN_MARKER)}.*?{re.escape(HOOK_END_MARKER)}\n?",
        flags=re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing, count=1)
    elif existing.strip():
        if not existing.endswith("\n"):
            existing += "\n"
        updated = existing + "\n" + block
    else:
        updated = "#!/bin/sh\n" + block

    tmp = hooks_dir / f".tmp-post-commit-{secrets.token_hex(6)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o700)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(updated.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(hook_file))
        try:
            cur_mode = os.stat(str(hook_file)).st_mode
            os.chmod(str(hook_file), cur_mode | stat.S_IXUSR)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return {
        "status": "installed",
        "repo_root": str(repo_root),
        "hook_path": str(hook_file),
        "project_scope": eff_project,
    }


def uninstall_git_post_commit_hook(*, repo_dir: str | Path | None = None) -> dict[str, Any]:
    """Remove only the Second Brain managed block from `.git/hooks/post-commit`, preserving user hooks."""
    repo_root = resolve_git_repository(repo_dir)
    hooks_dir = _resolve_git_hooks_dir(repo_root)
    hook_file = hooks_dir / "post-commit"
    if not os.path.lexists(str(hook_file)):
        return {"status": "not_installed", "repo_root": str(repo_root), "hook_path": str(hook_file)}
    _assert_no_symlink_ancestors(hook_file)

    existing = hook_file.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    pattern = re.compile(
        rf"\n?{re.escape(HOOK_BEGIN_MARKER)}.*?{re.escape(HOOK_END_MARKER)}\n?",
        flags=re.DOTALL,
    )
    if not pattern.search(existing):
        return {"status": "not_installed", "repo_root": str(repo_root), "hook_path": str(hook_file)}

    remaining = pattern.sub("\n", existing).strip()
    if not remaining or remaining == "#!/bin/sh":
        hook_file.unlink()
        return {"status": "removed", "repo_root": str(repo_root), "hook_path": str(hook_file), "deleted_empty_hook": True}

    tmp = hooks_dir / f".tmp-post-commit-{secrets.token_hex(6)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o700)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write((remaining + "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(hook_file))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return {"status": "removed", "repo_root": str(repo_root), "hook_path": str(hook_file), "deleted_empty_hook": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="second-brain hook",
        description="Install/manage git post-commit hooks and distill commits into Second Brain.",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_st = sub.add_parser("status", help="Show git post-commit hook status for a repository.")
    p_st.add_argument("--repo", default=".")
    p_st.add_argument("--json", action="store_true")

    p_in = sub.add_parser("install", help="Install non-destructive Second Brain post-commit hook.")
    p_in.add_argument("--repo", default=".")
    p_in.add_argument("--brain-dir", default=None)
    p_in.add_argument("--project", default=None)
    p_in.add_argument("--json", action="store_true")

    p_un = sub.add_parser("uninstall", help="Remove Second Brain post-commit hook block.")
    p_un.add_argument("--repo", default=".")
    p_un.add_argument("--json", action="store_true")

    p_ds = sub.add_parser("distill", help="Distill recent git commits into 4-experiences/ and 0-raw/inbox/.")
    p_ds.add_argument("--repo", default=".")
    p_ds.add_argument("--brain-dir", default=None)
    p_ds.add_argument("--project", default=None)
    p_ds.add_argument("--max-commits", type=int, default=5)
    p_ds.add_argument("--commit", default=None)
    p_ds.add_argument("--no-inbox", action="store_true")
    p_ds.add_argument("--force", action="store_true")
    p_ds.add_argument("--quiet", action="store_true")
    p_ds.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.subcmd == "status":
            res = get_git_hook_status(args.repo)
            print(json.dumps(res, indent=2) if args.json else f"installed={res['installed']} hook={res['hook_path']}")
            return 0
        if args.subcmd == "install":
            res = install_git_post_commit_hook(repo_dir=args.repo, brain_dir=args.brain_dir, project_scope=args.project)
            print(json.dumps(res, indent=2) if args.json else f"Installed post-commit hook at {res['hook_path']} ({res['project_scope']})")
            return 0
        if args.subcmd == "uninstall":
            res = uninstall_git_post_commit_hook(repo_dir=args.repo)
            print(json.dumps(res, indent=2) if args.json else f"{res['status']}: {res['hook_path']}")
            return 0
        if args.subcmd == "distill":
            res = distill_git_commits(
                repo_dir=args.repo,
                brain_dir=args.brain_dir,
                project_scope=args.project,
                max_commits=args.max_commits,
                commit_sha=args.commit,
                stage_inbox=not args.no_inbox,
                force=bool(args.force),
            )
            if not args.quiet:
                print(
                    json.dumps(res, indent=2)
                    if args.json
                    else f"Distilled {res['distilled_count']} commit(s) ({res['project_scope']})"
                )
            return 0
    except Exception as exc:
        if not getattr(args, "quiet", False):
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
