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

from collections.abc import Mapping, Sequence
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from typing import Any

if __name__ in ("cross_encoder", "scripts.cross_encoder"):
    sys.modules.setdefault("cross_encoder", sys.modules[__name__])
    sys.modules.setdefault("scripts.cross_encoder", sys.modules[__name__])

from scripts.config import _is_immutable_darwin_root_symlink, _is_reparse_or_link, security_state_dir
from scripts.lexical_store import FTS_STOP_WORDS, TOKEN_PATTERN, porter_stem
from scripts.memory_format import split_frontmatter
from scripts.memory_review import section_text

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
_DEFAULT_RESOLVED_DIR: tuple[str, int, int, int, int, int, int, int, int] | None = None
_DEFAULT_RESOLVED_CHECKED_MONO: float = 0.0
_ONNX_STAT_TTL_SECONDS: float = 0.0
_VERIFIED_DIR_CACHE: dict[tuple[Any, ...], bool] = {}
_ONNX_PAIR_SCORE_MEMO: dict[tuple[str, int, tuple[int, int, int, int, int, int], str, str], float] = {}


def _stat_is_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or (
        os.name == "nt" and bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _lstat_file_sig(fp: str | Path, *, allow_missing: bool = False) -> tuple[int, int] | None:
    """Return regular non-reparse ``(st_mtime_ns, st_size)`` for ``fp``, or None on unsafe/missing."""
    p_str = str(fp)
    try:
        st = os.lstat(p_str)
    except FileNotFoundError:
        return (0, 0) if allow_missing else None
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or _stat_is_reparse(st):
        return None
    return (st.st_mtime_ns, st.st_size)


def _ancestors_reparse_free(cand: Path) -> bool:
    try:
        dir_st = os.lstat(str(cand))
    except OSError:
        return False
    if not stat.S_ISDIR(dir_st.st_mode) or _stat_is_reparse(dir_st):
        return False
    for ancestor in cand.parents:
        try:
            ast = os.lstat(str(ancestor))
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if _stat_is_reparse(ast) and not _is_immutable_darwin_root_symlink(ancestor):
            return False
    return True

_DOC_INTERACTION_LOCK = threading.Lock()
_DOC_INTERACTION_CACHE: dict[
    int,
    tuple[
        str,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        frozenset[str],
        frozenset[str],
        frozenset[str],
        frozenset[str],
        frozenset[str],
        str,
    ],
] = {}


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
            if _is_immutable_darwin_root_symlink(ancestor):
                continue
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
                from scripts.onnx_models import verify_slot_installation

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
        sess_opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        sess_opts.inter_op_num_threads = 1
        self._model_dir_str = str(model_dir)
        m_sig = _lstat_file_sig(model_path) or (0, 0)
        t_sig = _lstat_file_sig(tok_path) or (0, 0)
        c_sig = _lstat_file_sig(model_dir / "config.json", allow_missing=True) or (-1, -1)
        self._model_stat_sig: tuple[int, int, int, int, int, int] = (*m_sig, *t_sig, *c_sig)
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
                hit = _ONNX_PAIR_SCORE_MEMO.get(
                    (self._model_dir_str, self._max_length, self._model_stat_sig, q_s, d_s)
                )
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
                _ONNX_PAIR_SCORE_MEMO[
                    (self._model_dir_str, self._max_length, self._model_stat_sig, q_s, d_s)
                ] = sc
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
    global _DEFAULT_RESOLVED_DIR, _DEFAULT_RESOLVED_CHECKED_MONO
    eff_max_len = max(8, min(int(max_length), 512))
    is_default_lookup = explicit_dir is None and not (os.environ.get("BRAIN_CROSS_ENCODER_DIR") or "").strip()

    if is_default_lookup:
        expected_default_dir = os.path.normcase(
            os.path.abspath(str(security_state_dir() / "models" / "cross-encoder"))
        )
        with _ONNX_CACHE_LOCK:
            fast_dir = _DEFAULT_RESOLVED_DIR
        if fast_dir is not None and os.path.normcase(fast_dir[0]) == expected_default_dir:
            (
                dir_str,
                exp_m_ns,
                exp_m_sz,
                exp_t_ns,
                exp_t_sz,
                exp_man_ns,
                exp_man_sz,
                exp_cfg_ns,
                exp_cfg_sz,
            ) = fast_dir
            dir_path = Path(dir_str)
            if _ancestors_reparse_free(dir_path):
                m_sig = _lstat_file_sig(dir_path / "model.onnx")
                t_sig = _lstat_file_sig(dir_path / "tokenizer.json")
                man_sig = _lstat_file_sig(dir_path / "onnx-manifest.json", allow_missing=True)
                cfg_sig = _lstat_file_sig(dir_path / "config.json", allow_missing=True)
                if (
                    m_sig == (exp_m_ns, exp_m_sz)
                    and t_sig == (exp_t_ns, exp_t_sz)
                    and man_sig == (exp_man_ns, exp_man_sz)
                    and cfg_sig == (exp_cfg_ns, exp_cfg_sz)
                ):
                    with _ONNX_CACHE_LOCK:
                        cached_fast = _CACHED_ONNX_ENCODERS.get((dir_str, eff_max_len))
                        if cached_fast is not None:
                            _DEFAULT_RESOLVED_CHECKED_MONO = time.monotonic()
                            return cached_fast[2]

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
    man_sig_full = _lstat_file_sig(resolved / "onnx-manifest.json", allow_missing=True)
    cfg_sig_full = _lstat_file_sig(resolved / "config.json", allow_missing=True)
    with _ONNX_CACHE_LOCK:
        if len(_CACHED_ONNX_ENCODERS) >= 8:
            _CACHED_ONNX_ENCODERS.clear()
        _CACHED_ONNX_ENCODERS[cache_key] = (m_mtime, t_mtime, encoder)
        if is_default_lookup and man_sig_full is not None and cfg_sig_full is not None:
            _DEFAULT_RESOLVED_DIR = (
                str(resolved),
                mst_full.st_mtime_ns,
                mst_full.st_size,
                tst_full.st_mtime_ns,
                tst_full.st_size,
                man_sig_full[0],
                man_sig_full[1],
                cfg_sig_full[0],
                cfg_sig_full[1],
            )
            _DEFAULT_RESOLVED_CHECKED_MONO = time.monotonic()
    return encoder


def clear_cross_encoder_cache() -> None:
    """Clear in-memory ONNX session and parsed document interaction caches."""
    global _DEFAULT_RESOLVED_DIR, _DEFAULT_RESOLVED_CHECKED_MONO
    with _ONNX_CACHE_LOCK:
        _CACHED_ONNX_ENCODERS.clear()
        _DEFAULT_RESOLVED_DIR = None
        _DEFAULT_RESOLVED_CHECKED_MONO = 0.0
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


def _stem_positions_map(stems: Sequence[str]) -> dict[str, tuple[int, ...]]:
    """Build an immutable mapping from stem to sorted token positions for O(1) bigram proximity lookups."""
    pos_lists: dict[str, list[int]] = {}
    for idx, st in enumerate(stems):
        pos_lists.setdefault(st, []).append(idx)
    return {st: tuple(idxs) for st, idxs in pos_lists.items()}


def _extract_doc_features(
    content: str,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    frozenset[str],
    frozenset[str],
    frozenset[str],
    frozenset[str],
    frozenset[str],
    str,
    frozenset[str],
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
    str,
]:
    cache_key = hash(content)
    with _DOC_INTERACTION_LOCK:
        cached = _DOC_INTERACTION_CACHE.get(cache_key)
    if cached is not None and cached[0] == content:
        return cached[1:]

    parsed = split_frontmatter(content or "")
    body = "\n".join(parsed[1]) if parsed else (content or "")
    raw_claim = section_text(body, "Claim")
    raw_ev = section_text(body, "Evidence")
    compact_text = f"{raw_claim[:360]}\n{raw_ev[:280]}".strip() if (raw_claim or raw_ev) else body[:640]
    claim = raw_claim or body[:600]
    evidence = raw_ev
    claim_toks, claim_stems = _content_tokens_and_stems(claim)
    ev_toks, ev_stems = _content_tokens_and_stems(evidence)
    id_toks = tuple(m.casefold() for m in _IDENTIFIER_TOKEN_RE.findall(claim))
    trigrams = _char_trigrams(claim_toks + id_toks[:16])
    claim_tok_set = frozenset(claim_toks)
    claim_stem_set = frozenset(claim_stems)
    ev_stem_set = frozenset(ev_stems)
    all_tok_set = claim_tok_set | frozenset(ev_toks)
    all_stem_set = claim_stem_set | ev_stem_set
    claim_pos_by_stem = _stem_positions_map(claim_stems)
    ev_pos_by_stem = _stem_positions_map(ev_stems)
    content_casefold = (content or "").casefold()

    features = (
        claim_toks,
        claim_stems,
        ev_toks,
        ev_stems,
        trigrams,
        claim_tok_set,
        claim_stem_set,
        all_tok_set,
        all_stem_set,
        compact_text,
        ev_stem_set,
        claim_pos_by_stem,
        ev_pos_by_stem,
        content_casefold,
    )
    with _DOC_INTERACTION_LOCK:
        if len(_DOC_INTERACTION_CACHE) >= 8192:
            _DOC_INTERACTION_CACHE.clear()
        _DOC_INTERACTION_CACHE[cache_key] = (content, *features)
    return features


def _ordered_bigram_proximity(
    q_stems: Sequence[str],
    doc_stems: Sequence[str],
    *,
    max_gap: int = 4,
    q_bigrams: tuple[tuple[str, str], ...] | None = None,
    pos_by_stem: Mapping[str, Sequence[int]] | None = None,
) -> float:
    if len(q_stems) < 2 or len(doc_stems) < 2:
        return 0.0
    pairs = q_bigrams if q_bigrams is not None else tuple((q_stems[i], q_stems[i + 1]) for i in range(len(q_stems) - 1))
    if pos_by_stem is None:
        pos_lists: dict[str, list[int]] = {}
        for idx, st in enumerate(doc_stems):
            pos_lists.setdefault(st, []).append(idx)
        pos_lookup: Mapping[str, Sequence[int]] = pos_lists
    else:
        pos_lookup = pos_by_stem
    hits = 0
    total = len(pairs)
    for left_s, right_s in pairs:
        left_positions = pos_lookup.get(left_s)
        right_positions = pos_lookup.get(right_s)
        if not left_positions or not right_positions:
            continue
        if any(0 < (rp - lp) <= max_gap for lp in left_positions for rp in right_positions):
            hits += 1
    return hits / total


def _min_span_density(
    q_stem_set: set[str],
    doc_stems: Sequence[str],
    *,
    pos_by_stem: Mapping[str, Sequence[int]] | None = None,
) -> float:
    if len(q_stem_set) < 2 or not doc_stems:
        return 0.0
    if pos_by_stem is not None:
        matched_lists = [pos_by_stem[st] for st in q_stem_set if st in pos_by_stem and pos_by_stem[st]]
        k = len(matched_lists)
        if k < 2:
            return 0.0
        if all(len(p) == 1 for p in matched_lists):
            lo = min(p[0] for p in matched_lists)
            hi = max(p[0] for p in matched_lists)
            best_span = hi - lo + 1
            return _clamp01((k - 1) / max(k - 1, best_span - 1))
        if k == 2:
            a, b = matched_lists[0], matched_lists[1]
            ia = ib = 0
            na, nb = len(a), len(b)
            best_diff = len(doc_stems) + 1
            while ia < na and ib < nb:
                va, vb = a[ia], b[ib]
                diff = abs(va - vb)
                if diff < best_diff:
                    best_diff = diff
                    if best_diff <= 1:
                        break
                if va < vb:
                    ia += 1
                else:
                    ib += 1
            best_span = best_diff + 1
            return _clamp01(1.0 / max(1, best_span - 1))
    matched_positions: list[tuple[int, str]] = [
        (idx, st) for idx, st in enumerate(doc_stems) if st in q_stem_set
    ]
    if len(matched_positions) < 2:
        return 0.0
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

    backend_name = "cross_encoder"

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
        q_bigrams = tuple((q_stems[i], q_stems[i + 1]) for i in range(len(q_stems) - 1)) if len(q_stems) >= 2 else ()
        q_id_toks = tuple(m.casefold() for m in _IDENTIFIER_TOKEN_RE.findall(query or ""))
        q_compound_ids = frozenset(
            t for t in q_id_toks if ("_" in t or "-" in t or ":" in t or "." in t or any(ch.isdigit() for ch in t))
        )
        q_trigrams = _char_trigrams(q_toks + q_id_toks[:12])
        denom = max(1, len(q_stem_set))

        scores: list[float] = []
        with _DOC_INTERACTION_LOCK:
            batch_feats = [
                c[1:] if (c := _DOC_INTERACTION_CACHE.get(hash(doc))) is not None and c[0] == doc else None
                for doc in contents
            ]
        for content, pre_feat in zip(contents, batch_feats):
            (
                claim_toks,
                claim_stems,
                ev_toks,
                ev_stems,
                doc_trigrams,
                claim_tok_set,
                claim_stem_set,
                all_tok_set,
                all_stem_set,
                _compact_text,
                ev_stem_set,
                claim_pos_by_stem,
                ev_pos_by_stem,
                content_low,
            ) = pre_feat if pre_feat is not None else _extract_doc_features(content)

            exact_claim = len(q_tok_set & claim_tok_set) / max(1, len(q_tok_set))
            claim_stem_overlap_n = len(q_stem_set & claim_stem_set)
            stem_claim = claim_stem_overlap_n / denom
            claim_cov = 0.55 * exact_claim + 0.45 * stem_claim

            exact_all = len(q_tok_set & all_tok_set) / max(1, len(q_tok_set))
            stem_all = len(q_stem_set & all_stem_set) / denom
            full_cov = 0.50 * exact_all + 0.50 * stem_all

            if len(q_stems) >= 2:
                if q_stem_set.isdisjoint(all_stem_set):
                    bigram_score = 0.0
                    span_score = 0.0
                else:
                    claim_has_overlap = claim_stem_overlap_n > 0
                    bg_claim = (
                        _ordered_bigram_proximity(
                            q_stems,
                            claim_stems,
                            q_bigrams=q_bigrams,
                            pos_by_stem=claim_pos_by_stem,
                        )
                        if claim_has_overlap
                        else 0.0
                    )
                    sp_claim = (
                        _min_span_density(
                            q_stem_set,
                            claim_stems,
                            pos_by_stem=claim_pos_by_stem,
                        )
                        if claim_stem_overlap_n >= 2
                        else 0.0
                    )
                    if claim_stems == ev_stems:
                        bigram_score = bg_claim
                        span_score = sp_claim
                    else:
                        ev_stem_overlap_n = len(q_stem_set & ev_stem_set) if ev_stem_set else 0
                        bg_ev = (
                            _ordered_bigram_proximity(
                                q_stems,
                                ev_stems,
                                q_bigrams=q_bigrams,
                                pos_by_stem=ev_pos_by_stem,
                            )
                            if (ev_stem_overlap_n > 0 and bg_claim < 0.70)
                            else 0.0
                        )
                        bigram_score = max(bg_claim, 0.70 * bg_ev)
                        sp_ev = (
                            _min_span_density(
                                q_stem_set,
                                ev_stems,
                                pos_by_stem=ev_pos_by_stem,
                            )
                            if (ev_stem_overlap_n >= 2 and sp_claim < 0.70)
                            else 0.0
                        )
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
                matched_ids = sum(1 for cid in q_compound_ids if cid in content_low)
                raw_score = 0.80 * raw_score + 0.20 * (matched_ids / len(q_compound_ids))
            scores.append(0.0 if raw_score <= 0.0 else (1.0 if raw_score >= 1.0 else raw_score))
        return scores


_DEFAULT_INTERACTION_ENCODER = InteractionCrossEncoder()


def _compact_onnx_doc_text(doc: str) -> str:
    return _extract_doc_features(doc or "")[9]


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
    """Rerank trusted ``RecallResult`` candidates with a truthful two-stage Cross-Encoder cascade.

    Stage 1 (`InteractionCrossEncoder`, <0.1 ms) scores all candidates to shortlist up to 12 top
    contenders. When `allow_onnx` is enabled and a local ONNX Cross-Encoder is verified, Stage 2
    runs neural sequence classification (`onnx_enc.score_pairs`) over the shortlisted candidates on
    every query. Only candidates genuinely scored by ONNX receive ``rerank:onnx_cross_encoder`` and
    0.20/0.80 neural blending; any candidate not scored by ONNX receives ``rerank:cross_encoder``
    and 0.82/0.18 heuristic blending.
    """
    items = list(results)
    if not items:
        return []
    contents = [str(getattr(it, "content", "") or "") for it in items]
    stage1_scores = _DEFAULT_INTERACTION_ENCODER.score_pairs(query, contents)
    ce_scores = list(stage1_scores)
    onnx_scored_idxs: set[int] = set()

    onnx_enc = get_onnx_cross_encoder(onnx_dir) if allow_onnx else None
    if onnx_enc is not None:
        shortlist_cap = min(len(items), max((int(top_k) if top_k is not None else 5) + 3, 8), 12)
        ranked_idx = sorted(range(len(items)), key=lambda idx: stage1_scores[idx], reverse=True)
        contender_idxs = ranked_idx[:shortlist_cap]
        if contender_idxs:
            try:
                onnx_sub = onnx_enc.score_pairs(
                    query,
                    [_compact_onnx_doc_text(contents[idx]) for idx in contender_idxs],
                )
                if len(onnx_sub) == len(contender_idxs):
                    for idx, o_score in zip(contender_idxs, onnx_sub):
                        ce_scores[idx] = o_score
                        onnx_scored_idxs.add(idx)
            except Exception:
                onnx_scored_idxs.clear()

    has_any_onnx = bool(onnx_scored_idxs)
    scored_rows: list[tuple[int, float, str, int, str, float]] = []
    for idx, (item, ce) in enumerate(zip(items, ce_scores)):
        used_onnx = idx in onnx_scored_idxs
        item_backend = (
            str(getattr(onnx_enc, "backend_name", None) or "onnx_cross_encoder")
            if (used_onnx and onnx_enc is not None)
            else _DEFAULT_INTERACTION_ENCODER.backend_name
        )
        base_score = float(getattr(item, "score", 0.0) or 0.0)
        if mode == "rrf":
            rrf_w = 0.020 if used_onnx else 0.010
            blended = round(base_score + rrf_w * ce, 6)
        elif mode == "lexical" and not used_onnx:
            blended = round(_clamp01(0.35 * base_score + 0.65 * ce), 6)
        elif used_onnx:
            blended = round(_clamp01(0.20 * base_score + 0.80 * ce), 6)
        else:
            blended = round(_clamp01(0.82 * base_score + 0.18 * ce), 6)
        tier = 1 if (used_onnx or not has_any_onnx) else 0
        scored_rows.append((tier, blended, str(getattr(item, "filename", "")), idx, item_backend, ce))

    scored_rows.sort(key=lambda row: (-row[0], -row[1], row[2], row[3]))
    if top_k is not None:
        scored_rows = scored_rows[: max(1, int(top_k))]
    reranked: list[Any] = []
    for _tier, blended, _fn, idx, item_backend, ce in scored_rows:
        item = items[idx]
        prev_reasons = tuple(
            r for r in getattr(item, "match_reasons", ()) if not str(r).startswith("rerank:")
        )
        reranked.append(
            replace(
                item,
                score=blended,
                match_reasons=prev_reasons + (f"rerank:{item_backend}:{ce:.3f}",),
            )
        )
    return reranked
