# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Quarantined Skill & Knowledge Pack Importer (`agent-skills` + `pm-skills`, `2C.6`).

Discovers external skill packs (e.g., all 24 `addyosmani/agent-skills` playbooks or
`phuryn/pm-skills`), snapshots immutable raw source files into
`brain/sources/skill-packs/<pack>/<skill>.md` with a SHA-256 manifest, and stages
strictly **unapproved / quarantined** artifacts (`review_status: pending` or `quarantined`
procedural atoms anchored to `sources/` + `status: draft` Playbooks in `5-playbooks/`).
External packs NEVER self-promote to `accepted` or `active` without Owner CAS sign-off.
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
import re
import sys
from typing import Any

if __name__ in ("skill_pack_importer", "scripts.skill_pack_importer"):
    sys.modules.setdefault("skill_pack_importer", sys.modules[__name__])
    sys.modules.setdefault("scripts.skill_pack_importer", sys.modules[__name__])

from scripts.atom_mutator import atomic_write_bytes, content_sha256, vault_writer_lock  # noqa: E402
from scripts.config import (  # noqa: E402
    BRAIN_DIR,
    _is_reparse_or_link,
    resolve_module_vault_path,
    snapshot_module_defaults,
)
from scripts.evidence import find_evidence_anchor  # noqa: E402
from scripts.host_adapters import inspect_and_redact_outbound  # noqa: E402
from scripts.memory_format import render_memory_document, split_frontmatter  # noqa: E402
from scripts.memory_review import parse_frontmatter_values  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

DEFAULT_AGENT_SKILLS_DIRS = (
    Path.home() / "local-ai" / "repos" / "agent-skills" / "skills",
)
DEFAULT_PM_SKILLS_DIRS = (
    Path.home() / "local-ai" / "repos" / "pm-skills",
)

_PACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")
_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")
_INJECTION_PATTERNS = (
    re.compile(r"(?i)\bignore\s+(?:all\s+|any\s+)?previous\s+instructions\b"),
    re.compile(r"(?i)\boverride\s+(?:system|safety)\s+(?:rules?|prompts?)\b"),
    re.compile(r"(?i)\bexfiltrat(?:e|ion)\b"),
    re.compile(r"(?i)\brm\s+-rf\s+/"),
)


