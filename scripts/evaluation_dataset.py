"""Validate versioned retrieval and answerability evaluation datasets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable


class EvaluationDatasetError(ValueError):
    """Raised when an evaluation dataset is ambiguous or unsafe to score."""


SPLITS = {"regression", "development", "holdout"}
ANSWERABILITY = {"answerable", "unanswerable", "exploratory", "preference"}


def _is_atom_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.endswith(".md")
        and Path(value).name == value
        and value not in {".", ".."}
    )


def _resolve_atom_path(atoms_root: Path, filename: str) -> Path | None:
    try:
        path = (atoms_root / filename).resolve(strict=True)
    except OSError:
        return None
    if path.parent != atoms_root or not path.is_file():
        return None
    return path


def validate_dataset(
    dataset: dict,
    *,
    atoms_dir: str | Path,
    approval_check: Callable[[Path], bool],
    bind_files: bool = True,
) -> dict:
    """Validate labels and bind answerable cases to approved atom bytes.

    Candidate relevance labels may intentionally target pending atoms only for
    exploratory retrieval cases. Answerable cases must instead identify exact
    current atom revisions that pass the supplied owner-approval verifier.
    """
    errors: list[str] = []
    if not isinstance(dataset, dict):
        raise EvaluationDatasetError("Dataset root must be an object.")
    if dataset.get("schema_version") != 2:
        errors.append("schema_version must be 2.")
    for field in ("dataset_id", "dataset_version"):
        if not isinstance(dataset.get(field), str) or not dataset[field].strip():
            errors.append(f"{field} must be a non-empty string.")
    status = dataset.get("status")
    if status not in {"owner_approved", "pending_owner_review"}:
        errors.append("status must be owner_approved or pending_owner_review.")
    if not isinstance(dataset.get("purpose"), str) or not dataset["purpose"].strip():
        errors.append("purpose must be a non-empty string.")
    if status == "owner_approved":
        approval = dataset.get("owner_approval")
        if not isinstance(approval, dict) or not all(
            isinstance(approval.get(field), str) and approval[field].strip()
            for field in ("actor", "date", "scope")
        ):
            errors.append("owner_approval must include actor, date, and scope.")
    elif status == "pending_owner_review":
        # Agent-drafted labels are never presented as owner decisions; they stay provisional
        # until the owner reviews them and records an explicit owner_approval block.
        if dataset.get("owner_approval"):
            errors.append("pending_owner_review datasets must not carry an owner_approval block.")
        draft = dataset.get("draft_provenance")
        if not isinstance(draft, dict) or not all(
            isinstance(draft.get(field), str) and draft[field].strip()
            for field in ("drafted_by", "date", "scope")
        ):
            errors.append("draft_provenance must include drafted_by, date, and scope.")

    queries = dataset.get("queries")
    if not isinstance(queries, list) or not queries:
        errors.append("queries must be a non-empty list.")
        queries = []

    seen_ids: set[str] = set()
    atoms_root = Path(atoms_dir).resolve()
    for index, case in enumerate(queries):
        label = f"queries[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{label} must be an object.")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"{label}.id must be a non-empty string.")
        elif case_id in seen_ids:
            errors.append(f"duplicate query id: {case_id}.")
        else:
            seen_ids.add(case_id)
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            errors.append(f"{label}.query must be a non-empty string.")
        if not isinstance(case.get("expected_behavior"), str) or not case["expected_behavior"].strip():
            errors.append(f"{label}.expected_behavior must be a non-empty string.")
        if not isinstance(case.get("category"), str) or not case["category"].strip():
            errors.append(f"{label}.category must be a non-empty string.")
        if not isinstance(case.get("split"), str) or case["split"] not in SPLITS:
            errors.append(f"{label}.split must be one of {sorted(SPLITS)}.")
        scope = case.get("project_scope")
        if scope is not None and (not isinstance(scope, str) or not scope.strip()):
            errors.append(f"{label}.project_scope must be null or a non-empty string.")

        relevance = case.get("candidate_relevance")
        if not isinstance(relevance, list) or any(not _is_atom_filename(item) for item in relevance):
            errors.append(f"{label}.candidate_relevance must be a list of atom filenames.")
            relevance = []
        elif len(relevance) != len(set(relevance)):
            errors.append(f"{label}.candidate_relevance must not contain duplicates.")
        for filename in relevance:
            if bind_files and _resolve_atom_path(atoms_root, filename) is None:
                errors.append(f"{label} candidate atom does not exist: {filename}.")

        answerability = case.get("answerability")
        if not isinstance(answerability, str) or answerability not in ANSWERABILITY:
            errors.append(f"{label}.answerability must be one of {sorted(ANSWERABILITY)}.")
            continue
        expected = case.get("expected_atom_ids")
        acceptable = case.get("acceptable_atom_ids")
        claims = case.get("expected_claims")
        revisions = case.get("expected_atom_revisions")
        if not isinstance(expected, list) or any(not _is_atom_filename(item) for item in expected):
            errors.append(f"{label}.expected_atom_ids must be a list of atom filenames.")
            expected = []
        if not isinstance(acceptable, list) or any(not _is_atom_filename(item) for item in acceptable):
            errors.append(f"{label}.acceptable_atom_ids must be a list of atom filenames.")
            acceptable = []
        if len(expected + acceptable) != len(set(expected + acceptable)):
            errors.append(f"{label} expected and acceptable atom IDs must be unique.")
        if not isinstance(claims, list):
            errors.append(f"{label}.expected_claims must be a list.")
            claims = []
        if any(not isinstance(claim, dict) for claim in claims):
            errors.append(f"{label}.expected_claims entries must be objects.")
            claims = [claim for claim in claims if isinstance(claim, dict)]
        if not isinstance(revisions, list):
            errors.append(f"{label}.expected_atom_revisions must be a list.")
            revisions = []
        if any(not isinstance(revision, dict) for revision in revisions):
            errors.append(f"{label}.expected_atom_revisions entries must be objects.")
            revisions = [revision for revision in revisions if isinstance(revision, dict)]

        if answerability == "answerable":
            if case.get("answer_mode") != "standard":
                errors.append(f"{label} answerable cases require answer_mode=standard.")
            if case.get("expected_behavior") != "answer_with_evidence":
                errors.append(f"{label} answerable cases require expected_behavior=answer_with_evidence.")
            if not expected or not claims:
                errors.append(f"{label} answerable cases require expected atoms and expected claims.")
            if not set(expected).issubset(set(relevance)):
                errors.append(f"{label} expected atoms must also appear in candidate_relevance.")
            if not set(acceptable).issubset(set(relevance)):
                errors.append(f"{label} acceptable atoms must also appear in candidate_relevance.")
            if case.get("trap_reason") is not None:
                errors.append(f"{label} answerable cases must have trap_reason=null.")
        elif answerability == "unanswerable":
            if case.get("answer_mode") != "standard" or case.get("expected_behavior") != "abstain":
                errors.append(f"{label} unanswerable cases require standard mode and abstain behavior.")
            if expected or acceptable or claims or revisions:
                errors.append(f"{label} unanswerable cases cannot declare target atoms, claims, or revisions.")
            if relevance:
                errors.append(f"{label} unanswerable cases cannot declare candidate relevance.")
            if not isinstance(case.get("trap_reason"), str) or not case["trap_reason"].strip():
                errors.append(f"{label} unanswerable cases require a non-empty trap_reason.")
        elif answerability == "preference":
            if case.get("answer_mode") != "preference":
                errors.append(f"{label} preference cases require answer_mode=preference.")
            if case.get("expected_behavior") != "preference_recall":
                errors.append(f"{label} preference cases require expected_behavior=preference_recall.")
            if not relevance:
                errors.append(f"{label} preference cases require candidate_relevance.")
            if case.get("trap_reason") is not None:
                errors.append(f"{label} preference cases must have trap_reason=null.")
            if not expected:
                if acceptable or claims or revisions:
                    errors.append(f"{label} preference cases without expected atoms cannot declare acceptable atoms, claims, or revisions.")
            else:
                if not set(expected).issubset(set(relevance)):
                    errors.append(f"{label} expected atoms must also appear in candidate_relevance.")
                if not set(acceptable).issubset(set(relevance)):
                    errors.append(f"{label} acceptable atoms must also appear in candidate_relevance.")
        else:
            if case.get("answer_mode") != "candidate_retrieval" or case.get("expected_behavior") != "not_scored":
                errors.append(f"{label} exploratory cases require candidate_retrieval mode and not_scored behavior.")
            if expected or acceptable or claims or revisions:
                errors.append(f"{label} exploratory cases cannot declare answer claims or approved target revisions.")
            if not relevance:
                errors.append(f"{label} exploratory cases require candidate_relevance labels.")
            if case.get("trap_reason") is not None:
                errors.append(f"{label} exploratory cases must have trap_reason=null.")

        requires_revision_binding = answerability == "answerable" or (
            answerability == "preference" and bool(expected)
        )
        if not requires_revision_binding:
            continue
        revision_map: dict[str, str] = {}
        for revision in revisions:
            atom_id = revision.get("atom_id")
            digest = revision.get("revision_sha256")
            if not _is_atom_filename(atom_id):
                errors.append(f"{label} expected atom revision has invalid atom_id.")
                continue
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                errors.append(f"{label} expected revision for {atom_id} must be a lowercase SHA-256.")
                continue
            if atom_id in revision_map:
                errors.append(f"{label} has duplicate revision metadata for {atom_id}.")
                continue
            revision_map[atom_id] = digest
            if not bind_files:
                continue
            atom_path = _resolve_atom_path(atoms_root, atom_id)
            if atom_path is None:
                errors.append(f"{label} expected atom does not exist inside the atom directory: {atom_id}.")
                continue
            try:
                current_digest = hashlib.sha256(atom_path.read_bytes()).hexdigest()
            except OSError:
                errors.append(f"{label} expected atom does not exist: {atom_id}.")
                continue
            if current_digest != digest:
                errors.append(f"{label} revision hash is stale for {atom_id}.")
            else:
                try:
                    approved = approval_check(atom_path)
                except Exception as exc:
                    errors.append(f"{label} approval check failed for {atom_id}: {exc}.")
                else:
                    if not approved:
                        errors.append(f"{label} target is not currently owner-approved: {atom_id}.")
        required_revision_ids = set(expected) | set(acceptable)
        if set(revision_map) != required_revision_ids:
            errors.append(f"{label} revision metadata must cover exactly expected and acceptable atoms.")
        for claim_index, claim in enumerate(claims):
            claim_label = f"{label}.expected_claims[{claim_index}]"
            if not isinstance(claim.get("text"), str) or not claim["text"].strip():
                errors.append(f"{claim_label} must contain non-empty text.")
                continue
            supporters = claim.get("supported_by")
            if (
                not isinstance(supporters, list)
                or not supporters
                or any(not isinstance(item, str) for item in supporters)
                or len(supporters) != len(set(supporters))
                or any(item not in revision_map for item in supporters)
            ):
                errors.append(f"{claim_label}.supported_by must reference declared approved atom revisions.")

    if errors:
        raise EvaluationDatasetError("Evaluation dataset is invalid:\n- " + "\n- ".join(errors))
    return dataset


def load_dataset(
    path: str | Path,
    *,
    atoms_dir: str | Path,
    approval_check: Callable[[Path], bool],
    bind_files: bool = True,
) -> dict:
    """Read JSON and validate it before any benchmark score is calculated."""
    try:
        dataset = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDatasetError(f"Could not read evaluation dataset: {exc}") from exc
    return validate_dataset(
        dataset,
        atoms_dir=atoms_dir,
        approval_check=approval_check,
        bind_files=bind_files,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from collections import Counter

    scripts_dir = Path(__file__).resolve().parent
    root_dir = scripts_dir.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from config import ATOMS_DIR, BRAIN_DIR
    from review_atom import approved_revision_is_current

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=str(root_dir / "docs" / "retrieval-evaluation-proposed.json"),
        help="Path to the curated evaluation dataset JSON.",
    )
    parser.add_argument("--atoms-dir", default=ATOMS_DIR, help="Path to canonical atoms directory.")
    parser.add_argument("--brain-dir", default=BRAIN_DIR, help="Path to vault root directory.")
    parser.add_argument(
        "--require-all-splits",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require non-empty regression, development, and holdout splits.",
    )
    args = parser.parse_args(argv)

    atoms_present = Path(args.atoms_dir).is_dir()
    try:
        validated = load_dataset(
            args.dataset,
            atoms_dir=args.atoms_dir,
            approval_check=lambda p: approved_revision_is_current(
                str(p), brain_dir=args.brain_dir, atoms_dir=args.atoms_dir
            ),
            bind_files=atoms_present,
        )
    except EvaluationDatasetError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    queries = validated.get("queries", [])
    split_counts = Counter(q.get("split") for q in queries)
    answerability_counts = Counter(q.get("answerability") for q in queries)

    if args.require_all_splits:
        missing_splits = sorted(SPLITS - {s for s, c in split_counts.items() if c > 0})
        if missing_splits:
            print(f"Missing required non-empty dataset splits: {', '.join(missing_splits)}", file=sys.stderr)
            return 1

    summary = {
        "status": "valid",
        "dataset_id": validated.get("dataset_id"),
        "dataset_version": validated.get("dataset_version"),
        "query_count": len(queries),
        "splits": dict(sorted(split_counts.items())),
        "answerability": dict(sorted(answerability_counts.items())),
        "atom_binding": "checked" if atoms_present else "skipped_private_vault_absent",
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
