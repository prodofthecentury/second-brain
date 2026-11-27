"""Extraction, exact scoring, and cluster-aware statistics for the reasoning benchmark.

Tool answers are always compared as exact Fractions. Model answers may use an
item's ``approx_ok`` tolerance. Nothing in this module calls a model.
"""

from __future__ import annotations

import math
import random
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from statistics import NormalDist
from typing import Any, Iterable, Sequence


ANSWER_LINE_RE = re.compile(r"^\s*ANSWER\s*:\s*(.*?)\s*$", re.IGNORECASE)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
NUMBER_TOKEN_RE = re.compile(
    r"[+-]?(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.\d+)?(?:\s*/\s*[+-]?\d+)?%?"
)
UNIT_WORDS = {
    "bag", "bags", "pen", "pens", "pizza", "pizzas", "dollar", "dollars",
    "gb", "mb", "kb", "km", "m", "cm", "mm", "kg", "g", "h", "hr", "hrs",
    "hour", "hours", "minute", "minutes", "second", "seconds", "day", "days",
    "pupil", "pupils", "people", "person", "litre", "litres", "liter", "liters",
    "cup", "cups", "percent", "percentage",
}
ABSTAIN_WORDS = re.compile(
    r"\b(unknown|cannot|can't|can not|not enough|insufficient|abstain|unanswerable)\b",
    re.IGNORECASE,
)
YES_WORDS = {"yes", "true", "y"}
NO_WORDS = {"no", "false", "n"}


class ScoreError(ValueError):
    pass


def strip_think(text: str) -> str:
    return THINK_RE.sub("", text or "")


def _strip_wrapping(text: str) -> str:
    value = strip_think(text or "").strip()
    value = value.strip("`*_ \t\"'")
    value = re.sub(r"^(?:the answer is|answer)\s*[:=]\s*", "", value, flags=re.IGNORECASE)
    return value.strip()


