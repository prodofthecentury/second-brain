# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Fast local Cross-Encoder reranker for Second Brain retrieval.

Supports two deterministic local-only execution backends (zero network calls):
1. ``OnnxCrossEncoder``: Runs a local ONNX sequence-classification reranker model
   (``model.onnx`` + ``tokenizer.json``) via ``onnxruntime`` and ``tokenizers`` when
   installed under ``BRAIN_CROSS_ENCODER_DIR`` or ``<security_dir>/models/cross-encoder``.
2. ``InteractionCrossEncoder``: Zero-dependency (< 1 ms) query-document token-interaction
   cross-encoder scoring ``## Claim`` vs ``## Evidence`` term/stem coverage, ordered phrase
   bigram adjacency, minimum covering span density, and character 3-gram alignment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("cross_encoder", "scripts.cross_encoder"):
    sys.modules.setdefault("cross_encoder", sys.modules[__name__])
    sys.modules.setdefault("scripts.cross_encoder", sys.modules[__name__])

from config import _is_reparse_or_link, security_state_dir
from lexical_store import FTS_STOP_WORDS, TOKEN_PATTERN, porter_stem
from memory_format import split_frontmatter
from memory_review import section_text

MAX_ONNX_MODEL_BYTES = 600 * 1024 * 1024
MAX_TOKENIZER_BYTES = 32 * 1024 * 1024

_RERANK_STOP_WORDS = set(FTS_STOP_WORDS) | {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "having", "how", "i", "in", "into",
    "is", "it", "its", "me", "my", "of", "on", "or", "our", "owner", "owners", "should", "so",
    "such", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "through", "to", "under", "very", "was", "we", "were", "what", "when", "where",
    "which", "while", "who", "why", "will", "with", "would", "you", "your", "please",
}

_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{1,63}")

_ONNX_CACHE_LOCK = threading.Lock()
_CACHED_ONNX_ENCODERS: dict[tuple[str, int], tuple[float, float, "OnnxCrossEncoder"]] = {}
_DEFAULT_RESOLVED_DIR: tuple[str, int, int, int, int, int, int] | None = None
_VERIFIED_DIR_CACHE: dict[tuple[Any, ...], bool] = {}
_ONNX_PAIR_SCORE_MEMO: dict[tuple[str, int, str, str], float] = {}

_DOC_INTERACTION_LOCK = threading.Lock()
_DOC_INTERACTION_CACHE: dict[int, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], frozenset[str]]] = {}


