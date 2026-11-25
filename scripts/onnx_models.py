# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["filelock", "httpx", "onnxruntime", "tokenizers", "numpy"]
# ///
"""SHA-256-Pinned Verified Local ONNX Weights Installer & Manager (`second-brain models`).

Downloads, cryptographically verifies (exact byte length + SHA-256 digest against
immutable HuggingFace Git commit SHAs over DNS-pinned HTTPS), smoke-tests in
CPU-only ONNX Runtime, and atomically installs quantized local neural models into
`<security_state_dir>/models/{cross-encoder,nli}`:
  * `cross-encoder`: `Xenova/ms-marco-MiniLM-L-6-v2` (INT8 quantized, ~23.1 MB)
  * `nli` (`deberta` default): `Xenova/nli-deberta-v3-xsmall` (INT8 quantized, ~87.2 MB)
  * `nli` (`compact` / `mobilebert`): `Xenova/mobilebert-uncased-mnli` (INT8 quantized, ~27.0 MB)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime
import gc
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit
import uuid

from filelock import FileLock
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import _is_immutable_darwin_root_symlink, _is_reparse_or_link, security_state_dir  # noqa: E402
from cross_encoder import (  # noqa: E402
    MAX_ONNX_MODEL_BYTES,
    OnnxCrossEncoder,
    clear_cross_encoder_cache,
)
from host_adapters import (  # noqa: E402
    HostSharingRule,
    PinnedHTTPTransport,
    restrict_security_file_permissions,
    validate_endpoint_network_boundary,
)


class OnnxModelError(RuntimeError):
    """Raised when local ONNX model installation, verification, or removal fails."""


class OnnxModelSecurityError(OnnxModelError):
    """Raised when URL allowlists, SSRF DNS checks, symlinks, or SHA-256 digests fail."""


ONNX_MANIFEST_FILENAME = "onnx-manifest.json"
CORE_SLOTS = ("cross-encoder", "nli")
VALID_SLOTS = ("cross-encoder", "nli", "kokoro-tts")
MAX_REDIRECT_HOPS = 5
_MODELS_MUTATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class PinnedModelArtifact:
    local_filename: str
    remote_rel_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class PinnedModelSpec:
    slot: str
    preset: str
    model_id: str
    repo_id: str
    commit_sha: str
    description: str
    artifacts: tuple[PinnedModelArtifact, ...]

    def remote_url(self, rel_path: str) -> str:
        return f"https://huggingface.co/{self.repo_id}/resolve/{self.commit_sha}/{rel_path.lstrip('/')}"

    @property
    def total_bytes(self) -> int:
        return sum(a.size_bytes for a in self.artifacts)


PINNED_ONNX_CATALOG: dict[tuple[str, str], PinnedModelSpec] = {
    ("cross-encoder", "default"): PinnedModelSpec(
        slot="cross-encoder",
        preset="default",
        model_id="Xenova/ms-marco-MiniLM-L-6-v2@int8",
        repo_id="Xenova/ms-marco-MiniLM-L-6-v2",
        commit_sha="a09144355adeed5f58c8ed011d209bf8ee5a1fec",
        description="Quantized MS-MARCO MiniLM-L-6-v2 Cross-Encoder reranker (~23.9 MB total)",
        artifacts=(
            PinnedModelArtifact(
                local_filename="config.json",
                remote_rel_path="config.json",
                size_bytes=824,
                sha256="d827779a72d27ae68cf878a6fc2e954542663fe21ca515d9f4783fc96be2d37e",
            ),
            PinnedModelArtifact(
                local_filename="tokenizer.json",
                remote_rel_path="tokenizer.json",
                size_bytes=711_396,
                sha256="d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            ),
            PinnedModelArtifact(
                local_filename="model.onnx",
                remote_rel_path="onnx/model_quantized.onnx",
                size_bytes=23_143_499,
                sha256="e9d8ebf845c413e981c175bfe49a3bfa9b3dcce2a3ba54875ee5df5a58639fbe",
            ),
        ),
    ),
    ("nli", "default"): PinnedModelSpec(
        slot="nli",
        preset="default",
        model_id="Xenova/nli-deberta-v3-xsmall@int8",
        repo_id="Xenova/nli-deberta-v3-xsmall",
        commit_sha="2a4f614a701367a02d51389039afc998faeda637",
        description="Quantized DeBERTa-v3-xsmall 3-class NLI contradiction adjudicator (~95.9 MB total)",
        artifacts=(
            PinnedModelArtifact(
                local_filename="config.json",
                remote_rel_path="config.json",
                size_bytes=1_038,
                sha256="ec0bd14cc28640326474399cd61d38ccd52b64900228799d0f81debda8c4bc53",
            ),
            PinnedModelArtifact(
                local_filename="tokenizer.json",
                remote_rel_path="tokenizer.json",
                size_bytes=8_656_551,
                sha256="a86f883318afa11c8c10466f1bf4efaeb6ded28a52cbe57217a8fa0d0a2a87df",
            ),
            PinnedModelArtifact(
                local_filename="model.onnx",
                remote_rel_path="onnx/model_quantized.onnx",
                size_bytes=87_246_587,
                sha256="3fac2500c45c75af42c7711de0d1b93d59577456100208be0dc1f9e8811946b6",
            ),
        ),
    ),
    ("nli", "compact"): PinnedModelSpec(
        slot="nli",
        preset="compact",
        model_id="Xenova/mobilebert-uncased-mnli@int8",
        repo_id="Xenova/mobilebert-uncased-mnli",
        commit_sha="8b0ea66ab7b190bba77418ba03b67d69cfc9a1ee",
        description="Compact quantized MobileBERT-MNLI 3-class NLI adjudicator (~27.7 MB total)",
        artifacts=(
            PinnedModelArtifact(
                local_filename="config.json",
                remote_rel_path="config.json",
                size_bytes=1_092,
                sha256="93b1698798a8ff921b49c40d3afa7584342b94b06fde7851d342134eadaf61c7",
            ),
            PinnedModelArtifact(
                local_filename="tokenizer.json",
                remote_rel_path="tokenizer.json",
                size_bytes=711_396,
                sha256="d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            ),
            PinnedModelArtifact(
                local_filename="model.onnx",
                remote_rel_path="onnx/model_quantized.onnx",
                size_bytes=26_967_165,
                sha256="1dc36bc1f41b1d1da9e8f28f69f8dc6fa316410b2cde057756ddfb7083a48021",
            ),
        ),
    ),
    ("kokoro-tts", "default"): PinnedModelSpec(
        slot="kokoro-tts",
        preset="default",
        model_id="onnx-community/Kokoro-82M-v1.0-ONNX@int8",
        repo_id="onnx-community/Kokoro-82M-v1.0-ONNX",
        commit_sha="1939ad2a8e416c0acfeecc08a694d14ef25f2231",
        description="Quantized Kokoro-82M v1.0 ONNX offline 24kHz CPU TTS (~92.9 MB total)",
        artifacts=(
            PinnedModelArtifact(
                local_filename="config.json",
                remote_rel_path="config.json",
                size_bytes=44,
                sha256="df34b4f930b23447cd4dc410fabfb42eb3f24e803e6c3f97d618fb359380a36f",
            ),
            PinnedModelArtifact(
                local_filename="tokenizer.json",
                remote_rel_path="tokenizer.json",
                size_bytes=3_497,
                sha256="77a02c8e164413299b4b4c403b14f8e0e1c1b727db4d46a09d6327b861060a34",
            ),
            PinnedModelArtifact(
                local_filename="model.onnx",
                remote_rel_path="onnx/model_quantized.onnx",
                size_bytes=92_361_116,
                sha256="fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478",
            ),
            PinnedModelArtifact(
                local_filename="af_heart.bin",
                remote_rel_path="voices/af_heart.bin",
                size_bytes=522_240,
                sha256="d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b",
            ),
        ),
    ),
}


def resolve_pinned_spec(slot: str, preset: str = "default") -> PinnedModelSpec:
    clean_slot = (slot or "").strip().lower()
    if clean_slot == "kokoro":
        clean_slot = "kokoro-tts"
    if clean_slot not in VALID_SLOTS:
        raise OnnxModelError(f"Unsupported ONNX model slot: {slot!r} (expected one of {VALID_SLOTS}).")
    clean_preset = (preset or "default").strip().lower()
    if clean_slot == "nli" and clean_preset in {"deberta", "sota"}:
        clean_preset = "default"
    elif clean_slot == "nli" and clean_preset in {"mobilebert", "small"}:
        clean_preset = "compact"
    elif clean_slot == "cross-encoder" and clean_preset in {"compact", "minilm"}:
        clean_preset = "default"
    spec = PINNED_ONNX_CATALOG.get((clean_slot, clean_preset))
    if spec is None:
        raise OnnxModelError(f"Unknown preset {preset!r} for ONNX slot {clean_slot!r}.")
    return spec


def _models_root(state_dir: str | Path | None = None) -> Path:
    base = Path(state_dir).expanduser().resolve() if state_dir is not None else security_state_dir()
    return base / "models"


def _assert_no_symlink_ancestors(path: Path) -> None:
    for candidate in (path, *path.parents):
        if os.path.lexists(str(candidate)) and _is_reparse_or_link(str(candidate)):
            if not _is_immutable_darwin_root_symlink(candidate):
                raise OnnxModelSecurityError(
                    f"Refusing to access ONNX model directory via symlink or reparse point: {candidate}"
                )


def _is_allowed_hf_hostname(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    return host == "huggingface.co" or host.endswith(".huggingface.co") or host.endswith(".hf.co")


def _validate_hf_download_url(
    url: str,
    *,
    skip_dns: bool = False,
) -> tuple[str, Any]:
    parsed = urlsplit((url or "").strip())
    hostname = (parsed.hostname or "").strip().lower()
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password or parsed.fragment:
        raise OnnxModelSecurityError(f"Rejected non-HTTPS or credential-bearing model URL: {url!r}")
    if parsed.port not in {None, 443}:
        raise OnnxModelSecurityError(f"Rejected non-443 HTTPS model port in URL: {url!r}")
    if not _is_allowed_hf_hostname(hostname):
        raise OnnxModelSecurityError(
            f"Blocked untrusted model download host '{hostname}' (only huggingface.co / *.hf.co allowed)."
        )
    rule = HostSharingRule(
        host_id="onnx_model_hub",
        provider="openai_compatible",
        transport="online",
        endpoint_url=f"https://{hostname}",
        default_model="pinned-onnx",
        api_key_env=None,
        enabled=True,
        owner_consent_recorded=True,
    )
    pinned = validate_endpoint_network_boundary(rule, skip_dns_resolution=skip_dns)
    return url.strip(), pinned


def _download_verified_artifact(
    url: str,
    dest_path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    timeout_s: float = 120.0,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> None:
    if expected_size <= 0 or expected_size > MAX_ONNX_MODEL_BYTES:
        raise OnnxModelSecurityError(f"Invalid pinned artifact size: {expected_size}")
    if len(expected_sha256) != 64:
        raise OnnxModelSecurityError("Invalid pinned SHA-256 digest.")

    current_url = url
    skip_dns = bool(http_transport is not None and skip_dns_check_for_mock_transport)
    max_stream_bytes = min(MAX_ONNX_MODEL_BYTES, expected_size + 65_536)

    for _hop in range(MAX_REDIRECT_HOPS + 1):
        clean_url, pinned_target = _validate_hf_download_url(current_url, skip_dns=skip_dns)
        owned_pinned: PinnedHTTPTransport | None = None
        if http_transport is not None:
            client = httpx.Client(
                transport=http_transport,
                timeout=timeout_s,
                follow_redirects=False,
                trust_env=False,
            )
        elif pinned_target is not None and pinned_target.addresses:
            owned_pinned = PinnedHTTPTransport(pinned_target)
            client = httpx.Client(
                transport=owned_pinned,
                timeout=timeout_s,
                follow_redirects=False,
                trust_env=False,
            )
        else:
            client = httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False)

        try:
            with client:
                with client.stream(
                    "GET",
                    clean_url,
                    headers={"User-Agent": "SecondBrain-ModelInstaller/0.5"},
                ) as resp:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        loc = (resp.headers.get("location") or "").strip()
                        if not loc:
                            raise OnnxModelSecurityError("Redirect response missing Location header.")
                        current_url = urljoin(clean_url, loc)
                        continue
                    resp.raise_for_status()

                    _assert_no_symlink_ancestors(dest_path)
                    hasher = hashlib.sha256()
                    received = 0
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                    fd = os.open(str(dest_path), flags, 0o600)
                    try:
                        with os.fdopen(fd, "wb") as fh:
                            for chunk in resp.iter_bytes(chunk_size=131_072):
                                if not chunk:
                                    continue
                                received += len(chunk)
                                if received > max_stream_bytes:
                                    raise OnnxModelSecurityError(
                                        f"Artifact stream exceeded pinned size ({received} > {expected_size} bytes)."
                                    )
                                hasher.update(chunk)
                                fh.write(chunk)
                            fh.flush()
                            os.fsync(fh.fileno())
                    except Exception:
                        try:
                            if dest_path.exists():
                                dest_path.unlink()
                        except OSError:
                            pass
                        raise

                    actual_sha = hasher.hexdigest()
                    if received != expected_size:
                        try:
                            dest_path.unlink()
                        except OSError:
                            pass
                        raise OnnxModelSecurityError(
                            f"Pinned byte-length mismatch for {dest_path.name}: expected {expected_size}, got {received}."
                        )
                    if not hmac.compare_digest(actual_sha, expected_sha256.lower()):
                        try:
                            dest_path.unlink()
                        except OSError:
                            pass
                        raise OnnxModelSecurityError(
                            f"Pinned SHA-256 mismatch for {dest_path.name}: expected {expected_sha256}, got {actual_sha}."
                        )
                    restrict_security_file_permissions(dest_path)
                    return
        finally:
            if owned_pinned is not None:
                owned_pinned.close()

    raise OnnxModelSecurityError(f"Exceeded maximum HTTPS redirect hops ({MAX_REDIRECT_HOPS}) downloading {url}.")


def _smoke_test_staged_model(slot: str, staged_dir: Path) -> dict[str, Any]:
    if slot == "kokoro-tts":
        from tts_engine import smoke_test_kokoro_slot

        return smoke_test_kokoro_slot(staged_dir)
    enc = OnnxCrossEncoder(staged_dir, max_length=256 if slot == "cross-encoder" else 384)
    try:
        if slot == "cross-encoder":
            scores = enc.score_pairs(
                "Memory API binds strictly to loopback 127.0.0.1 port 8765",
                [
                    "Memory API binds strictly to loopback 127.0.0.1 port 8765.",
                    "Banana bread recipe requires two ripe bananas and flour.",
                ],
            )
            if len(scores) != 2 or not (scores[0] > scores[1]):
                raise OnnxModelError(f"Cross-encoder smoke test failed sanity ranking: {scores}")
            return {"sanity_scores": [round(s, 4) for s in scores]}

        probs = enc.predict_nli_probs(
            "Mutual TLS authentication is strictly required on all internal gRPC services.",
            "Mutual TLS authentication is never used on internal gRPC services.",
        )
        if probs is None:
            raise OnnxModelError("NLI model did not return 3-class probabilities.")
        p_contra, p_neutral, p_entail = probs
        if not (p_contra > p_entail and p_contra > 0.45):
            raise OnnxModelError(
                f"NLI smoke test failed contradiction sanity check: contra={p_contra:.4f}, neutral={p_neutral:.4f}, entail={p_entail:.4f}"
            )
        return {
            "nli_indices": list(enc._nli_indices),
            "sanity_probs": {
                "contradiction": round(p_contra, 4),
                "neutral": round(p_neutral, 4),
                "entailment": round(p_entail, 4),
            },
        }
    finally:
        # Release ONNX Runtime session file handles on Windows before directory rename!
        del enc
        gc.collect()


def _replace_dir_retry(src: Path, dst: Path) -> None:
    for delay in (0.05, 0.1, 0.2, 0.4):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            gc.collect()
            time.sleep(delay)
    os.replace(str(src), str(dst))


def verify_slot_installation(
    slot: str,
    *,
    state_dir: str | Path | None = None,
    slot_dir: str | Path | None = None,
    run_inference: bool = False,
) -> dict[str, Any]:
    clean_slot = (slot or "").strip().lower()
    if clean_slot == "kokoro":
        clean_slot = "kokoro-tts"
    if clean_slot not in VALID_SLOTS:
        raise OnnxModelError(f"Invalid ONNX slot: {slot!r}")
    target_slot_dir = (
        Path(os.path.abspath(os.path.expanduser(str(slot_dir))))
        if slot_dir is not None
        else (_models_root(state_dir) / clean_slot)
    )
    default_backend = (
        "kokoro_unavailable"
        if clean_slot == "kokoro-tts"
        else ("interaction_cross_encoder" if clean_slot == "cross-encoder" else "predicate_fast_nli")
    )
    if not os.path.lexists(str(target_slot_dir)):
        return {
            "slot": clean_slot,
            "installed": False,
            "verified": False,
            "active_backend": default_backend,
            "directory": str(target_slot_dir),
            "issues": [],
        }

    slot_dir = target_slot_dir
    issues: list[str] = []
    try:
        _assert_no_symlink_ancestors(slot_dir)
    except OnnxModelSecurityError as exc:
        return {
            "slot": clean_slot,
            "installed": True,
            "verified": False,
            "directory": str(slot_dir),
            "issues": [str(exc)],
        }

    manifest_path = slot_dir / ONNX_MANIFEST_FILENAME
    manifest: dict[str, Any] = {}
    has_manifest_file = os.path.lexists(str(manifest_path))
    if has_manifest_file:
        if _is_reparse_or_link(str(manifest_path)) or not stat.S_ISREG(os.lstat(str(manifest_path)).st_mode):
            issues.append(f"Unsafe {ONNX_MANIFEST_FILENAME}")
        else:
            try:
                loaded_m = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded_m, dict):
                    manifest = loaded_m
                else:
                    issues.append(f"Invalid {ONNX_MANIFEST_FILENAME}: expected JSON object")
            except Exception as exc:
                issues.append(f"Invalid {ONNX_MANIFEST_FILENAME}: {exc}")

    preset = str(manifest.get("preset") or "default")
    spec = PINNED_ONNX_CATALOG.get((clean_slot, preset))
    file_hashes: dict[str, str] = {}
    file_sizes: dict[str, int] = {}

    req_files = ("model.onnx", "tokenizer.json", "af_heart.bin") if clean_slot == "kokoro-tts" else ("model.onnx", "tokenizer.json")
    for req_name in req_files:
        fp = slot_dir / req_name
        if not os.path.lexists(str(fp)) or _is_reparse_or_link(str(fp)):
            issues.append(f"Missing or symlinked required file: {req_name}")
            continue
        st = os.lstat(str(fp))
        if not stat.S_ISREG(st.st_mode):
            issues.append(f"Non-regular file: {req_name}")
            continue
        if st.st_size <= 0 or st.st_size > MAX_ONNX_MODEL_BYTES:
            issues.append(f"Out-of-bounds file size for {req_name}: {st.st_size}")
            continue
        h = hashlib.sha256(fp.read_bytes()).hexdigest()
        file_hashes[req_name] = h
        file_sizes[req_name] = st.st_size

    cfg_fp = slot_dir / "config.json"
    if os.path.lexists(str(cfg_fp)):
        if _is_reparse_or_link(str(cfg_fp)) or not stat.S_ISREG(os.lstat(str(cfg_fp)).st_mode):
            issues.append("Unsafe config.json")
        else:
            st_cfg = os.lstat(str(cfg_fp))
            file_hashes["config.json"] = hashlib.sha256(cfg_fp.read_bytes()).hexdigest()
            file_sizes["config.json"] = st_cfg.st_size

    pinned_match = False
    if manifest:
        if spec is not None:
            if manifest.get("commit_sha") != spec.commit_sha:
                issues.append(
                    f"Mismatched commit_sha in {ONNX_MANIFEST_FILENAME} for {spec.model_id}: "
                    f"expected {spec.commit_sha}, got {manifest.get('commit_sha')!r}"
                )
            else:
                pinned_match = True
                for art in spec.artifacts:
                    got_sha = file_hashes.get(art.local_filename)
                    if not got_sha or not hmac.compare_digest(got_sha, art.sha256):
                        issues.append(f"SHA-256 mismatch against pinned {spec.model_id} for {art.local_filename}")
                        pinned_match = False
                    elif file_sizes.get(art.local_filename) != art.size_bytes:
                        issues.append(f"Size mismatch against pinned {spec.model_id} for {art.local_filename}")
                        pinned_match = False
        else:
            rec_arts = manifest.get("artifacts")
            if not isinstance(rec_arts, dict) or not rec_arts:
                issues.append(f"Unrecognized preset {preset!r} and missing artifact digests in {ONNX_MANIFEST_FILENAME}")
            else:
                req_manifest_files = (
                    ("model.onnx", "tokenizer.json", "af_heart.bin")
                    if clean_slot == "kokoro-tts"
                    else ("model.onnx", "tokenizer.json")
                )
                for req_name in req_manifest_files:
                    entry = rec_arts.get(req_name)
                    exp_sha = str(entry.get("sha256") or "") if isinstance(entry, dict) else ""
                    got_sha = file_hashes.get(req_name, "")
                    if len(exp_sha) != 64 or not got_sha or not hmac.compare_digest(got_sha, exp_sha.lower()):
                        issues.append(f"SHA-256 mismatch against {ONNX_MANIFEST_FILENAME} for {req_name}")
    elif not has_manifest_file:
        # Check if unmanifested files match any pinned catalog entry for this slot; otherwise reject in managed models dir
        for (cat_slot, _cat_preset), cand_spec in PINNED_ONNX_CATALOG.items():
            if cat_slot != clean_slot:
                continue
            if all(
                file_hashes.get(a.local_filename)
                and hmac.compare_digest(file_hashes[a.local_filename], a.sha256)
                and file_sizes.get(a.local_filename) == a.size_bytes
                for a in cand_spec.artifacts
            ):
                pinned_match = True
                spec = cand_spec
                break
        if not pinned_match:
            issues.append(
                f"Missing {ONNX_MANIFEST_FILENAME} and files do not match any pinned SHA-256 catalog entry for '{clean_slot}'"
            )

    smoke_info: dict[str, Any] | None = None
    if not issues and run_inference:
        try:
            smoke_info = _smoke_test_staged_model(clean_slot, slot_dir)
        except Exception as exc:
            issues.append(f"Runtime inference check failed: {exc}")

    has_required_extras = True if clean_slot != "kokoro-tts" else ("af_heart.bin" in file_hashes)
    verified = len(issues) == 0 and "model.onnx" in file_hashes and "tokenizer.json" in file_hashes and has_required_extras
    if clean_slot == "kokoro-tts":
        active_backend = "kokoro_onnx_tts" if verified else "kokoro_unavailable"
    elif clean_slot == "cross-encoder":
        active_backend = "onnx_cross_encoder" if verified else "interaction_cross_encoder"
    else:
        active_backend = "onnx_nli" if verified else "predicate_fast_nli"
    return {
        "slot": clean_slot,
        "installed": True,
        "verified": verified,
        "pinned_catalog_verified": bool(verified and pinned_match),
        "model_id": manifest.get("model_id") or ("custom-local-onnx" if verified else None),
        "preset": preset if manifest else None,
        "commit_sha": manifest.get("commit_sha"),
        "installed_at": manifest.get("installed_at"),
        "active_backend": active_backend,
        "directory": str(slot_dir),
        "sha256": file_hashes,
        "smoke": smoke_info,
        "issues": issues,
    }


def get_onnx_models_status(
    *,
    state_dir: str | Path | None = None,
    run_inference: bool = False,
) -> dict[str, Any]:
    cross = verify_slot_installation("cross-encoder", state_dir=state_dir, run_inference=run_inference)
    nli = verify_slot_installation("nli", state_dir=state_dir, run_inference=run_inference)
    kokoro = verify_slot_installation("kokoro-tts", state_dir=state_dir, run_inference=run_inference)
    ort_ready = False
    ort_version: str | None = None
    try:
        import onnxruntime as ort
        import tokenizers  # noqa: F401

        ort_ready = True
        ort_version = getattr(ort, "__version__", "installed")
    except Exception:
        ort_ready = False
    return {
        "models_root": str(_models_root(state_dir)),
        "onnx_runtime_ready": ort_ready,
        "onnx_runtime_version": ort_version,
        "all_verified": bool(cross["verified"] and nli["verified"]),
        "voice_verified": bool(kokoro["verified"]),
        "slots": {
            "cross-encoder": cross,
            "nli": nli,
            "kokoro-tts": kokoro,
        },
        "available_presets": {
            "cross-encoder": ["default"],
            "nli": ["default", "compact"],
            "kokoro-tts": ["default"],
        },
    }


def pull_onnx_models(
    target: str = "all",
    *,
    preset: str = "default",
    force: bool = False,
    state_dir: str | Path | None = None,
    timeout_s: float = 180.0,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Download, SHA-256 verify, smoke-test, and atomically activate local ONNX models."""
    clean_target = (target or "all").strip().lower()
    if clean_target == "kokoro":
        clean_target = "kokoro-tts"
    if clean_target == "all":
        slots_to_pull = list(CORE_SLOTS)
    elif clean_target in VALID_SLOTS:
        slots_to_pull = [clean_target]
    else:
        raise OnnxModelError(f"Unsupported target {target!r}; expected 'all', 'cross-encoder', 'nli', or 'kokoro-tts'.")

    root = _models_root(state_dir)
    _assert_no_symlink_ancestors(root)
    root.mkdir(parents=True, exist_ok=True)
    restrict_security_file_permissions(root)
    lock_file = root / ".models.lock"
    _assert_no_symlink_ancestors(lock_file)

    with _MODELS_MUTATION_LOCK, FileLock(str(lock_file), timeout=300):
        _assert_no_symlink_ancestors(root)
        clear_cross_encoder_cache()
        results: dict[str, Any] = {}

        for slot in slots_to_pull:
            slot_preset = preset if (slot == "nli" or preset == "default") else "default"
            spec = resolve_pinned_spec(slot, slot_preset)
            if not force:
                cur = verify_slot_installation(slot, state_dir=state_dir, run_inference=False)
                if cur.get("pinned_catalog_verified") and cur.get("model_id") == spec.model_id:
                    results[slot] = {
                        "status": "already_up_to_date",
                        "model_id": spec.model_id,
                        "directory": cur["directory"],
                    }
                    continue

            staging_dir = root / f".staging-{slot}-{uuid.uuid4().hex[:12]}"
            backup_dir: Path | None = None
            dest_dir = root / slot
            _assert_no_symlink_ancestors(dest_dir)
            try:
                staging_dir.mkdir(parents=True, exist_ok=False)
                restrict_security_file_permissions(staging_dir)

                for art in spec.artifacts:
                    _download_verified_artifact(
                        spec.remote_url(art.remote_rel_path),
                        staging_dir / art.local_filename,
                        expected_size=art.size_bytes,
                        expected_sha256=art.sha256,
                        timeout_s=timeout_s,
                        http_transport=http_transport,
                        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
                    )

                smoke = _smoke_test_staged_model(slot, staging_dir)
                manifest_doc = {
                    "schema_version": 1,
                    "slot": spec.slot,
                    "preset": spec.preset,
                    "model_id": spec.model_id,
                    "repo_id": spec.repo_id,
                    "commit_sha": spec.commit_sha,
                    "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    "artifacts": {a.local_filename: {"size_bytes": a.size_bytes, "sha256": a.sha256} for a in spec.artifacts},
                    "smoke": smoke,
                }
                m_path = staging_dir / ONNX_MANIFEST_FILENAME
                m_path.write_text(json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                restrict_security_file_permissions(m_path)

                clear_cross_encoder_cache()
                gc.collect()
                if dest_dir.exists():
                    backup_dir = root / f".prev-{slot}-{uuid.uuid4().hex[:8]}"
                    _replace_dir_retry(dest_dir, backup_dir)
                try:
                    _replace_dir_retry(staging_dir, dest_dir)
                    restrict_security_file_permissions(dest_dir)
                except Exception:
                    if backup_dir and backup_dir.exists() and not dest_dir.exists():
                        _replace_dir_retry(backup_dir, dest_dir)
                    raise
                if backup_dir and backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)

                results[slot] = {
                    "status": "installed",
                    "model_id": spec.model_id,
                    "preset": spec.preset,
                    "commit_sha": spec.commit_sha,
                    "total_bytes": spec.total_bytes,
                    "directory": str(dest_dir),
                    "smoke": smoke,
                }
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)

        clear_cross_encoder_cache()
        models_st = get_onnx_models_status(state_dir=state_dir, run_inference=False)
        return {
            "status": "ok",
            "all_verified": bool(models_st.get("all_verified")),
            "results": results,
            "slots": models_st.get("slots", {}),
            "models": models_st,
        }