class SkillPackImportError(ValueError):
    """Raised on invalid pack name, unsafe paths, or missing skill pack directory."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _render_doc(frontmatter: dict[str, Any], body_text: str) -> str:
    fm_lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in frontmatter.items()]
    return render_memory_document(fm_lines, body_text.splitlines())


def _slugify(raw: str) -> str:
    slug = _SLUG_CLEAN_RE.sub("-", str(raw or "").strip().lower()).strip("-")
    return slug[:64] or "unnamed-skill"


def discover_default_skill_packs() -> dict[str, str | None]:
    def _first_existing(env_var: str, candidates: tuple[Path, ...]) -> str | None:
        all_cands: list[Path] = []
        override = (os.environ.get(env_var) or "").strip()
        if override:
            all_cands.append(Path(os.path.expanduser(override)))
        all_cands.extend(candidates)
        for cand in all_cands:
            if os.path.lexists(str(cand)) and not _is_reparse_or_link(str(cand)) and cand.is_dir():
                return str(cand.resolve())
        return None

    return {
        "agent-skills": _first_existing("SECOND_BRAIN_AGENT_SKILLS_DIR", DEFAULT_AGENT_SKILLS_DIRS),
        "pm-skills": _first_existing("SECOND_BRAIN_PM_SKILLS_DIR", DEFAULT_PM_SKILLS_DIRS),
    }


def _collect_skill_markdown_files(source_dir: Path) -> list[tuple[str, Path]]:
    """Collect ``(skill_slug, file_path)`` pairs from `SKILL.md` subdirs or flat markdown files."""
    if os.path.lexists(str(source_dir)) and _is_reparse_or_link(str(source_dir)):
        raise PermissionError(f"Refusing symlinked skill pack root: {source_dir}")
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Skill pack directory not found: {source_dir}")

    found: list[tuple[str, Path]] = []
    seen_slugs: set[str] = set()
    for entry in sorted(source_dir.iterdir(), key=lambda p: p.name.lower()):
        if _is_reparse_or_link(str(entry)) or entry.name.startswith("."):
            continue
        if entry.is_dir():
            skill_md = entry / "SKILL.md"
            if os.path.lexists(str(skill_md)) and not _is_reparse_or_link(str(skill_md)) and skill_md.is_file():
                slug = _slugify(entry.name)
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    found.append((slug, skill_md))
        elif entry.is_file() and entry.suffix.lower() == ".md" and entry.name.lower() not in {"readme.md", "license.md", "changelog.md"}:
            slug = _slugify(entry.stem)
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                found.append((slug, entry))

    if not found:
        # Nested packs (e.g. pm-skills/<plugin>/skills/<name>/SKILL.md) — never follow symlinks
        for cur_dir, dirnames, filenames in os.walk(str(source_dir), followlinks=False):
            if _is_reparse_or_link(cur_dir):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d
                for d in sorted(dirnames, key=str.lower)
                if not d.startswith(".") and not _is_reparse_or_link(os.path.join(cur_dir, d))
            ]
            for fn in sorted(filenames, key=str.lower):
                if fn != "SKILL.md":
                    continue
                skill_md = Path(cur_dir) / fn
                if _is_reparse_or_link(str(skill_md)) or not skill_md.is_file():
                    continue
                slug = _slugify(Path(cur_dir).name)
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    found.append((slug, skill_md))
    return found


def _parse_skill_doc(raw_text: str, fallback_slug: str) -> dict[str, Any]:
    parsed = split_frontmatter(raw_text)
    fm_vals: dict[str, Any] = {}
    body_lines = raw_text.splitlines()
    if parsed is not None:
        fm_lines, body_lines = parsed
        fm_vals = parse_frontmatter_values(fm_lines)

    body_text = "\n".join(body_lines).strip()
    title = str(fm_vals.get("name") or "").strip()
    if not title:
        for ln in body_lines:
            m = re.match(r"^#\s+(.+?)\s*$", ln)
            if m:
                title = m.group(1).strip()
                break
    title = title or fallback_slug.replace("-", " ").title()
    desc = str(fm_vals.get("description") or "").strip()

    steps: list[str] = []
    for ln in body_lines:
        hm = re.match(r"^###\s+(?:Step\s+\d+[:.]?\s*)?(.+?)\s*$", ln, re.I)
        if hm:
            steps.append(hm.group(1).strip())
        elif re.match(r"^\d+\.\s+.{6,160}$", ln.strip()):
            steps.append(re.sub(r"^\d+\.\s+", "", ln.strip()))
        if len(steps) >= 8:
            break
    if not steps:
        steps = [
            f"Inspect requirements and applicability for {title}.",
            f"Follow documented workflow inside sources/skill-packs/.../{fallback_slug}.md.",
            "Verify outputs and tests before owner promotion.",
        ]

    # Pick unique verbatim substring from raw_text >= 20 chars for strict evidence.py anchoring
    quote = ""
    candidates: list[str] = []
    for para in re.split(r"\r?\n\s*\r?\n", body_text):
        cleaned = para.strip()
        if cleaned and not cleaned.startswith("#") and not cleaned.startswith("```") and len(cleaned) >= 20:
            candidates.append(cleaned[:320].strip())
    if desc and len(desc) >= 20 and desc in raw_text:
        candidates.insert(0, desc)
    for cand in candidates:
        if raw_text.count(cand) == 1:
            quote = cand
            break
    if not quote:
        # Fallback: first non-empty unique slice of raw_text
        for ln in raw_text.splitlines():
            s = ln.strip()
            if len(s) >= 20 and raw_text.count(s) == 1:
                quote = s
                break
    if not quote:
        quote = raw_text.strip()[: min(len(raw_text.strip()), 180)]

    return {
        "title": title[:140],
        "description": (desc or quote)[:300],
        "steps": steps,
        "quote": quote,
    }


def _scan_risk_flags(text: str) -> list[str]:
    flags: list[str] = []
    red = inspect_and_redact_outbound(text[:40_000], redact_pii=False, fail_on_secret_detection=False)
    flags.extend(f"secret:{s}" for s in red.secret_hits)
    for idx, pat in enumerate(_INJECTION_PATTERNS):
        if pat.search(text):
            flags.append(f"injection_pattern_{idx + 1}")
    return flags


def _assert_not_protected_existing_atom(atom_path: Path) -> None:
    """Refuse to overwrite a symlinked atom or an owner-accepted/human-reviewed atom."""
    if os.path.lexists(str(atom_path)) and _is_reparse_or_link(str(atom_path)):
        raise PermissionError(f"Refusing to write through symlinked atom path: {atom_path}")
    if not atom_path.is_file():
        return
    try:
        existing_raw = atom_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PermissionError(f"Cannot inspect existing atom before write: {atom_path}") from exc
    parsed = split_frontmatter(existing_raw)
    fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
    rev_status = str(fm_vals.get("review_status") or "").strip().lower()
    claim_state = str(fm_vals.get("claim_state") or "").strip().lower()
    if rev_status == "accepted" or claim_state == "human_reviewed":
        raise PermissionError(
            f"Refusing to overwrite existing owner-accepted atom: {atom_path.name}"
        )


def _assert_not_protected_existing_playbook(playbook_path: Path) -> None:
    """Refuse to overwrite a symlinked playbook or an active owner-promoted playbook."""
    if os.path.lexists(str(playbook_path)) and _is_reparse_or_link(str(playbook_path)):
        raise PermissionError(f"Refusing to write through symlinked playbook path: {playbook_path}")
    if not playbook_path.is_file():
        return
    try:
        existing_raw = playbook_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PermissionError(f"Cannot inspect existing playbook before write: {playbook_path}") from exc
    parsed = split_frontmatter(existing_raw)
    fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
    status = str(fm_vals.get("status") or "").strip().lower()
    if status == "active":
        raise PermissionError(
            f"Refusing to overwrite existing active playbook: {playbook_path.name}"
        )


def import_skill_pack(
    source_dir: str | Path,
    pack_name: str,
    *,
    brain_dir: str | Path | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Snapshot pack skills into `brain/sources/skill-packs/<pack>/` & stage quarantined pending atoms + draft playbooks."""
    clean_pack = _slugify(pack_name)
    if not _PACK_NAME_RE.fullmatch(clean_pack):
        raise SkillPackImportError(f"Invalid skill pack name: {pack_name!r}")

    raw_src = Path(os.path.expanduser(str(source_dir)))
    if not os.path.lexists(str(raw_src)):
        raise FileNotFoundError(f"Skill pack directory not found: {raw_src}")
    if _is_reparse_or_link(str(raw_src)):
        raise PermissionError(f"Refusing symlinked skill pack root: {raw_src}")
    src_root = raw_src.resolve()
    skills = _collect_skill_markdown_files(src_root)
    if not skills:
        raise SkillPackImportError(f"No SKILL.md or skill markdown files found inside {src_root}")
    if limit is not None and int(limit) > 0:
        skills = skills[: int(limit)]

    raw_brain = Path(
        resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    )
    if os.path.lexists(str(raw_brain)) and _is_reparse_or_link(str(raw_brain)):
        raise PermissionError(f"Refusing symlinked brain directory: {raw_brain}")
    resolved_brain = raw_brain.resolve()

    with vault_writer_lock(str(resolved_brain)):
        for check_sub in ("sources", "sources/skill-packs", "2-atoms", "5-playbooks"):
            p = resolved_brain / check_sub
            if os.path.lexists(str(p)) and _is_reparse_or_link(str(p)):
                raise PermissionError(f"Refusing symlinked vault directory: {p}")
            p.mkdir(parents=True, exist_ok=True)
            if _is_reparse_or_link(str(p)):
                raise PermissionError(f"Refusing symlinked vault directory: {p}")

        pack_sources_dir = resolved_brain / "sources" / "skill-packs" / clean_pack
        if os.path.lexists(str(pack_sources_dir)) and _is_reparse_or_link(str(pack_sources_dir)):
            raise PermissionError(f"Refusing symlinked pack sources dir: {pack_sources_dir}")
        pack_sources_dir.mkdir(parents=True, exist_ok=True)
        if _is_reparse_or_link(str(pack_sources_dir)):
            raise PermissionError(f"Refusing symlinked pack sources dir: {pack_sources_dir}")

        atoms_dir = resolved_brain / "2-atoms"
        playbooks_dir = resolved_brain / "5-playbooks"
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        # Pre-flight check: refuse to overwrite any existing accepted atom, active playbook, or symlinked target
        for slug, _skill_path in skills:
            snap_target = pack_sources_dir / f"{slug}.md"
            if os.path.lexists(str(snap_target)) and _is_reparse_or_link(str(snap_target)):
                raise PermissionError(f"Refusing symlinked snapshot path: {snap_target}")
            _assert_not_protected_existing_atom(atoms_dir / f"pack-{clean_pack}-{slug}.md")
            _assert_not_protected_existing_playbook(playbooks_dir / f"{_slugify(f'{clean_pack}-{slug}')}.md")

        manifest_entries: list[dict[str, Any]] = []
        created_atoms: list[str] = []
        created_playbooks: list[str] = []
        quarantined_count = 0

        for slug, skill_path in skills:
            if _is_reparse_or_link(str(skill_path)):
                raise PermissionError(f"Refusing symlinked skill source file: {skill_path}")
            raw_text = skill_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            if not raw_text.endswith("\n"):
                raw_text += "\n"
            parsed = _parse_skill_doc(raw_text, slug)
            risk_flags = _scan_risk_flags(raw_text)
            review_status = "pending"
            claim_state = "quarantined" if risk_flags else "candidate"
            if risk_flags:
                quarantined_count += 1

            # 1. Immutable snapshot under brain/sources/skill-packs/<pack>/<slug>.md
            snap_name = f"{slug}.md"
            snap_path = pack_sources_dir / snap_name
            if os.path.lexists(str(snap_path)) and _is_reparse_or_link(str(snap_path)):
                raise PermissionError(f"Refusing symlinked snapshot path: {snap_path}")
            snap_bytes = raw_text.encode("utf-8")
            atomic_write_bytes(str(snap_path), snap_bytes)
            source_sha = hashlib.sha256(snap_bytes).hexdigest()
            source_rel = f"sources/skill-packs/{clean_pack}/{snap_name}"

            # 2. Byte-offset-anchored procedural pending/quarantined atom in brain/2-atoms/
            atom_filename = f"pack-{clean_pack}-{slug}.md"
            atom_target = atoms_dir / atom_filename
            _assert_not_protected_existing_atom(atom_target)
            atom_id = f"pack-{clean_pack}-{slug}"
            atom_body = (
                f"## Claim\n"
                f"{parsed['quote']}\n\n"
                f"## Context\n"
                f"- Origin pack: `{clean_pack}` (`origin: imported_unknown`, source: skill pack import).\n"
                f"- Requires explicit owner CAS approval before promotion to recall or active Playbooks.\n\n"
                f"## Evidence\n"
                f"> {parsed['quote']}\n\n"
                f"## Links\n"
            )
            base_fm: dict[str, Any] = {
                "id": atom_id,
                "type": "atom",
                "memory_type": "procedural",
                "title": parsed["title"],
                "review_status": review_status,
                "origin": "imported_unknown",
                "claim_state": claim_state,
                "epistemic_label": "procedure",
                "citation_integrity": "anchored",
                "certainty": "tentative",
                "created": today,
                "last-reviewed": today,
                "importance": 0.45,
                "confidence": 0.40,
                "sources": [source_rel],
                "tags": ["skill-pack", clean_pack, slug],
                "contexts": ["coding"],
                "pack_name": clean_pack,
                "pack_skill": slug,
            }
            if risk_flags:
                base_fm["risk_flags"] = risk_flags

            draft_doc = _render_doc(base_fm, atom_body)
            anchor = find_evidence_anchor(draft_doc, str(resolved_brain))
            base_fm.update(anchor)
            final_atom_doc = _render_doc(base_fm, atom_body)
            atomic_write_bytes(str(atom_target), final_atom_doc.encode("utf-8"))
            created_atoms.append(atom_filename)

            # 3. Draft (NEVER active!) Playbook candidate in brain/5-playbooks/
            pb_id = _slugify(f"{clean_pack}-{slug}")
            pb_filename = f"{pb_id}.md"
            pb_target = playbooks_dir / pb_filename
            _assert_not_protected_existing_playbook(pb_target)
            steps_md = "\n".join(f"{idx}. {step}" for idx, step in enumerate(parsed["steps"], start=1))
            pb_body = (
                f"# {parsed['title']}\n\n"
                f"## Trigger & Applicability\n"
                f"{parsed['description']}\n\n"
                f"## Preconditions & Inputs\n"
                f"- Owner review and CAS promotion required (`status: draft`, imported from `{clean_pack}`).\n\n"
                f"## Ordered Steps\n"
                f"{steps_md}\n\n"
                f"## Expected Outputs & Verification\n"
                f"- Verify source compliance against `{source_rel}`.\n\n"
                f"## Failure Modes & Rollback\n"
                f"- Deprecate or reject draft playbook if external pack workflow conflicts with local invariants.\n"
            )
            pb_fm = {
                "id": pb_id,
                "type": "playbook",
                "title": parsed["title"],
                "status": "draft",
                "origin": "imported_unknown",
                "pack_name": clean_pack,
                "created": today,
                "backing_atoms": [atom_filename],
                "contexts": ["coding"],
                "tags": ["skill-pack", clean_pack],
            }
            pb_doc = _render_doc(pb_fm, pb_body)
            atomic_write_bytes(str(pb_target), pb_doc.encode("utf-8"))
            created_playbooks.append(pb_filename)

            manifest_entries.append(
                {
                    "slug": slug,
                    "title": parsed["title"],
                    "source_rel": source_rel,
                    "source_sha256": source_sha,
                    "atom_filename": atom_filename,
                    "playbook_filename": pb_filename,
                    "review_status": review_status,
                    "claim_state": claim_state,
                    "risk_flags": risk_flags,
                }
            )

        manifest_path = pack_sources_dir / "manifest.json"
        if os.path.lexists(str(manifest_path)) and _is_reparse_or_link(str(manifest_path)):
            raise PermissionError(f"Refusing symlinked manifest path: {manifest_path}")
        manifest = {
            "schema_version": 1,
            "pack_name": clean_pack,
            "source_dir": str(src_root),
            "imported_at": _now_iso(),
            "count": len(manifest_entries),
            "quarantined_count": quarantined_count,
            "skills": manifest_entries,
        }
        atomic_write_bytes(
            str(manifest_path),
            (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
        )
    return {
        "status": "imported_quarantined",
        "pack_name": clean_pack,
        "source_dir": str(src_root),
        "imported_count": len(manifest_entries),
        "quarantined_count": quarantined_count,
        "atoms_created": created_atoms,
        "playbooks_drafted": created_playbooks,
        "manifest_path": str(pack_sources_dir / "manifest.json"),
    }


def list_imported_packs(*, brain_dir: str | Path | None = None) -> dict[str, Any]:
    raw_brain = Path(
        resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    )
    if os.path.lexists(str(raw_brain)) and _is_reparse_or_link(str(raw_brain)):
        raise PermissionError(f"Refusing symlinked brain directory: {raw_brain}")
    resolved_brain = raw_brain.resolve()
    root = resolved_brain / "sources" / "skill-packs"
    packs: list[dict[str, Any]] = []
    if root.is_dir() and not _is_reparse_or_link(str(root)):
        for sub in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            mp = sub / "manifest.json"
            if sub.is_dir() and mp.is_file() and not _is_reparse_or_link(str(mp)):
                try:
                    data = json.loads(mp.read_text(encoding="utf-8"))
                    packs.append(
                        {
                            "pack_name": data.get("pack_name") or sub.name,
                            "count": int(data.get("count") or 0),
                            "quarantined_count": int(data.get("quarantined_count") or 0),
                            "imported_at": data.get("imported_at"),
                            "skills": data.get("skills") or [],
                        }
                    )
                except Exception:
                    continue
    return {
        "discovered_local": discover_default_skill_packs(),
        "imported_packs": packs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("review", help="List discovered local packs & quarantined imported pack manifests")
    sub.add_parser("status", help="Alias for review")
    p_imp = sub.add_parser("import", help="Import a local skill pack directory as quarantined pending drafts")
    p_imp.add_argument("--name", required=True, help="Pack identifier (e.g. agent-skills, pm-skills)")
    p_imp.add_argument("--path", default="", help="Path to local pack directory (auto-discovers defaults if omitted)")
    p_imp.add_argument("--limit", type=int, default=0)

    args = parser.parse_args(argv)
    if args.cmd in ("review", "status"):
        print(json.dumps(list_imported_packs(), indent=2, ensure_ascii=False))
        return 0
    src = args.path.strip() or (discover_default_skill_packs().get(args.name) or "")
    if not src:
        raise SkillPackImportError(f"Could not auto-locate pack '{args.name}'; pass --path <dir>.")
    res = import_skill_pack(src, args.name, limit=(args.limit or None))
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