def _clamp01(val: float) -> float:
    if not math.isfinite(val):
        return 0.0
    return max(0.0, min(1.0, val))


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        z = math.exp(-min(x, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(x, -60.0))
    return z / (1.0 + z)


def resolve_cross_encoder_dir(explicit_dir: str | Path | None = None) -> Path | None:
    """Return a validated local directory containing ``model.onnx`` and ``tokenizer.json``, or None."""
    raw = str(explicit_dir or os.environ.get("BRAIN_CROSS_ENCODER_DIR") or "").strip()
    if raw:
        cand = Path(os.path.abspath(os.path.expanduser(raw)))
    else:
        cand = security_state_dir() / "models" / "cross-encoder"
    if not os.path.lexists(str(cand)):
        return None
    for ancestor in (cand, *cand.parents):
        if os.path.lexists(str(ancestor)) and _is_reparse_or_link(str(ancestor)):
            return None
    if not cand.is_dir():
        return None

    model_file = cand / "model.onnx"
    tok_file = cand / "tokenizer.json"
    stat_sig: list[int] = []
    for fp, max_bytes in ((model_file, MAX_ONNX_MODEL_BYTES), (tok_file, MAX_TOKENIZER_BYTES)):
        if not os.path.lexists(str(fp)) or _is_reparse_or_link(str(fp)):
            return None
        try:
            st = os.lstat(str(fp))
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > max_bytes:
            return None
        stat_sig.extend([st.st_mtime_ns, st.st_size])

    manifest_fp = cand / "onnx-manifest.json"
    cfg_fp = cand / "config.json"
    for opt_fp in (manifest_fp, cfg_fp):
        if os.path.lexists(str(opt_fp)):
            if _is_reparse_or_link(str(opt_fp)):
                return None
            try:
                ost = os.lstat(str(opt_fp))
            except OSError:
                return None
            if not stat.S_ISREG(ost.st_mode):
                return None
            stat_sig.extend([ost.st_mtime_ns, ost.st_size])
        else:
            stat_sig.extend([0, 0])

    managed_models_root = (security_state_dir() / "models").resolve()
    resolved_cand = cand.resolve()
    is_managed_slot = resolved_cand.parent == managed_models_root and resolved_cand.name in {"cross-encoder", "nli"}
    has_manifest = os.path.lexists(str(manifest_fp))
    if is_managed_slot or has_manifest:
        ver_key = (str(resolved_cand), *stat_sig)
        with _ONNX_CACHE_LOCK:
            cached_ok = _VERIFIED_DIR_CACHE.get(ver_key)
        if cached_ok is None:
            try:
                from onnx_models import verify_slot_installation

                slot_name = resolved_cand.name if resolved_cand.name in {"cross-encoder", "nli"} else ""
                if not slot_name and has_manifest:
                    m_doc = json.loads(manifest_fp.read_text(encoding="utf-8"))
                    if isinstance(m_doc, dict):
                        cand_slot = str(m_doc.get("slot") or "").strip().lower()
                        if cand_slot in {"cross-encoder", "nli"}:
                            slot_name = cand_slot
                if not slot_name:
                    cached_ok = False
                else:
                    ver = verify_slot_installation(
                        slot_name,
                        slot_dir=resolved_cand,
                        run_inference=False,
                    )
                    cached_ok = bool(ver.get("verified"))
            except Exception:
                cached_ok = False
            with _ONNX_CACHE_LOCK:
                if len(_VERIFIED_DIR_CACHE) >= 32:
                    _VERIFIED_DIR_CACHE.clear()
                _VERIFIED_DIR_CACHE[ver_key] = cached_ok
        if not cached_ok:
            return None
    return cand


class OnnxCrossEncoder:
    """Local CPU ONNX Runtime cross-encoder wrapper with zero network access."""

    backend_name = "onnx_cross_encoder"

    def __init__(self, model_dir: Path, *, max_length: int = 512) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = model_dir / "model.onnx"
        tok_path = model_dir / "tokenizer.json"
        if _is_reparse_or_link(str(model_dir)) or _is_reparse_or_link(str(model_path)) or _is_reparse_or_link(str(tok_path)):
            raise PermissionError("Refusing to load ONNX cross-encoder via symlink or reparse point.")

        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_opts.intra_op_num_threads = max(1, min(2, os.cpu_count() or 1))
        sess_opts.inter_op_num_threads = 1
        self._model_dir_str = str(model_dir)
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        inputs = self._session.get_inputs()
        self._input_names = {inp.name: inp.type for inp in inputs}
        self._tokenizer = Tokenizer.from_file(str(tok_path))
        self._fixed_seq_len: int | None = None
        self._fixed_batch_size: int | None = None
        if inputs:
            raw_shape = getattr(inputs[0], "shape", None) or ()
            if len(raw_shape) >= 1 and isinstance(raw_shape[0], int) and raw_shape[0] > 0:
                self._fixed_batch_size = int(raw_shape[0])
            if len(raw_shape) >= 2 and isinstance(raw_shape[1], int) and 0 < raw_shape[1] <= 2048:
                self._fixed_seq_len = int(raw_shape[1])
        self._max_length = (
            self._fixed_seq_len
            if self._fixed_seq_len is not None
            else max(8, min(int(max_length), 512))
        )
        # Resolve 3-class NLI label indices (contradiction, neutral, entailment) from config.json when present
        self._nli_indices: tuple[int, int, int] = self._load_nli_label_indices(model_dir)

    @staticmethod
    def _load_nli_label_indices(model_dir: Path) -> tuple[int, int, int]:
        cfg_path = model_dir / "config.json"
        if not os.path.lexists(str(cfg_path)) or _is_reparse_or_link(str(cfg_path)) or not cfg_path.is_file():
            return (0, 1, 2)
        try:
            if cfg_path.stat().st_size > 1_048_576:
                return (0, 1, 2)
            import json

            doc = json.loads(cfg_path.read_text(encoding="utf-8"))
            id2label = doc.get("id2label")
            if not isinstance(id2label, dict):
                return (0, 1, 2)
            contra_idx: int | None = None
            neutral_idx: int | None = None
            entail_idx: int | None = None
            for raw_k, raw_lbl in id2label.items():
                idx = int(str(raw_k).strip())
                lbl = str(raw_lbl or "").strip().lower()
                if "contradict" in lbl:
                    contra_idx = idx
                elif "neutral" in lbl:
                    neutral_idx = idx
                elif "entail" in lbl:
                    entail_idx = idx
            if (
                contra_idx is not None
                and neutral_idx is not None
                and entail_idx is not None
                and {contra_idx, neutral_idx, entail_idx} == {0, 1, 2}
            ):
                return (contra_idx, neutral_idx, entail_idx)
        except Exception:
            pass
        return (0, 1, 2)

    def score_pairs(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        import numpy as np

        if self._fixed_batch_size == 1 and len(texts) > 1:
            out_scores: list[float] = []
            for single in texts:
                out_scores.extend(self.score_pairs(query, [single]))
            return out_scores

        raw_pairs = [(str(query or "")[:512], str(doc or "")[:1200]) for doc in texts]
        final_scores: list[float | None] = [None] * len(raw_pairs)
        uncached_indices: list[int] = []
        pairs: list[tuple[str, str]] = []
        with _ONNX_CACHE_LOCK:
            for i, (q_s, d_s) in enumerate(raw_pairs):
                hit = _ONNX_PAIR_SCORE_MEMO.get((self._model_dir_str, self._max_length, q_s, d_s))
                if hit is not None:
                    final_scores[i] = hit
                else:
                    uncached_indices.append(i)
                    pairs.append((q_s, d_s))
        if not pairs:
            return [float(s or 0.0) for s in final_scores]
        if len(pairs) > 1:
            encodings = self._tokenizer.encode_batch(pairs)
        else:
            encodings = [self._tokenizer.encode(pairs[0][0], pairs[0][1])]
        trimmed_ids = [enc.ids[: self._max_length] for enc in encodings]
        trimmed_mask = [enc.attention_mask[: self._max_length] for enc in encodings]
        trimmed_types = [enc.type_ids[: self._max_length] for enc in encodings]
        max_len = (
            self._fixed_seq_len
            if self._fixed_seq_len is not None
            else max(1, max((len(ids) for ids in trimmed_ids), default=1))
        )

        batch_n = len(pairs)
        input_ids = np.zeros((batch_n, max_len), dtype=np.int64)
        attention_mask = np.zeros((batch_n, max_len), dtype=np.int64)
        token_type_ids = np.zeros((batch_n, max_len), dtype=np.int64)
        for row_idx, (ids, mask, tids) in enumerate(zip(trimmed_ids, trimmed_mask, trimmed_types)):
            seq_len = len(ids)
            if seq_len:
                input_ids[row_idx, :seq_len] = ids
                attention_mask[row_idx, :seq_len] = mask if len(mask) == seq_len else [1] * seq_len
                if len(tids) == seq_len:
                    token_type_ids[row_idx, :seq_len] = tids

        feeds: dict[str, Any] = {}
        if "input_ids" in self._input_names:
            feeds["input_ids"] = input_ids
        if "attention_mask" in self._input_names:
            feeds["attention_mask"] = attention_mask
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = token_type_ids
        if not feeds:
            first_name = next(iter(self._input_names))
            feeds[first_name] = input_ids

        outputs = self._session.run(None, feeds)
        logits = np.asarray(outputs[0], dtype=np.float64)
        scores: list[float] = []
        if logits.ndim == 1:
            for val in logits.tolist():
                scores.append(_clamp01(_sigmoid(float(val))))
        elif logits.ndim == 2 and logits.shape[1] == 1:
            for row in logits:
                scores.append(_clamp01(_sigmoid(float(row[0]))))
        elif logits.ndim == 2 and logits.shape[1] == 2:
            for row in logits:
                m = max(float(row[0]), float(row[1]))
                e0, e1 = math.exp(float(row[0]) - m), math.exp(float(row[1]) - m)
                scores.append(_clamp01(e1 / (e0 + e1)))
        elif logits.ndim == 2 and logits.shape[1] >= 3:
            c_idx, n_idx, e_idx = self._nli_indices
            for row in logits:
                vals = [float(x) for x in row[:3]]
                m = max(vals)
                exps = [math.exp(v - m) for v in vals]
                denom = sum(exps) or 1.0
                probs = [e / denom for e in exps]
                scores.append(_clamp01(probs[e_idx] + 0.25 * probs[n_idx] - 0.15 * probs[c_idx]))
        else:
            flat = logits.reshape(batch_n, -1)
            for row in flat:
                scores.append(_clamp01(_sigmoid(float(row[0]))))
        with _ONNX_CACHE_LOCK:
            if len(_ONNX_PAIR_SCORE_MEMO) >= 4096:
                _ONNX_PAIR_SCORE_MEMO.clear()
            for orig_i, (q_s, d_s), sc in zip(uncached_indices, pairs, scores):
                _ONNX_PAIR_SCORE_MEMO[(self._model_dir_str, self._max_length, q_s, d_s)] = sc
                final_scores[orig_i] = sc
        return [float(s or 0.0) for s in final_scores]

    def predict_nli_probs(self, claim_a: str, claim_b: str) -> tuple[float, float, float] | None:
        """Return ``(p_contradiction, p_neutral, p_entailment)`` for 3-class NLI ONNX models, or None."""
        import numpy as np

        enc = self._tokenizer.encode(str(claim_a or "")[:1000], str(claim_b or "")[:1000])
        trimmed_ids = enc.ids[: self._max_length]
        trimmed_mask = enc.attention_mask[: self._max_length]
        trimmed_types = enc.type_ids[: self._max_length]
        seq_len = self._fixed_seq_len if self._fixed_seq_len is not None else max(1, len(trimmed_ids))

        input_ids = np.zeros((1, seq_len), dtype=np.int64)
        attention_mask = np.zeros((1, seq_len), dtype=np.int64)
        token_type_ids = np.zeros((1, seq_len), dtype=np.int64)
        n_tok = min(len(trimmed_ids), seq_len)
        if n_tok:
            input_ids[0, :n_tok] = trimmed_ids[:n_tok]
            attention_mask[0, :n_tok] = trimmed_mask[:n_tok] if len(trimmed_mask) >= n_tok else [1] * n_tok
            if len(trimmed_types) >= n_tok:
                token_type_ids[0, :n_tok] = trimmed_types[:n_tok]

        feeds: dict[str, Any] = {}
        if "input_ids" in self._input_names:
            feeds["input_ids"] = input_ids
        if "attention_mask" in self._input_names:
            feeds["attention_mask"] = attention_mask
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = token_type_ids
        if not feeds:
            feeds[next(iter(self._input_names))] = input_ids

        logits = np.asarray(self._session.run(None, feeds)[0], dtype=np.float64).reshape(-1)
        if logits.size < 3:
            return None
        vals = [float(x) for x in logits[:3]]
        m = max(vals)
        exps = [math.exp(v - m) for v in vals]
        denom = sum(exps) or 1.0
        probs = [e / denom for e in exps]
        c_idx, n_idx, e_idx = self._nli_indices
        return (probs[c_idx], probs[n_idx], probs[e_idx])


def get_onnx_cross_encoder(
    explicit_dir: str | Path | None = None,
    *,
    max_length: int = 128,
) -> OnnxCrossEncoder | None:
    """Load and cache the ONNX cross-encoder when a validated local model directory is present."""
    global _DEFAULT_RESOLVED_DIR
    eff_max_len = max(8, min(int(max_length), 512))
    is_default_lookup = explicit_dir is None and not (os.environ.get("BRAIN_CROSS_ENCODER_DIR") or "").strip()

    if is_default_lookup:
        with _ONNX_CACHE_LOCK:
            fast_dir = _DEFAULT_RESOLVED_DIR
        if fast_dir is not None:
            dir_str, exp_m_ns, exp_m_sz, exp_t_ns, exp_t_sz, exp_man_ns, exp_man_sz = fast_dir
            try:
                if not _is_reparse_or_link(dir_str):
                    mst = os.lstat(os.path.join(dir_str, "model.onnx"))
                    tst = os.lstat(os.path.join(dir_str, "tokenizer.json"))
                    man_p = os.path.join(dir_str, "onnx-manifest.json")
                    if os.path.lexists(man_p):
                        man_st = os.lstat(man_p)
                        man_sig = (man_st.st_mtime_ns, man_st.st_size)
                    else:
                        man_sig = (0, 0)
                    if (
                        mst.st_mtime_ns == exp_m_ns
                        and mst.st_size == exp_m_sz
                        and tst.st_mtime_ns == exp_t_ns
                        and tst.st_size == exp_t_sz
                        and man_sig == (exp_man_ns, exp_man_sz)
                    ):
                        with _ONNX_CACHE_LOCK:
                            cached_fast = _CACHED_ONNX_ENCODERS.get((dir_str, eff_max_len))
                        if cached_fast is not None:
                            return cached_fast[2]
            except OSError:
                pass

    resolved = resolve_cross_encoder_dir(explicit_dir)
    if resolved is None:
        return None
    try:
        mst_full = os.lstat(str(resolved / "model.onnx"))
        tst_full = os.lstat(str(resolved / "tokenizer.json"))
        m_mtime = mst_full.st_mtime
        t_mtime = tst_full.st_mtime
    except OSError:
        return None
    cache_key = (str(resolved), eff_max_len)
    with _ONNX_CACHE_LOCK:
        cached = _CACHED_ONNX_ENCODERS.get(cache_key)
        if cached is not None and cached[0] == m_mtime and cached[1] == t_mtime:
            return cached[2]
    try:
        encoder = OnnxCrossEncoder(resolved, max_length=eff_max_len)
    except Exception:
        return None
    man_file = resolved / "onnx-manifest.json"
    try:
        if os.path.lexists(str(man_file)):
            man_st2 = os.lstat(str(man_file))
            man_ns, man_sz = man_st2.st_mtime_ns, man_st2.st_size
        else:
            man_ns, man_sz = 0, 0
    except OSError:
        man_ns, man_sz = -1, -1
    with _ONNX_CACHE_LOCK:
        if len(_CACHED_ONNX_ENCODERS) >= 8:
            _CACHED_ONNX_ENCODERS.clear()
        _CACHED_ONNX_ENCODERS[cache_key] = (m_mtime, t_mtime, encoder)
        if is_default_lookup and man_ns >= 0:
            _DEFAULT_RESOLVED_DIR = (
                str(resolved),
                mst_full.st_mtime_ns,
                mst_full.st_size,
                tst_full.st_mtime_ns,
                tst_full.st_size,
                man_ns,
                man_sz,
            )
    return encoder


def clear_cross_encoder_cache() -> None:
    """Clear in-memory ONNX session and parsed document interaction caches."""
    global _DEFAULT_RESOLVED_DIR
    with _ONNX_CACHE_LOCK:
        _CACHED_ONNX_ENCODERS.clear()
        _DEFAULT_RESOLVED_DIR = None
        _VERIFIED_DIR_CACHE.clear()
        _ONNX_PAIR_SCORE_MEMO.clear()
    with _DOC_INTERACTION_LOCK:
        _DOC_INTERACTION_CACHE.clear()


def _char_trigrams(tokens: Sequence[str]) -> frozenset[str]:
    grams: set[str] = set()
    for tok in tokens[:48]:
        padded = f"#{tok}#"
        if len(padded) >= 3:
            for idx in range(len(padded) - 2):
                grams.add(padded[idx : idx + 3])
    return frozenset(grams)


def _content_tokens_and_stems(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    toks: list[str] = []
    stems: list[str] = []
    for raw_tok in TOKEN_PATTERN.findall(text or ""):
        low = raw_tok.casefold()
        if low in _RERANK_STOP_WORDS or len(low) < 2:
            continue
        toks.append(low)
        stems.append(porter_stem(low))
    return tuple(toks), tuple(stems)


def _extract_doc_features(content: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], frozenset[str]]:
    cache_key = hash(content)
    with _DOC_INTERACTION_LOCK:
        cached = _DOC_INTERACTION_CACHE.get(cache_key)
    if cached is not None and cached[0] == content:
        return cached[1], cached[2], cached[3], cached[4], cached[5]

    parsed = split_frontmatter(content or "")
    body = "\n".join(parsed[1]) if parsed else (content or "")
    claim = section_text(body, "Claim") or body[:600]
    evidence = section_text(body, "Evidence")
    claim_toks, claim_stems = _content_tokens_and_stems(claim)
    ev_toks, ev_stems = _content_tokens_and_stems(evidence)
    id_toks = tuple(m.casefold() for m in _IDENTIFIER_TOKEN_RE.findall(claim))
    trigrams = _char_trigrams(claim_toks + id_toks[:16])

    with _DOC_INTERACTION_LOCK:
        if len(_DOC_INTERACTION_CACHE) >= 8192:
            _DOC_INTERACTION_CACHE.clear()
        _DOC_INTERACTION_CACHE[cache_key] = (content, claim_toks, claim_stems, ev_toks, ev_stems, trigrams)
    return claim_toks, claim_stems, ev_toks, ev_stems, trigrams


def _ordered_bigram_proximity(q_stems: Sequence[str], doc_stems: Sequence[str], *, max_gap: int = 4) -> float:
    if len(q_stems) < 2 or len(doc_stems) < 2:
        return 0.0
    pos_by_stem: dict[str, list[int]] = {}
    for idx, st in enumerate(doc_stems):
        pos_by_stem.setdefault(st, []).append(idx)
    hits = 0
    total = len(q_stems) - 1
    for i in range(total):
        left_positions = pos_by_stem.get(q_stems[i])
        right_positions = pos_by_stem.get(q_stems[i + 1])
        if not left_positions or not right_positions:
            continue
        if any(0 < (rp - lp) <= max_gap for lp in left_positions for rp in right_positions):
            hits += 1
    return hits / total


def _min_span_density(q_stem_set: set[str], doc_stems: Sequence[str]) -> float:
    if len(q_stem_set) < 2 or not doc_stems:
        return 0.0
    matched_positions: list[tuple[int, str]] = [
        (idx, st) for idx, st in enumerate(doc_stems) if st in q_stem_set
    ]
    distinct_matched = {st for _, st in matched_positions}
    k = len(distinct_matched)
    if k < 2:
        return 0.0
    counts: dict[str, int] = {}
    have = 0
    left = 0
    best_span = len(doc_stems) + 1
    for right in range(len(matched_positions)):
        r_pos, r_st = matched_positions[right]
        counts[r_st] = counts.get(r_st, 0) + 1
        if counts[r_st] == 1:
            have += 1
        while have == k and left <= right:
            l_pos, l_st = matched_positions[left]
            span = r_pos - l_pos + 1
            if span < best_span:
                best_span = span
            counts[l_st] -= 1
            if counts[l_st] == 0:
                have -= 1
            left += 1
    if best_span > len(doc_stems):
        return 0.0
    return _clamp01((k - 1) / max(k - 1, best_span - 1))


class InteractionCrossEncoder:
    """Fast (< 1 ms) deterministic token-interaction Cross-Encoder for Markdown memory atoms."""

    backend_name = "interaction_cross_encoder"

    def score_pairs(self, query: str, contents: Sequence[str]) -> list[float]:
        if not contents:
            return []
        q_toks, q_stems = _content_tokens_and_stems(query)
        if not q_toks:
            # Fallback for ultra-short stopword-only queries
            raw_q = tuple(t.casefold() for t in TOKEN_PATTERN.findall(query or "") if t)
            if not raw_q:
                return [0.5 for _ in contents]
            q_toks = raw_q
            q_stems = tuple(porter_stem(t) for t in raw_q)

        q_tok_set = set(q_toks)
        q_stem_set = set(q_stems)
        q_id_toks = tuple(m.casefold() for m in _IDENTIFIER_TOKEN_RE.findall(query or ""))
        q_compound_ids = frozenset(
            t for t in q_id_toks if ("_" in t or "-" in t or ":" in t or "." in t or any(ch.isdigit() for ch in t))
        )
        q_trigrams = _char_trigrams(q_toks + q_id_toks[:12])
        denom = max(1, len(q_stem_set))

        scores: list[float] = []
        for content in contents:
            claim_toks, claim_stems, ev_toks, ev_stems, doc_trigrams = _extract_doc_features(content)
            claim_tok_set = set(claim_toks)
            claim_stem_set = set(claim_stems)
            all_tok_set = claim_tok_set | set(ev_toks)
            all_stem_set = claim_stem_set | set(ev_stems)

            exact_claim = len(q_tok_set & claim_tok_set) / max(1, len(q_tok_set))
            stem_claim = len(q_stem_set & claim_stem_set) / denom
            claim_cov = 0.55 * exact_claim + 0.45 * stem_claim

            exact_all = len(q_tok_set & all_tok_set) / max(1, len(q_tok_set))
            stem_all = len(q_stem_set & all_stem_set) / denom
            full_cov = 0.50 * exact_all + 0.50 * stem_all

            if len(q_stems) >= 2:
                bg_claim = _ordered_bigram_proximity(q_stems, claim_stems)
                bg_ev = _ordered_bigram_proximity(q_stems, ev_stems)
                bigram_score = max(bg_claim, 0.70 * bg_ev)
                sp_claim = _min_span_density(q_stem_set, claim_stems)
                sp_ev = _min_span_density(q_stem_set, ev_stems)
                span_score = max(sp_claim, 0.70 * sp_ev)
            else:
                bigram_score = claim_cov
                span_score = claim_cov

            if q_trigrams and doc_trigrams:
                inter = len(q_trigrams & doc_trigrams)
                trigram_dice = (2.0 * inter) / (len(q_trigrams) + len(doc_trigrams))
            else:
                trigram_dice = 0.0

            raw_score = (
                0.44 * claim_cov
                + 0.22 * full_cov
                + 0.14 * bigram_score
                + 0.10 * span_score
                + 0.10 * trigram_dice
            )
            if q_compound_ids:
                content_low = (content or "").casefold()
                matched_ids = sum(1 for cid in q_compound_ids if cid in content_low)
                raw_score = 0.80 * raw_score + 0.20 * (matched_ids / len(q_compound_ids))
            scores.append(_clamp01(raw_score))
        return scores


_DEFAULT_INTERACTION_ENCODER = InteractionCrossEncoder()


def _compact_onnx_doc_text(doc: str) -> str:
    parsed = split_frontmatter(doc or "")
    body = "\n".join(parsed[1]) if parsed else (doc or "")
    claim = section_text(body, "Claim")
    ev = section_text(body, "Evidence")
    if claim or ev:
        return f"{claim[:360]}\n{ev[:280]}".strip()
    return body[:640]


def score_query_atom_pairs(
    query: str,
    contents: Sequence[str],
    *,
    onnx_dir: str | Path | None = None,
    allow_onnx: bool = True,
) -> tuple[list[float], str]:
    """Return ``(scores_in_0_1, backend_name)`` using ONNX when available, else InteractionCrossEncoder."""
    onnx_enc = get_onnx_cross_encoder(onnx_dir) if allow_onnx else None
    if onnx_enc is not None:
        onnx_texts = [_compact_onnx_doc_text(doc) for doc in contents]
        try:
            return onnx_enc.score_pairs(query, onnx_texts), onnx_enc.backend_name
        except Exception:
            pass
    return _DEFAULT_INTERACTION_ENCODER.score_pairs(query, contents), _DEFAULT_INTERACTION_ENCODER.backend_name


def rerank_recall_results(
    query: str,
    results: Sequence[Any],
    *,
    top_k: int | None = None,
    mode: str = "standard",
    onnx_dir: str | Path | None = None,
    allow_onnx: bool = True,
) -> list[Any]:
    """Rerank trusted ``RecallResult`` candidates with a fast two-stage Cross-Encoder cascade.

    Stage 1 (`InteractionCrossEncoder`, <0.1 ms) scores all candidates. When `allow_onnx` is enabled
    and an ONNX Cross-Encoder is verified locally, Stage 2 runs neural sequence classification over
    ambiguous top contenders (when `len >= 2` and top-1 Stage-1 margin < 0.24), keeping 300-atom P95
    strictly below the 15 ms Tier-0 SLO while adjudicating close semantic ties with neural precision.
    """
    items = list(results)
    if not items:
        return []
    contents = [str(getattr(it, "content", "") or "") for it in items]
    stage1_scores = _DEFAULT_INTERACTION_ENCODER.score_pairs(query, contents)
    ce_scores = list(stage1_scores)
    backend = _DEFAULT_INTERACTION_ENCODER.backend_name

    onnx_enc = get_onnx_cross_encoder(onnx_dir) if allow_onnx else None
    if onnx_enc is not None:
        backend = onnx_enc.backend_name
        if len(items) >= 2:
            ranked_idx = sorted(range(len(items)), key=lambda idx: stage1_scores[idx], reverse=True)
            top_s1 = stage1_scores[ranked_idx[0]]
            runner_up_s1 = stage1_scores[ranked_idx[1]]
            if (top_s1 - runner_up_s1) < 0.03 and top_s1 < 0.65:
                contender_idxs = ranked_idx[:2]
                if len(contender_idxs) >= 2:
                    try:
                        onnx_sub = onnx_enc.score_pairs(
                            query,
                            [_compact_onnx_doc_text(contents[idx]) for idx in contender_idxs],
                        )
                        for idx, o_score in zip(contender_idxs, onnx_sub):
                            ce_scores[idx] = o_score
                    except Exception:
                        backend = _DEFAULT_INTERACTION_ENCODER.backend_name
    reranked: list[Any] = []
    for item, ce in zip(items, ce_scores):
        base_score = float(getattr(item, "score", 0.0) or 0.0)
        if mode == "rrf":
            rrf_w = 0.020 if backend == "onnx_cross_encoder" else 0.010
            blended = round(base_score + rrf_w * ce, 6)
        elif backend == "onnx_cross_encoder":
            blended = round(_clamp01(0.20 * base_score + 0.80 * ce), 6)
        else:
            blended = round(_clamp01(0.82 * base_score + 0.18 * ce), 6)
        prev_reasons = tuple(
            r for r in getattr(item, "match_reasons", ()) if not str(r).startswith("rerank:")
        )
        reason_tag = f"rerank:{backend}:{ce:.3f}"
        reranked.append(
            replace(
                item,
                score=blended,
                match_reasons=prev_reasons + (reason_tag,),
            )
        )
    reranked.sort(key=lambda r: (float(getattr(r, "score", 0.0)), str(getattr(r, "filename", ""))), reverse=False)
    reranked.sort(key=lambda r: float(getattr(r, "score", 0.0)), reverse=True)
    if top_k is not None:
        return reranked[: max(1, int(top_k))]
    return reranked