def remove_onnx_models(
    target: str = "all",
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    clean_target = (target or "all").strip().lower()
    if clean_target == "kokoro":
        clean_target = "kokoro-tts"
    if clean_target == "all":
        slots_to_remove = list(VALID_SLOTS)
    elif clean_target in VALID_SLOTS:
        slots_to_remove = [clean_target]
    else:
        raise OnnxModelError(f"Unsupported target {target!r}; expected 'all', 'cross-encoder', 'nli', or 'kokoro-tts'.")

    root = _models_root(state_dir)
    _assert_no_symlink_ancestors(root)
    if not os.path.lexists(str(root)):
        return {
            "status": "removed",
            "removed_slots": [],
            "models": get_onnx_models_status(state_dir=state_dir, run_inference=False),
        }
    lock_file = root / ".models.lock"
    _assert_no_symlink_ancestors(lock_file)

    with _MODELS_MUTATION_LOCK, FileLock(str(lock_file), timeout=60):
        clear_cross_encoder_cache()
        gc.collect()

        removed: list[str] = []
        for slot in slots_to_remove:
            slot_dir = root / slot
            if os.path.lexists(str(slot_dir)):
                _assert_no_symlink_ancestors(slot_dir)
                shutil.rmtree(slot_dir)
                removed.append(slot)
        return {
            "status": "removed",
            "removed_slots": removed,
            "models": get_onnx_models_status(state_dir=state_dir, run_inference=False),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage SHA-256-pinned local ONNX Cross-Encoder & NLI models.")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_st = sub.add_parser("status", help="Show installed ONNX model status and SHA-256 verification")
    p_st.add_argument("--json", action="store_true")

    p_pull = sub.add_parser("pull", help="Download and SHA-256 verify local quantized ONNX weights")
    p_pull.add_argument("target", nargs="?", default="all", choices=("all", "cross-encoder", "nli", "kokoro-tts", "kokoro"))
    p_pull.add_argument("--preset", default="default", choices=("default", "compact", "deberta", "mobilebert"))
    p_pull.add_argument("--force", action="store_true", help="Re-download even if already verified")
    p_pull.add_argument("--json", action="store_true")

    p_ver = sub.add_parser("verify", help="Verify SHA-256 digests and run live ONNX CPU smoke inference")
    p_ver.add_argument("--json", action="store_true")

    p_rm = sub.add_parser("remove", help="Remove local ONNX weights (reverts to <1ms deterministic engines)")
    p_rm.add_argument("target", nargs="?", default="all", choices=("all", "cross-encoder", "nli", "kokoro-tts", "kokoro"))
    p_rm.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    cmd = args.cmd or "status"

    try:
        if cmd == "status":
            st = get_onnx_models_status(run_inference=False)
            if getattr(args, "json", False):
                print(json.dumps(st, indent=2, sort_keys=True))
            else:
                print(f"ONNX Models Root: {st['models_root']}")
                print(f"ONNX Runtime:     {'Ready (' + str(st['onnx_runtime_version']) + ')' if st['onnx_runtime_ready'] else 'Missing'}")
                for slot_name, info in st["slots"].items():
                    badge = "VERIFIED" if info["verified"] else ("INVALID" if info["installed"] else "FALLBACK")
                    print(f"  [{badge}] {slot_name:14s} -> backend={info['active_backend']} model={info.get('model_id') or 'none'}")
                    for iss in info.get("issues") or []:
                        print(f"      issue: {iss}")
            return 0

        if cmd == "verify":
            st = get_onnx_models_status(run_inference=True)
            if getattr(args, "json", False):
                print(json.dumps(st, indent=2, sort_keys=True))
            else:
                for slot_name, info in st["slots"].items():
                    print(f"[{'PASS' if info['verified'] else 'FAIL'}] {slot_name}: backend={info['active_backend']} model={info.get('model_id') or 'none'}")
                    for iss in info.get("issues") or []:
                        print(f"  - {iss}")
            return 0 if st["all_verified"] else 1

        if cmd == "pull":
            res = pull_onnx_models(args.target, preset=args.preset, force=args.force)
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2, sort_keys=True))
            else:
                for slot_name, rinfo in res["results"].items():
                    print(f"[{rinfo['status'].upper()}] {slot_name}: {rinfo['model_id']} -> {rinfo['directory']}")
            return 0

        if cmd == "remove":
            res = remove_onnx_models(args.target)
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2, sort_keys=True))
            else:
                print(f"Removed ONNX model slots: {', '.join(res['removed_slots']) or 'none'}")
            return 0
    except OnnxModelError as exc:
        print(f"ONNX model error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