def parse_fraction(text: str) -> Fraction | None:
    """Parse a numeric token into an exact Fraction. Never uses binary float."""
    raw = _strip_wrapping(text)
    if not raw:
        return None
    raw = raw.replace("−", "-").replace("–", "-")
    raw = raw.replace("$", "").replace("£", "").replace("€", "").replace(",", "")
    raw = raw.replace("**", "").replace("*", "")
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw.endswith("%"):
        # Stripping the percent sign leaves the written number: 50% is 50, not 1/2.
        raw = raw[:-1].strip()
    raw = re.sub(
        r"\s+(?:" + "|".join(sorted(UNIT_WORDS, key=len, reverse=True)) + r")\b\.?$",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    mixed = re.fullmatch(r"([+-]?\d+)\s+(\d+)\s*/\s*(\d+)", raw)
    if mixed:
        whole = int(mixed.group(1))
        num = int(mixed.group(2))
        den = int(mixed.group(3))
        if den == 0:
            return None
        sign = -1 if whole < 0 else 1
        value = Fraction(abs(whole) * den + num, den) * sign
        return value
    slash = re.fullmatch(r"([+-]?\d+)\s*/\s*([+-]?\d+)", raw)
    if slash:
        den = int(slash.group(2))
        if den == 0:
            return None
        return Fraction(int(slash.group(1)), den)
    if re.fullmatch(r"[+-]?\d+", raw):
        return Fraction(int(raw), 1)
    if re.fullmatch(r"[+-]?\d+\.\d+", raw):
        return Fraction(Decimal(raw))
    return None


def _gold_fraction(answer: Any) -> Fraction | None:
    if answer is None:
        return None
    if isinstance(answer, bool):
        return None
    if isinstance(answer, int):
        return Fraction(answer, 1)
    if isinstance(answer, float):
        return Fraction(Decimal(str(answer)))
    if isinstance(answer, Fraction):
        return answer
    return parse_fraction(str(answer))


def _within_tolerance(got: Fraction, gold: Fraction, approx_ok: dict | None, *, tool: bool) -> bool:
    if got == gold:
        return True
    if tool or not approx_ok:
        return False
    if "decimals" in approx_ok:
        places = int(approx_ok["decimals"])
        quantum = Decimal("1").scaleb(-places)
        got_r = Decimal(got.numerator) / Decimal(got.denominator)
        gold_r = Decimal(gold.numerator) / Decimal(gold.denominator)
        return got_r.quantize(quantum, rounding=ROUND_HALF_UP) == gold_r.quantize(quantum, rounding=ROUND_HALF_UP)
    if "rel" in approx_ok:
        rel = Fraction(Decimal(str(approx_ok["rel"])))
        if gold == 0:
            return abs(got) <= rel
        return abs(got - gold) / abs(gold) <= rel
    return False


def numbers_equal(got_text: str, gold: Any, answer_type: str, approx_ok: dict | None, *, tool: bool) -> bool:
    got = parse_fraction(got_text)
    target = _gold_fraction(gold)
    if got is None or target is None:
        return False
    # "as a decimal" is a different answer from the unreduced fraction, even when the values match.
    if answer_type == "decimal" and "/" in str(got_text):
        return False
    if answer_type == "integer" and target.denominator != 1:
        return False
    if answer_type == "integer" and not tool:
        # 52.0 matches an integer gold; 52.5 does not, even before tolerance.
        if approx_ok is None and got.denominator != 1 and got != target:
            return False
    return _within_tolerance(got, target, approx_ok, tool=tool)


_BARE_NUMBER_RE = re.compile(
    r"[+-]?(?:\d+(?:\.\d+)?|\d+\s*/\s*\d+|\d+\s+\d+\s*/\s*\d+)%?"
)


def _whole_string_parses(text: str, answer_type: str, aliases: Sequence[str] | None = None) -> bool:
    """True only when the entire stripped reply is already the answer type."""
    raw = (text or "").strip()
    if not raw or "\n" in raw:
        return False
    if answer_type in {"integer", "decimal", "fraction"}:
        cleaned = raw.replace("$", "").replace("£", "").replace("€", "").replace(",", "").strip("`*_ \t")
        return _BARE_NUMBER_RE.fullmatch(cleaned) is not None and parse_fraction(cleaned) is not None
    if answer_type == "yesno":
        return raw.casefold().rstrip(".") in YES_WORDS | NO_WORDS
    if answer_type == "date":
        return _parse_date(raw) is not None
    if answer_type == "entity":
        names = list(aliases or [])
        return _entity_exact(raw, names) is not None
    if answer_type == "sequence":
        return _split_sequence(raw) is not None
    if answer_type == "assignment":
        return _parse_assignment(raw) is not None
    return False


def _parse_date(text: str) -> str | None:
    raw = _strip_wrapping(text)
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    # New Zealand day-first convention.
    dmy = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", raw)
    if dmy:
        day, month, year = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _entity_exact(text: str, aliases: Sequence[str]) -> str | None:
    raw = _strip_wrapping(text)
    folded = raw.casefold()
    for alias in aliases:
        if folded == str(alias).casefold():
            return str(alias)
    return None


def _entity_fallback(text: str, aliases: Sequence[str], distractors: Sequence[str]) -> str | None:
    raw = strip_think(text or "")
    hits = []
    for alias in aliases:
        if re.search(rf"\b{re.escape(str(alias))}\b", raw, flags=re.IGNORECASE):
            hits.append(str(alias))
    if len(set(h.casefold() for h in hits)) != 1:
        return None
    chosen = hits[0]
    for distractor in distractors:
        if re.search(rf"\b{re.escape(str(distractor))}\b", raw, flags=re.IGNORECASE):
            if str(distractor).casefold() != chosen.casefold():
                return None
    return chosen


def _split_sequence(text: str) -> list[str] | None:
    raw = _strip_wrapping(text)
    if not raw:
        return None
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    parts = [part.strip().strip("'\"") for part in raw.split(",")]
    parts = [part for part in parts if part]
    return parts or None


def _parse_assignment(text: str) -> dict[str, str] | None:
    raw = _strip_wrapping(text)
    if raw.startswith("{") and raw.endswith("}"):
        body = raw[1:-1]
        pieces = [part.strip() for part in body.split(",")]
    else:
        pieces = [part.strip() for part in re.split(r",|\n", raw) if part.strip()]
    found: dict[str, str] = {}
    for piece in pieces:
        match = re.fullmatch(r"([A-Za-z0-9_]+)\s*[:=]\s*([A-Za-z]+)", piece.strip())
        if not match:
            return None
        found[match.group(1)] = match.group(2).casefold()
    return found or None


def _last_number_token(text: str) -> str | None:
    matches = list(NUMBER_TOKEN_RE.finditer(strip_think(text or "")))
    if not matches:
        return None
    return matches[-1].group(0)


def _last_date_token(text: str) -> str | None:
    matches = list(re.finditer(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}", strip_think(text or "")))
    if not matches:
        return None
    return matches[-1].group(0)


def _is_refusal_text(text: str) -> bool:
    """A refusal is the whole reply, not the word 'unknown' inside an answer."""
    raw = (text or "").strip().strip(".")
    if not raw or re.search(r"\d", raw):
        return False
    return re.fullmatch(
        r"(?i)(?:unknown|abstain|not enough(?: information)?|i (?:cannot|can't|can not|do not|don't) (?:verify|know|tell)(?: an answer)?(?: for this)?)",
        raw,
    ) is not None


def extract_answer(text: str, item: dict, *, engine_abstained: bool = False, engine_value: bool = False) -> dict[str, Any]:
    """Return extraction metadata. Precedence is the spec's five steps."""
    if engine_abstained:
        return {"value": "ABSTAIN", "extraction": "engine", "format_ok": True}
    raw = strip_think(text or "")
    lines = raw.splitlines()
    answer_lines = []
    for line in lines:
        match = ANSWER_LINE_RE.match(line)
        if match:
            answer_lines.append(match.group(1).strip())
    if answer_lines:
        chosen = answer_lines[-1]
        if _is_refusal_text(chosen):
            return {"value": "ABSTAIN", "extraction": "strict", "format_ok": True}
        return {"value": chosen, "extraction": "strict", "format_ok": True}
    if not answer_lines and _is_refusal_text(raw.strip()):
        return {"value": "ABSTAIN", "extraction": "engine" if engine_value else "fallback", "format_ok": False}
    answer_type = str(item.get("answer_type") or "")
    aliases = list(item.get("aliases") or [])
    stripped = raw.strip()
    if engine_value and stripped and _whole_string_parses(stripped, answer_type, aliases):
        return {"value": stripped, "extraction": "engine", "format_ok": True}
    if answer_type in {"integer", "decimal", "fraction"}:
        token = _last_number_token(raw)
        if token is not None:
            return {"value": token, "extraction": "fallback", "format_ok": False}
    elif answer_type == "date":
        token = _last_date_token(raw)
        if token is not None:
            return {"value": token, "extraction": "fallback", "format_ok": False}
    elif answer_type == "entity":
        hit = _entity_fallback(raw, aliases, list(item.get("distractors") or []))
        if hit is not None:
            return {"value": hit, "extraction": "fallback", "format_ok": False}
    elif answer_type == "yesno":
        match = re.search(r"\b(yes|no|true|false)\b", raw, re.IGNORECASE)
        if match:
            return {"value": match.group(1), "extraction": "fallback", "format_ok": False}
    return {"value": "NONE", "extraction": "none", "format_ok": False}


def _normalize_sequence(value: Any) -> list[str] | None:
    if isinstance(value, list):
        return [str(part).strip() for part in value]
    if isinstance(value, str):
        return _split_sequence(value)
    return None


def _normalize_assignment(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict):
        return {str(key): str(val).casefold() for key, val in value.items()}
    if isinstance(value, str):
        return _parse_assignment(value)
    return None


def score_prediction(
    predicted: str,
    item: dict,
    *,
    tool: bool = False,
    extraction: str = "strict",
) -> dict[str, Any]:
    """Score one extracted prediction. ``tool=True`` ignores ``approx_ok``."""
    answer_type = str(item.get("answer_type") or "")
    gold = item.get("answer")
    approx_ok = item.get("approx_ok")
    if predicted == "NONE":
        return {"correct": False, "abstained": False, "reason": "none"}
    if predicted == "ABSTAIN" or (isinstance(predicted, str) and predicted.casefold() == "unknown"):
        correct = (not item.get("answerable", True)) or answer_type == "unanswerable"
        return {"correct": correct, "abstained": True, "reason": "abstain"}
    if answer_type == "unanswerable":
        return {"correct": False, "abstained": False, "reason": "should_abstain"}
    if answer_type in {"integer", "decimal", "fraction"}:
        ok = numbers_equal(predicted, gold, answer_type, approx_ok, tool=tool)
        return {"correct": ok, "abstained": False, "reason": "number" if ok else "number_mismatch"}
    if answer_type == "yesno":
        token = predicted.strip().casefold().rstrip(".")
        gold_token = str(gold).strip().casefold()
        yes = token in YES_WORDS
        no = token in NO_WORDS
        gold_yes = gold_token in YES_WORDS
        gold_no = gold_token in NO_WORDS
        ok = (yes and gold_yes) or (no and gold_no)
        return {"correct": ok, "abstained": False, "reason": "yesno"}
    if answer_type == "date":
        ok = _parse_date(predicted) is not None and _parse_date(predicted) == _parse_date(str(gold))
        return {"correct": ok, "abstained": False, "reason": "date"}
    if answer_type == "entity":
        aliases = list(item.get("aliases") or [])
        if gold is not None and str(gold) not in aliases:
            aliases = [str(gold), *aliases]
        exact = _entity_exact(predicted, aliases)
        if exact is None and extraction == "fallback":
            exact = _entity_fallback(predicted, aliases, list(item.get("distractors") or []))
        return {"correct": exact is not None, "abstained": False, "reason": "entity"}
    if answer_type == "sequence":
        got = _normalize_sequence(predicted)
        target = _normalize_sequence(gold)
        ok = got is not None and target is not None and [part.casefold() for part in got] == [part.casefold() for part in target]
        return {"correct": ok, "abstained": False, "reason": "sequence"}
    if answer_type == "assignment":
        got = _normalize_assignment(predicted)
        target = _normalize_assignment(gold)
        ok = got is not None and target is not None and got == target
        return {"correct": ok, "abstained": False, "reason": "assignment"}
    if answer_type == "code":
        return {"correct": False, "abstained": False, "reason": "code_uses_tests"}
    return {"correct": False, "abstained": False, "reason": "unsupported_type"}


def type_normalized_equal(left: Any, right: Any) -> bool:
    """Logic solutions compare after type normalization, so 357 == '357'."""
    if left == right:
        return True
    if isinstance(left, int) and not isinstance(left, bool) and str(left) == str(right).strip():
        return True
    if isinstance(right, int) and not isinstance(right, bool) and str(right) == str(left).strip():
        return True
    if isinstance(left, list) or isinstance(right, list):
        return _normalize_sequence(left) == _normalize_sequence(right)
    if isinstance(left, dict) or isinstance(right, dict):
        return _normalize_assignment(left) == _normalize_assignment(right)
    return str(left).strip() == str(right).strip()


# --- statistics -----------------------------------------------------------------


def wilson_interval(k: int, n: int, *, z: float | None = None) -> tuple[float, float]:
    """Item-level Wilson score interval. Ignores template clustering."""
    if n <= 0:
        return (0.0, 1.0)
    if z is None:
        z = NormalDist().inv_cdf(0.975)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt((p * (1.0 - p) / n) + z2 / (4.0 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value (binomial test on discordant pairs)."""
    n = b + c
    if n == 0:
        return 1.0
    probs = [_binom_pmf(n, i) for i in range(n + 1)]
    observed = probs[b]
    total = sum(pr for pr in probs if pr <= observed + 1e-15)
    return min(1.0, total)


def _binom_pmf(n: int, k: int) -> float:
    return math.comb(n, k) / (2 ** n)


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values, returned in the original order."""
    indexed = sorted(enumerate(p_values), key=lambda pair: pair[1])
    m = len(indexed)
    adjusted = [1.0] * m
    running = 0.0
    for rank, (original, pval) in enumerate(indexed):
        factor = m - rank
        running = max(running, min(1.0, factor * float(pval)))
        adjusted[original] = running
    return adjusted


def _grouped(rows: Sequence[dict], value_key: str = "correct") -> dict[str, list[float]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        groups[str(row["template_id"])].append(float(row[value_key]))
    return dict(groups)


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def design_effect(rows: Sequence[dict], value_key: str = "correct") -> dict[str, float]:
    """ANOVA design effect. A single instance per template, or zero variance, is 1."""
    groups = _grouped(rows, value_key)
    if not groups:
        return {"deff": 1.0, "n_eff": 0.0, "rho": 0.0, "m_bar": 0.0}
    sizes = [len(values) for values in groups.values()]
    n = sum(sizes)
    g = len(groups)
    m_bar = n / g
    if m_bar <= 1.0 or n <= 1:
        return {"deff": 1.0, "n_eff": float(n), "rho": 0.0, "m_bar": m_bar}
    all_values = [v for values in groups.values() for v in values]
    grand = _mean(all_values)
    if all(v == grand for v in all_values):
        return {"deff": 1.0, "n_eff": float(n), "rho": 0.0, "m_bar": m_bar}
    ss_within = 0.0
    ss_between = 0.0
    for values in groups.values():
        local = _mean(values)
        ss_between += len(values) * (local - grand) ** 2
        ss_within += sum((v - local) ** 2 for v in values)
    df_b = g - 1
    df_w = n - g
    msb = ss_between / df_b if df_b else 0.0
    msw = ss_within / df_w if df_w else 0.0
    if msb == 0.0 and msw == 0.0:
        rho = 0.0
    else:
        rho = (msb - msw) / (msb + (m_bar - 1.0) * msw) if (msb + (m_bar - 1.0) * msw) else 0.0
    rho = max(-1.0, min(1.0, rho))
    deff = 1.0 + (m_bar - 1.0) * max(0.0, rho)
    if deff <= 0:
        deff = 1.0
    return {"deff": deff, "n_eff": n / deff, "rho": rho, "m_bar": m_bar}


def cluster_bootstrap_means(
    groups: dict[str, list[float]],
    *,
    b: int = 10_000,
    seed: int = 20261127,
    stat=None,
) -> list[float]:
    """Resample templates with replacement and keep every instance."""
    names = list(groups)
    if not names:
        return []
    rng = random.Random(seed)
    replicates = []
    for _ in range(b):
        drawn = [names[rng.randrange(len(names))] for _ in names]
        values = [v for name in drawn for v in groups[name]]
        if stat is None:
            replicates.append(_mean(values))
        else:
            replicates.append(float(stat(drawn, groups)))
    return replicates


def percentile_interval(replicates: Sequence[float], *, alpha: float = 0.05) -> tuple[float, float]:
    if not replicates:
        return (0.0, 0.0)
    ordered = sorted(replicates)
    lo = ordered[int(math.floor((alpha / 2.0) * (len(ordered) - 1)))]
    hi = ordered[int(math.ceil((1.0 - alpha / 2.0) * (len(ordered) - 1)))]
    return (lo, hi)


def _jackknife_acceleration(groups: dict[str, list[float]]) -> float:
    names = list(groups)
    g = len(names)
    if g < 3:
        return 0.0
    full_values = [v for values in groups.values() for v in values]
    theta = _mean(full_values)
    leave = []
    for dropped in names:
        kept = [v for name, values in groups.items() if name != dropped for v in values]
        leave.append(_mean(kept))
    mean_leave = _mean(leave)
    numer = sum((mean_leave - value) ** 3 for value in leave)
    denom = sum((mean_leave - value) ** 2 for value in leave)
    if denom <= 0:
        return 0.0
    return numer / (6.0 * (denom ** 1.5))


def bca_interval(
    replicates: Sequence[float],
    acceleration: float,
    *,
    theta: float | None = None,
    alpha: float = 0.05,
) -> tuple[float, float]:
    if not replicates:
        return (0.0, 0.0)
    ordered = sorted(replicates)
    # z0 is the bias of the bootstrap around the original estimate, not around the replicate mean.
    if theta is None:
        theta = _mean(ordered)
    n = len(ordered)
    prop = sum(1 for value in ordered if value < theta) / n
    prop = min(max(prop, 1e-6), 1 - 1e-6)
    z0 = NormalDist().inv_cdf(prop)
    z_lo = NormalDist().inv_cdf(alpha / 2.0)
    z_hi = NormalDist().inv_cdf(1.0 - alpha / 2.0)

    def _adjusted(z_alpha: float) -> float:
        numer = z0 + z_alpha
        denom = 1.0 - acceleration * numer
        if denom == 0:
            return 0.5
        point = NormalDist().cdf(z0 + numer / denom)
        return min(max(point, 0.0), 1.0)

    lo_p = _adjusted(z_lo)
    hi_p = _adjusted(z_hi)
    lo = ordered[min(n - 1, max(0, int(math.floor(lo_p * (n - 1)))))]
    hi = ordered[min(n - 1, max(0, int(math.ceil(hi_p * (n - 1)))))]
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


def _betacf(a: float, b: float, x: float) -> float:
    max_iter = 200
    amp = 1.0
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-10:
            break
    return h


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - log_beta) / a
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x)
    return 1.0 - math.exp(b * math.log(1.0 - x) + a * math.log(x) - log_beta) / b * _betacf(b, a, 1.0 - x)


def student_t_cdf(t: float, df: int) -> float:
    x = df / (df + t * t)
    a = df / 2.0
    b = 0.5
    ib = _regularized_incomplete_beta(a, b, x)
    if t >= 0:
        return 1.0 - 0.5 * ib
    return 0.5 * ib


def student_t_ppf(p: float, df: int) -> float:
    if p <= 0.0 or p >= 1.0:
        raise ScoreError("student t probability must be inside (0, 1)")
    lo, hi = -100.0, 100.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def cluster_t_interval(groups: dict[str, list[float]], *, alpha: float = 0.05) -> tuple[float, float]:
    """Cluster-t interval with a CR2 / Bell–McCaffrey correction on the mean."""
    names = list(groups)
    g = len(names)
    if g < 2:
        point = _mean([v for values in groups.values() for v in values])
        return (point, point)
    n = sum(len(values) for values in groups.values())
    point = _mean([v for values in groups.values() for v in values])
    pieces = []
    for values in groups.values():
        local = _mean(values)
        h = len(values) / n
        # CR2: scale the cluster residual by (1-h)^{-1/2}. Do not also multiply by G/(G-1).
        residual = len(values) * (local - point)
        adjusted = residual / math.sqrt(max(1e-12, 1.0 - h))
        pieces.append(adjusted / n)
    variance = sum(piece * piece for piece in pieces)
    se = math.sqrt(max(0.0, variance))
    critical = student_t_ppf(1.0 - alpha / 2.0, g - 1)
    return (point - critical * se, point + critical * se)


def _wider(left: tuple[float, float], right: tuple[float, float]) -> tuple[float, float]:
    if (left[1] - left[0]) >= (right[1] - right[0]):
        return left
    return right


def accuracy_interval(
    rows: Sequence[dict],
    *,
    value_key: str = "correct",
    b: int = 10_000,
    seed: int = 20261127,
) -> dict[str, Any]:
    """Primary CI. Below about 50 templates the verdict uses the wider of BCa and cluster-t."""
    groups = _grouped(rows, value_key)
    point = _mean([float(row[value_key]) for row in rows]) if rows else 0.0
    replicates = cluster_bootstrap_means(groups, b=b, seed=seed)
    percentile = percentile_interval(replicates)
    g = len(groups)
    effect = design_effect(rows, value_key)
    k = sum(1 for row in rows if row.get(value_key))
    wilson = wilson_interval(k, len(rows))
    result: dict[str, Any] = {
        "point": point,
        "n": len(rows),
        "templates": g,
        "percentile": percentile,
        "wilson_item": wilson,
        "wilson_label": "item-level (ignores clustering)",
        **effect,
    }
    if g < 50:
        bca = bca_interval(replicates, _jackknife_acceleration(groups), theta=point)
        cluster_t = cluster_t_interval(groups)
        verdict = _wider(bca, cluster_t)
        result.update({
            "bca": bca,
            "cluster_t": cluster_t,
            "verdict_interval": verdict,
            "verdict_method": "wider_of_bca_and_cluster_t",
        })
    else:
        result.update({
            "verdict_interval": percentile,
            "verdict_method": "percentile_cluster_bootstrap",
        })
    return result


def paired_cluster_delta(
    rows: Sequence[dict],
    *,
    left_key: str = "left",
    right_key: str = "right",
    b: int = 10_000,
    seed: int = 20261127,
) -> dict[str, Any]:
    """Template-cluster paired bootstrap of the mean per-item difference (left - right)."""
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        groups[str(row["template_id"])].append(float(row[left_key]) - float(row[right_key]))
    replicates = cluster_bootstrap_means(dict(groups), b=b, seed=seed)
    point = _mean([v for values in groups.values() for v in values])
    interval = percentile_interval(replicates)
    g = len(groups)
    payload: dict[str, Any] = {"point": point, "percentile": interval, "templates": g}
    if g < 50:
        bca = bca_interval(replicates, _jackknife_acceleration(dict(groups)), theta=point)
        cluster_t = cluster_t_interval(dict(groups))
        payload["bca"] = bca
        payload["cluster_t"] = cluster_t
        payload["verdict_interval"] = _wider(bca, cluster_t)
        payload["verdict_method"] = "wider_of_bca_and_cluster_t"
    else:
        payload["verdict_interval"] = interval
        payload["verdict_method"] = "percentile_cluster_bootstrap"
    return payload
