"""Single capture policy used by distillation, approval, and MCP write tools."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_POLICY_PATH = Path(__file__).resolve().parents[1] / "docs" / "capture_policy.json"
_DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "CAPTURE_POLICY.md"
POLICY_URI = "brain://policy/capture"
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:password|passwd|api[_-]?key|secret|access[_-]?token)\s*[:=]\s*\S+"
)


@lru_cache(maxsize=1)
def load_policy() -> dict:
    return json.loads(_POLICY_PATH.read_text(encoding="utf-8"))


def policy_markdown() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


def max_claim_words() -> int:
    return int(load_policy()["max_claim_words"])


def max_atoms_per_note() -> int:
    return int(load_policy()["max_atoms_per_note"])


def prompt_text() -> str:
    return str(load_policy()["prompt"])


def agent_brief() -> str:
    policy = load_policy()
    return (
        "Capture policy: store one durable checkable fact per atom "
        f"({', '.join(policy['qualifies'][:6])}). "
        f"Reject {', '.join(policy['rejects'][:6])}. "
        f"Claims are at most {policy['max_claim_words']} words, use absolute dates, "
        "and keep a verbatim evidence span. Uncertain facts stay tentative and are never auto-approved. "
        f"Read {POLICY_URI}."
    )


def tool_hint() -> str:
    return (
        " Follow the capture policy at brain://policy/capture: one durable checkable fact, "
        "no secrets, no mood, no fragments, absolute dates. A rejection lists the reasons so you can resubmit."
    )


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text or "")


def rejection_reasons(text: str) -> list[str]:
    """Reasons this text cannot be stored as a memory. An empty list means it passes."""
    policy = load_policy()
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    reasons: list[str] = []
    if len(raw) < int(policy["min_fact_chars"]):
        reasons.append("fragment: shorter than the minimum fact length")
    words = _words(raw)
    dangling = {item.casefold() for item in policy["dangling_endings"]}
    if words and words[-1] == words[-1].casefold() and words[-1].rstrip(".") in dangling:
        reasons.append("fragment: sentence ends in a function word")
    elif len(words) < 3 and not re.search(r"\d", raw):
        reasons.append("fragment: too little content to be a fact")
    if re.match(r"^#{1,6}\s+\S", raw.lstrip()):
        reasons.append("markup: heading")
    if raw.startswith("```") or raw.strip() in {"{", "}", "};"}:
        reasons.append("markup: code fence or brace")
    if raw.endswith(":") and not raw.casefold().startswith("decision:"):
        reasons.append("markup: lead-in with no fact after the colon")
    if re.search(r"\|?\s*:?-{3,}", raw) and "|" in raw:
        reasons.append("markup: table separator")
    folded = raw.casefold()
    if any(pattern in folded for pattern in policy["mood_patterns"]) and not re.search(r"\d|[\\/]", raw):
        reasons.append("mood: feeling with no checkable fact")
    for phrase in policy["relative_dates"]:
        if re.search(rf"\b{re.escape(phrase)}\b", folded):
            reasons.append(f"relative-date: {phrase} is not an absolute date")
            break
    if _SECRET_ASSIGNMENT_RE.search(raw):
        reasons.append("secret: password, key, or token assignment")
    else:
        from scripts.inbox_workflow import scan_inbox_payload_risks

        secret_flags = [flag for flag in scan_inbox_payload_risks(raw) if str(flag).startswith("secret_credential")]
        if secret_flags:
            reasons.append("secret: " + ", ".join(secret_flags))
    if len(words) > int(policy["max_claim_words"]):
        reasons.append(f"shape: claim is longer than {policy['max_claim_words']} words")
    speculative = bool(re.match(r"(?i)\s*(?:i think|i guess|maybe|probably|might be)\b", raw))
    if speculative and "tentative:" not in folded:
        reasons.append("speculation: mark it tentative or state the checkable fact")
    return reasons
