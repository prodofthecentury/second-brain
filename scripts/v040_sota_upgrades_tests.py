# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Suite 21 — v0.4.0 SOTA Upgrades End-to-End Verification & Security Suite.

Covers all 4 SOTA tracks end-to-end:
  1. Retrieval & Model Upgrade:
     - Hardware-profile auto-wired `qwen2.5-coder:7b` generation model + env/policy overrides
     - O(|changed|) incremental single-atom Chroma vector upserts above 64 atoms (> 64 stays current)
     - Fast local Cross-Encoder reranker (`InteractionCrossEncoder` + live `OnnxCrossEncoder` session)
     - Persistent multi-turn chat history (`chat_threads.sqlite3`), coreference follow-up expansion,
       turn pinning to notes, secret scrubbing, and strict owner-only API isolation (`403` for agents)
  2. Real NLI & Auto-Linking Graph:
     - Zero false-positive polarity/ordinal alerts on Markdown `.**`, contrastive clauses, and list numbers
     - Fast local NLI adjudication (`nli_mode="fast" | "hybrid"`) catching true contradictions
     - Automatic `[[wikilink]]`, duplicate, and contradiction candidate linking during `verify_note_excerpt`
       and vault-wide `auto_link_vault_atoms` (`/v1/graph/auto-link`)
     - Antigravity 17/17 MCP plugin parity
  3. SOTA Clipper & Document Extraction:
     - Browser Clipper Readability-style DOM-to-Markdown + server-side `trafilatura` HTML-to-Markdown
     - 1-click pairing codes (`SBC-XXXX-XXXX-XXXX`) with single-use consumption, CORS preflight, and rate-limiting
     - Coordinate-aware 2-column PDF de-interleaving, Markdown pipe-table reconstruction, and local OCR PDF
       extraction preserving exact `[page N]` UTF-8 byte-span anchors
  4. Production Cloud Sync & 3-Surface UI Parity:
     - Pure-Python AWS S3 / Cloudflare R2 / Backblaze B2 SigV4 (`AWS4-HMAC-SHA256`) over mock S3 verifier + `sync_gc`
     - Cross-platform OS Keychain / Windows DPAPI master-key sealing (`master_key_b64=None` on disk)
     - Incremental Chroma vector sync on `sync_pull` + `AutoSyncScheduler` background loop & conflict pause
     - Interactive 3-way conflict resolver (`keep_local`, `accept_remote`, `keep_both`, `manual_merge`) with
       common-ancestor base text, CAS protection, `.history/` backup, and canonical atom trust demotion
     - 3-surface UI parity & strict zero-`innerHTML` static XSS enforcement
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, unquote, urlparse

import httpx

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _seed_verified_atom(
    brain_dir: Path,
    *,
    filename: str,
    claim: str,
    evidence: str | None = None,
    project: str = "project:alpha",
    memory_type: str = "semantic",
    approve: bool = True,
) -> Path:
    from atom_mutator import apply_review_transition, content_sha256
    from evidence import find_evidence_anchor

    raw_dir = brain_dir / "0-raw"
    atoms_dir = brain_dir / "2-atoms"
    raw_dir.mkdir(parents=True, exist_ok=True)
    atoms_dir.mkdir(parents=True, exist_ok=True)
    (brain_dir / "0-raw" / "archive").mkdir(parents=True, exist_ok=True)
    (brain_dir / ".sidecar").mkdir(parents=True, exist_ok=True)

    ev_txt = evidence or claim
    src_name = f"src-{Path(filename).stem}.md"
    src_path = raw_dir / src_name
    src_path.write_text(f"# Source\n\n{ev_txt}\n", encoding="utf-8")

    stem = Path(filename).stem
    draft = (
        f"---\n"
        f"id: {stem}\n"
        f"type: atom\n"
        f"memory_type: {memory_type}\n"
        f"epistemic_label: claim\n"
        f"sources:\n"
        f"  - 0-raw/{src_name}\n"
        f"contexts:\n"
        f"  - {project}\n"
        f"claim_state: candidate\n"
        f"review_status: pending\n"
        f"citation_integrity: anchored\n"
        f"---\n\n"
        f"## Claim\n{claim}\n\n"
        f"## Evidence\n{ev_txt}\n"
    )
    anchor = find_evidence_anchor(draft, str(brain_dir))
    parts = draft.split("---", 2)
    fm = parts[1] + "".join(f"{k}: {v}\n" for k, v in anchor.items())
    atom_path = atoms_dir / filename
    atom_path.write_text(f"---{fm}---{parts[2]}", encoding="utf-8")
    if approve:
        apply_review_transition(
            atom_path=str(atom_path),
            action="approve",
            expected_sha256=content_sha256(atom_path.read_bytes()),
            actor="test_owner",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
    return atom_path


def _deterministic_vec(text: str, dim: int = 32) -> list[float]:
    import re

    vals = [0.0] * dim
    tokens = re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
    for tok in tokens:
        weight = 6.0 if ("_" in tok or any(ch.isdigit() for ch in tok)) else 1.0
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = h[0] % dim
        sign = 1.0 if (h[1] & 1) == 0 else -1.0
        vals[idx] += sign * weight
    if not any(vals):
        vals[0] = 1.0
    norm = sum(v * v for v in vals) ** 0.5 or 1.0
    return [round(v / norm, 6) for v in vals]


def _build_minimal_linear_onnx_model(seq_len: int = 8) -> bytes:
    """Construct a valid minimal ONNX protobuf model (input_ids -> Cast -> ReduceSum -> [B, 1] logits) without external downloads."""
    def _varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        return bytes(out)

    def _field_varint(fnum: int, val: int) -> bytes:
        return _varint((fnum << 3) | 0) + _varint(val)

    def _field_bytes(fnum: int, payload: bytes | str) -> bytes:
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        return _varint((fnum << 3) | 2) + _varint(len(raw)) + raw

    def _tensor_type_proto(elem_type: int, dims: list[int | str]) -> bytes:
        shape_dims = b"".join(
            _field_bytes(1, _field_bytes(2, d) if isinstance(d, str) else _field_varint(1, d))
            for d in dims
        )
        tensor_type = _field_varint(1, elem_type) + _field_bytes(2, shape_dims)
        return _field_bytes(1, tensor_type)

    def _value_info(name: str, elem_type: int, dims: list[int | str]) -> bytes:
        return _field_bytes(1, name) + _field_bytes(2, _tensor_type_proto(elem_type, dims))

    # AttributeProto: to = FLOAT (1) on Cast (field 3 = int64 i, field 20 = type INT=2)
    attr_to_float = _field_bytes(1, "to") + _field_varint(3, 1) + _field_varint(20, 2)
    node_cast = (
        _field_bytes(1, "input_ids")
        + _field_bytes(2, "cast_ids")
        + _field_bytes(3, "cast_node")
        + _field_bytes(4, "Cast")
        + _field_bytes(5, attr_to_float)
    )
    # AttributeProto: axes = [1] (repeated int64 ints = field 8, type INTS=7), keepdims = 1
    attr_axes = _field_bytes(1, "axes") + _field_varint(8, 1) + _field_varint(20, 7)
    attr_keepdims = _field_bytes(1, "keepdims") + _field_varint(3, 1) + _field_varint(20, 2)
    node_reduce = (
        _field_bytes(1, "cast_ids")
        + _field_bytes(2, "logits")
        + _field_bytes(3, "reduce_node")
        + _field_bytes(4, "ReduceMean")
        + _field_bytes(5, attr_axes)
        + _field_bytes(5, attr_keepdims)
    )
    graph = (
        _field_bytes(1, node_cast)
        + _field_bytes(1, node_reduce)
        + _field_bytes(2, "tiny_cross_encoder")
        + _field_bytes(11, _value_info("input_ids", 7, ["batch", seq_len]))  # INT64 = 7
        + _field_bytes(12, _value_info("logits", 1, ["batch", 1]))  # FLOAT = 1
    )
    opset = _field_bytes(1, "") + _field_varint(2, 11)
    model = (
        _field_varint(1, 7)  # ir_version = 7
        + _field_bytes(8, opset)
        + _field_bytes(7, graph)
    )
    return model


class V040SotaUpgradesTests(unittest.TestCase):
    def test_track1_hardware_model_incremental_vectors_reranker_and_chat_threads(self) -> None:
        from api_product import ensure_owner_token
        from atom_mutator import apply_atom_edit, apply_atom_merge, content_sha256
        from chat_threads import (
            append_chat_turn,
            create_chat_thread,
            delete_chat_thread,
            get_chat_thread,
            list_chat_threads,
            pin_turn_to_note,
            resolve_followup_query,
        )
        from cross_encoder import InteractionCrossEncoder, OnnxCrossEncoder, clear_cross_encoder_cache
        from hardware_profile import (
            classify_hardware_tier,
            invalidate_hardware_profile_cache,
            resolve_active_local_generation_model,
        )
        from host_adapters import builtin_default_policies
        from index_manifest import index_manifest_is_current, load_index_manifest
        from lexical_store import rebuild_lexical_index
        from memory_api import (
            MemoryApiClient,
            MemoryApiRuntimeConfig,
            MemoryLoopbackHTTPServer,
            generate_memory_api_token,
            reset_api_rate_limiter,
        )
        import rebuild_vectors as rv_mod
        from recall import RecallOptions, search_lexical_memory, search_semantic_memory

        # 1.1 Hardware-profile auto-wiring (`qwen2.5-coder:7b`) + explicit env override
        fake_tier2 = classify_hardware_tier(
            os_name="Windows",
            os_release="11",
            architecture="AMD64",
            cpu_threads=16,
            total_ram_gib=16.0,
            available_ram_gib=10.0,
            cpp_runtime_ready=True,
            cpp_runtime_detail="MSVC C++ runtime present",
            gpu_backend="cuda",
            gpu_name="RTX 4070",
            vram_gib=12.0,
            ollama_reachable=True,
            installed_ollama_models=("qwen2.5-coder:7b", "llama3.2:3b", "nomic-embed-text:latest"),
        )
        invalidate_hardware_profile_cache()
        with patch("hardware_profile.detect_hardware_profile", return_value=fake_tier2):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BRAIN_GEN_MODEL", None)
                self.assertEqual(resolve_active_local_generation_model(use_cache=False), "qwen2.5-coder:7b")
                self.assertEqual(builtin_default_policies()["ollama_local"].default_model, "qwen2.5-coder:7b")
            with patch.dict(os.environ, {"BRAIN_GEN_MODEL": "llama3.2:3b"}, clear=False):
                self.assertEqual(resolve_active_local_generation_model(use_cache=False), "llama3.2:3b")
            # Malformed/path-traversal BRAIN_GEN_MODEL must be rejected and fall back safely
            with patch.dict(os.environ, {"BRAIN_GEN_MODEL": "../../evil?x=1"}, clear=False):
                self.assertEqual(resolve_active_local_generation_model(use_cache=False), "qwen2.5-coder:7b")

        # 1.2 O(|changed|) single-atom Chroma vector upserts ABOVE 64 atoms (> 64 stays current!)
        embed_call_count = 0

        def _counting_embed(text: str, model: str | None = None) -> list[float]:
            nonlocal embed_call_count
            embed_call_count += 1
            return _deterministic_vec(text)

        with tempfile.TemporaryDirectory() as tmp:
            import zipfile
            from build_clipper import build_clipper_zip

            built_zip = build_clipper_zip(Path(tmp) / "dist-clipper")
            with zipfile.ZipFile(built_zip, "r") as zf:
                self.assertIn("lib/readability-markdown.js", zf.namelist())
                self.assertIn("lib/clipper-client.js", zf.namelist())

            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            chroma_dir = brain / ".chroma"
            lex_db = brain / ".sidecar" / "lexical.sqlite3"

            # Bulk-seed 68 approved atoms (> 64 threshold!)
            src_bulk = brain / "0-raw" / "bulk-70.md"
            src_bulk.parent.mkdir(parents=True, exist_ok=True)
            (brain / "0-raw" / "archive").mkdir(parents=True, exist_ok=True)
            (brain / ".sidecar").mkdir(parents=True, exist_ok=True)
            atoms_dir.mkdir(parents=True, exist_ok=True)
            lines = [f"Cluster shard_{i:03d} enforces deterministic consensus epoch {i}." for i in range(68)]
            src_bulk.write_text("\n".join(lines) + "\n", encoding="utf-8")

            from evidence import find_evidence_anchor

            dlog_lines: list[str] = []
            for i, cl in enumerate(lines):
                fn = f"shard-{i:03d}.md"
                draft = (
                    f"---\nid: shard-{i:03d}\ntype: atom\nmemory_type: semantic\nepistemic_label: claim\n"
                    f"sources:\n  - 0-raw/bulk-70.md\ncontexts:\n  - project:scale\n"
                    f"claim_state: human_reviewed\nreview_status: accepted\ncitation_integrity: anchored\n---\n\n"
                    f"## Claim\n{cl}\n\n## Evidence\n{cl}\n"
                )
                anc = find_evidence_anchor(draft, str(brain))
                parts = draft.split("---", 2)
                fm = parts[1] + "".join(f"{k}: {v}\n" for k, v in anc.items())
                full_b = f"---{fm}---{parts[2]}".encode("utf-8")
                (atoms_dir / fn).write_bytes(full_b)
                dlog_lines.append(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "recorded_at": "2026-11-20T00:00:00+00:00",
                            "action": "approve",
                            "actor": "owner",
                            "atom_filename": fn,
                            "content_sha256_before": content_sha256(full_b),
                            "content_sha256_after": content_sha256(full_b),
                            **anc,
                        },
                        sort_keys=True,
                    )
                )
            (brain / "0-raw" / "archive" / "decision-record.jsonl").write_text(
                "\n".join(dlog_lines) + "\n", encoding="utf-8"
            )
            rebuild_lexical_index(str(atoms_dir), str(lex_db))

            with (
                patch.object(rv_mod, "embed_text", side_effect=_counting_embed),
                patch("ollama_utils.embed_text", side_effect=_counting_embed),
            ):
                full_res = rv_mod.rebuild_vectors(
                    atoms_dir=str(atoms_dir),
                    store_path=str(chroma_dir),
                )
                self.assertEqual(full_res["indexed"], 68)
                self.assertEqual(embed_call_count, 69)  # 1 initial dimension probe + 68 atoms

                # Now add & approve atom #69 via _seed_verified_atom -> O(1) incremental upsert!
                embed_call_count = 0
                a69 = _seed_verified_atom(
                    brain,
                    filename="shard-069.md",
                    claim="Cluster shard_069 enforces quantum-resistant Raft snapshots.",
                    project="project:scale",
                    approve=True,
                )
                # O(1) incremental upsert embedded ONLY shard-069.md (1 call, NOT 69 calls!)
                self.assertEqual(embed_call_count, 1)
                self.assertTrue(index_manifest_is_current(load_index_manifest(str(chroma_dir)), str(atoms_dir)))

                # Edit shard-069.md -> demotes to pending -> O(1) upsert of 1 changed atom (1 call, NOT 69!) and still current!
                embed_call_count = 0
                apply_atom_edit(
                    atom_path=str(a69),
                    expected_sha256=content_sha256(a69.read_bytes()),
                    edited_content=a69.read_text(encoding="utf-8").replace("quantum-resistant", "post-quantum"),
                    actor="owner",
                    brain_dir=str(brain),
                    atoms_dir=str(atoms_dir),
                )
                self.assertEqual(embed_call_count, 1)
                self.assertTrue(index_manifest_is_current(load_index_manifest(str(chroma_dir)), str(atoms_dir)))

                # Dense search still recalls untouched shard-005.md and NEVER returns pending shard-069.md
                dense_hits, _ = search_semantic_memory(
                    "Cluster shard_005 enforces deterministic consensus epoch 5.",
                    RecallOptions(n_results=5, projects=("project:scale",), retrieval_mode="dense"),
                    atoms_dir=str(atoms_dir),
                    chroma_path=str(chroma_dir),
                )
                self.assertIn("shard-005.md", [h.filename for h in dense_hits])
                self.assertNotIn("shard-069.md", [h.filename for h in dense_hits])

                # Merge two atoms above 64 -> also stays current in O(1) (embeds only 2 changed files, NOT 70!)
                a70 = _seed_verified_atom(
                    brain,
                    filename="shard-070.md",
                    claim="Duplicate rule for shard merge verification.",
                    project="project:scale",
                    approve=True,
                )
                embed_call_count = 0
                apply_atom_merge(
                    survivor_path=str(atoms_dir / "shard-001.md"),
                    survivor_expected_sha256=content_sha256((atoms_dir / "shard-001.md").read_bytes()),
                    superseded_path=str(a70),
                    superseded_expected_sha256=content_sha256(a70.read_bytes()),
                    actor="owner",
                    rationale="incremental merge test",
                    brain_dir=str(brain),
                    atoms_dir=str(atoms_dir),
                )
                self.assertEqual(embed_call_count, 2)
                self.assertTrue(index_manifest_is_current(load_index_manifest(str(chroma_dir)), str(atoms_dir)))

            # 1.3 Cross-Encoder Reranker (`InteractionCrossEncoder` + real `OnnxCrossEncoder` session)
            ce = InteractionCrossEncoder()
            exact_doc = "## Claim\nMemory API binds strictly to loopback 127.0.0.1 port 8765.\n\n## Evidence\n127.0.0.1:8765"
            scattered_doc = (
                "## Claim\nPort forwarding notes.\n\n## Evidence\n"
                "Some general memory notes. " + (" Filler words. " * 25) + "8765 and 127.0.0.1 mentioned far apart."
            )
            scores = ce.score_pairs(
                "Memory API loopback 127.0.0.1 port 8765",
                [scattered_doc, exact_doc],
            )
            self.assertGreater(scores[1], scores[0])

            # Real ONNX Runtime + HuggingFace tokenizers test
            onnx_dir = Path(tmp) / "onnx-ce"
            onnx_dir.mkdir(parents=True, exist_ok=True)
            (onnx_dir / "model.onnx").write_bytes(_build_minimal_linear_onnx_model(seq_len=8))
            minimal_tok = {
                "version": "1.0",
                "truncation": None,
                "padding": None,
                "added_tokens": [
                    {"id": 0, "content": "[PAD]", "single_word": False, "lstrip": False, "rstrip": False, "normalized": False, "special": True},
                    {"id": 1, "content": "[UNK]", "single_word": False, "lstrip": False, "rstrip": False, "normalized": False, "special": True},
                    {"id": 2, "content": "[CLS]", "single_word": False, "lstrip": False, "rstrip": False, "normalized": False, "special": True},
                    {"id": 3, "content": "[SEP]", "single_word": False, "lstrip": False, "rstrip": False, "normalized": False, "special": True},
                ],
                "normalizer": {"type": "Lowercase"},
                "pre_tokenizer": {"type": "Whitespace"},
                "post_processor": None,
                "decoder": None,
                "model": {
                    "type": "WordLevel",
                    "vocab": {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "loopback": 10, "port": 20},
                    "unk_token": "[UNK]",
                },
            }
            (onnx_dir / "tokenizer.json").write_text(json.dumps(minimal_tok), encoding="utf-8")
            clear_cross_encoder_cache()
            onnx_ce = OnnxCrossEncoder(onnx_dir, max_length=8)
            onnx_scores = onnx_ce.score_pairs("loopback port", ["loopback port", "other"])
            self.assertEqual(len(onnx_scores), 2)
            self.assertGreater(onnx_scores[0], onnx_scores[1])

            # 1.4 Multi-Turn Chat History, Coreference Resolution, Secret Scrubbing & Agent 403 Isolation
            rebuild_lexical_index(str(atoms_dir), str(lex_db))
            th = create_chat_thread(title="", project_scope="project:scale", brain_dir=str(brain))
            tid = th["thread_id"]
            append_chat_turn(tid, "user", "How does cluster shard_005 handle consensus?", brain_dir=str(brain))
            append_chat_turn(
                tid,
                "assistant",
                "Cluster shard_005 enforces deterministic consensus epoch 5 [V1]. Secret sk-abcdefghijklmnopqrstuvwxyz123456",
                citations=[
                    {
                        "citation_id": "V1",
                        "filename": "shard-005.md",
                        "tier": "verified",
                        "excerpt": "Inline secret ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                    }
                ],
                brain_dir=str(brain),
            )
            loaded_th = get_chat_thread(tid, brain_dir=str(brain))
            self.assertEqual(len(loaded_th["turns"]), 2)
            # Outbound/stored secret scrubbing verified on BOTH content and cited_sources/citations!
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", loaded_th["turns"][1]["content"])
            self.assertEqual(loaded_th["turns"][1]["cited_sources"], loaded_th["turns"][1]["citations"])
            self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ", json.dumps(loaded_th["turns"][1]["citations"]))

            expanded_q, was_exp = resolve_followup_query(
                "What epoch does it enforce?",
                [{"role": t["role"], "content": t["content"]} for t in loaded_th["turns"]],
            )
            self.assertTrue(was_exp)
            self.assertIn("shard_005", expanded_q)

            pinned = pin_turn_to_note(tid, tags=["architecture"], brain_dir=str(brain))
            self.assertEqual(pinned["status"], "pinned")
            self.assertTrue((brain / "notes" / str(pinned["filename"])).is_file())

            # Live Loopback API owner-only boundary check on /v1/chat/threads* and thread_id in /v1/chat/stream
            reset_api_rate_limiter()
            tok_path = Path(tmp) / "api-tok"
            own_path = Path(tmp) / "own-tok"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                inbox_dir=str(brain / "0-raw" / "inbox"),
                experiences_dir=str(brain / "4-experiences"),
                lexical_index_path=str(lex_db),
                chroma_path=str(chroma_dir),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
                token_path=tok_path,
                owner_token_path=own_path,
                policy_path=Path(tmp) / "policy.json",
                security_dir=Path(tmp) / "sec",
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            port = server.server_address[1]
            srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
            srv_thread.start()
            try:
                sdk_agent = MemoryApiClient(port=port, token=agent_tok)
                sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)

                with self.assertRaises(httpx.HTTPStatusError) as ctx_list:
                    sdk_agent.list_chat_threads()
                self.assertEqual(ctx_list.exception.response.status_code, 403)

                with self.assertRaises(httpx.HTTPStatusError) as ctx_stream_tid:
                    sdk_agent.chat_stream("What about shard_005?", mode="instant", thread_id=tid)
                self.assertEqual(ctx_stream_tid.exception.response.status_code, 403)

                # Plain agent CAN pass ephemeral stateless history without thread_id
                agent_evs = sdk_agent.chat_stream(
                    "What epoch does it enforce?",
                    mode="instant",
                    project_scope="project:scale",
                    history=[{"role": "user", "content": "Tell me about cluster shard_005"}],
                )
                self.assertTrue(any(e.get("type") == "context" for e in agent_evs))

                # Owner creates, streams, pins, and deletes persistent thread
                api_th = sdk_owner.create_chat_thread(title="API Thread", project_scope="project:scale", mode="instant")
                api_tid = str(api_th["thread_id"])
                owner_evs = sdk_owner.chat_stream(
                    "Cluster shard_005 consensus",
                    mode="instant",
                    project_scope="project:scale",
                    thread_id=api_tid,
                )
                self.assertTrue(any(e.get("thread_id") == api_tid for e in owner_evs if e.get("type") == "context"))
                pin_api = sdk_owner.pin_chat_turn(api_tid)
                self.assertEqual(pin_api["status"], "pinned")
                del_api = sdk_owner.delete_chat_thread(api_tid)
                self.assertEqual(del_api["status"], "deleted")
            finally:
                server.shutdown()
                server.server_close()
            _ = search_lexical_memory
            _ = list_chat_threads
            _ = delete_chat_thread

    def test_track2_polarity_precision_fast_nli_autolinking_and_antigravity_plugin(self) -> None:
        from ide_connector import connect as ide_connect
        from knowledge_graph import auto_link_vault_atoms, build_knowledge_graph, detect_contradictions
        from notes_store import NotePaths, create_note, verify_note_excerpt

        # 2.1 Live vault regression check: 0 false-positive contradictions on actual brain/ vault
        live_brain = REPO_ROOT / "brain"
        if (live_brain / "2-atoms").is_dir():
            live_radar = detect_contradictions(brain_dir=str(live_brain), atoms_dir=str(live_brain / "2-atoms"))
            self.assertEqual(live_radar["contradiction_count"], 0, live_radar["alerts"])

        with tempfile.TemporaryDirectory() as tmp:
            import zipfile
            from build_clipper import build_clipper_zip

            built_zip = build_clipper_zip(Path(tmp) / "dist-clipper")
            with zipfile.ZipFile(built_zip, "r") as zf:
                self.assertIn("lib/readability-markdown.js", zf.namelist())
                self.assertIn("lib/clipper-client.js", zf.namelist())

            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"

            # Former false-positive patterns: Markdown bold period `.**`, contrastive `but`, and ordered list ordinals `4.` vs `6.`
            _seed_verified_atom(
                brain,
                filename="rule-list-4.md",
                claim=(
                    "4. **Trust & Citation Integrity.** Every canonical memory atom must verify source byte-offsets, "
                    "so unverified drafts never enter default agent recall."
                ),
                project="project:sec",
            )
            _seed_verified_atom(
                brain,
                filename="rule-list-6.md",
                claim=(
                    "6. **Structural Provenance.** Every canonical memory atom verifies source byte-offsets, "
                    "but quarantined captures stay isolated until owner review."
                ),
                project="project:sec",
            )
            clean_radar = detect_contradictions(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                project_scope="project:sec",
                enable_nli=True,
                nli_mode="fast",
            )
            self.assertEqual(clean_radar["contradiction_count"], 0, clean_radar["alerts"])

            # True predicate-aligned polarity conflict MUST be caught by both heuristic & fast NLI!
            _seed_verified_atom(
                brain,
                filename="tls-enabled.md",
                claim="Internal gRPC control-plane transport must enforce mutual TLS authentication in production.",
                project="project:sec",
            )
            _seed_verified_atom(
                brain,
                filename="tls-disabled.md",
                claim="Internal gRPC control-plane transport must never enforce mutual TLS authentication in production.",
                project="project:sec",
            )
            true_radar = detect_contradictions(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                project_scope="project:sec",
                enable_nli=True,
                nli_mode="fast",
            )
            self.assertGreaterEqual(true_radar["contradiction_count"], 1)
            self.assertEqual(true_radar["alerts"][0]["nli_adjudication"]["verdict"], "contradiction")

            # 2.2 Automatic [[wikilink]], duplicate, and contradiction candidate linking during verify_note_excerpt()
            npaths = NotePaths.for_brain(brain)
            note = create_note(
                "Control Plane Security Note",
                (
                    "Internal gRPC control-plane transport must enforce mutual TLS authentication "
                    "with hardware-backed client certificates in production.\n"
                ),
                tags=["project:sec"],
                paths=npaths,
            )
            verified = verify_note_excerpt(
                str(note["filename"]),
                (
                    "Internal gRPC control-plane transport must enforce mutual TLS authentication "
                    "with hardware-backed client certificates in production."
                ),
                expected_note_sha256=str(note["sha256"]),
                actor="owner",
                paths=npaths,
            )
            self.assertIn("tls-enabled.md", verified["auto_linked_atoms"])
            self.assertTrue(len(verified["contradiction_alerts"]) >= 1)

            kg_before = build_knowledge_graph(brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertTrue(
                any(
                    e["from"] == verified["atom_filename"] and e["to"] == "tls-enabled.md" and e["kind"] == "link"
                    for e in kg_before["edges"]
                )
            )

            # Vault-wide auto_link_vault_atoms links orphan related atoms while preserving approval currency
            _seed_verified_atom(
                brain,
                filename="orphan-citation-rule.md",
                claim="Every canonical memory atom verifies source byte-offsets and SHA-256 citation integrity before recall.",
                project="project:sec",
            )
            al_res = auto_link_vault_atoms(
                actor="owner",
                project_scope="project:sec",
                dry_run=False,
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )
            self.assertGreaterEqual(al_res["updated_atom_count"], 1)
            self.assertLess(al_res["orphan_after"], al_res["orphan_before"])

            # Cross-project & unscoped isolation: unscoped claim MUST NOT auto-link confidential project:sec atoms!
            from knowledge_graph import suggest_atom_graph_links_for_claim

            unscoped_sug = suggest_atom_graph_links_for_claim(
                "Internal gRPC control-plane transport must enforce mutual TLS authentication in production.",
                contexts=(),
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )
            self.assertEqual(unscoped_sug["related_links"], [])
            self.assertEqual(unscoped_sug["contradiction_alerts"], [])

        # 2.3 Antigravity plugin 17/17 MCP tools parity
        ag_dry = ide_connect("antigravity", dry_run=True)
        self.assertEqual(ag_dry["host"], "antigravity")
        plugin_skill = (
            REPO_ROOT / "plugins" / "antigravity" / "second-brain" / "skills" / "second-brain-memory" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for tool_name in (
            "search_memory",
            "get_atom",
            "search_notes",
            "get_note",
            "remember",
            "add_to_inbox",
            "list_playbooks",
            "get_playbook",
            "assemble_plan",
            "record_experience",
            "record_feedback",
            "list_contradictions",
            "graph_neighbors",
            "list_snapshots",
            "recall_at_snapshot",
            "verify_ledger",
            "prove_leaf",
        ):
            self.assertIn(tool_name, plugin_skill)

    def test_track3_readability_clipper_oneclick_pairing_and_layout_ocr_pdf(self) -> None:
        from api_product import ensure_owner_token
        from audit_sources import audit_sources
        from doc_extractors import extract_pdf
        from doc_fixtures import build_positioned_pdf, build_scanned_image_pdf
        from memory_api import (
            MemoryApiClient,
            MemoryApiRuntimeConfig,
            MemoryLoopbackHTTPServer,
            generate_memory_api_token,
            reset_api_rate_limiter,
        )
        from web_clipper import extract_clean_markdown_from_html

        # 3.1 Node.js Readability DOM-to-Markdown + 1-click pairing client tests
        node_proc = subprocess.run(
            ["node", "--test", str(REPO_ROOT / "extensions" / "browser-clipper" / "test" / "clipper.test.js")],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(node_proc.returncode, 0, node_proc.stdout + "\n" + node_proc.stderr)

        # Server-side trafilatura / BS4 HTML-to-Markdown extraction
        noisy_html = """
        <html><body>
          <nav><a href="/login">Login</a> | <a href="/pricing">Pricing</a></nav>
          <article>
            <h1>Deterministic Memory Architecture</h1>
            <p>Canonical atoms verify <strong>UTF-8 byte offsets</strong> against immutable raw sources.</p>
            <table>
              <tr><th>Store</th><th>Role</th></tr>
              <tr><td>2-atoms/</td><td>Canonical Truth</td></tr>
            </table>
          </article>
          <footer>Cookie Preferences &amp; Newsletter</footer>
        </body></html>
        """
        md_clean = extract_clean_markdown_from_html(noisy_html, source_url="https://example.org/spec")
        self.assertIn("Deterministic Memory Architecture", md_clean)
        self.assertIn("UTF-8 byte offsets", md_clean)
        self.assertIn("2-atoms/", md_clean)

        with tempfile.TemporaryDirectory() as tmp:
            import zipfile
            from build_clipper import build_clipper_zip

            built_zip = build_clipper_zip(Path(tmp) / "dist-clipper")
            with zipfile.ZipFile(built_zip, "r") as zf:
                self.assertIn("lib/readability-markdown.js", zf.namelist())
                self.assertIn("lib/clipper-client.js", zf.namelist())

            brain = Path(tmp) / "brain"
            sec_dir = Path(tmp) / "sec"
            tok_path = sec_dir / "api-tok"
            own_path = sec_dir / "own-tok"
            sec_dir.mkdir(parents=True, exist_ok=True)
            for sub in ("0-raw/inbox", "2-atoms", "4-experiences", ".sidecar", "notes"):
                (brain / sub).mkdir(parents=True, exist_ok=True)

            # 1-Click Pairing Code flow over live Loopback HTTP API (CORS preflight + single-use + replay block + HTML clip)
            reset_api_rate_limiter()
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
                inbox_dir=str(brain / "0-raw" / "inbox"),
                experiences_dir=str(brain / "4-experiences"),
                lexical_index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
                chroma_path=str(brain / ".chroma"),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
                token_path=tok_path,
                owner_token_path=own_path,
                policy_path=Path(tmp) / "policy.json",
                security_dir=sec_dir,
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            port = server.server_address[1]
            srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
            srv_thread.start()
            try:
                sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)
                code_res = sdk_owner.create_clipper_pairing_code(label="Edge Clipper", ttl_seconds=120)
                code = str(code_res["code"])
                self.assertTrue(code.startswith("SBC-"))

                ext_origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
                base_url = f"http://127.0.0.1:{port}"
                with httpx.Client(
                    trust_env=False,
                    timeout=10,
                    limits=httpx.Limits(max_keepalive_connections=0, max_connections=20),
                    headers={"Connection": "close"},
                ) as client:
                    # Unpaired extension preflight to /v1/clipper/redeem MUST succeed (204)
                    opt_r = client.options(
                        f"{base_url}/v1/clipper/redeem",
                        headers={
                            "Host": f"127.0.0.1:{port}",
                            "Origin": ext_origin,
                            "Access-Control-Request-Method": "POST",
                        },
                    )
                    self.assertEqual(opt_r.status_code, 204)

                    # Web origin https://evil.example MUST be rejected (403) even on /v1/clipper/redeem!
                    evil_r = client.post(
                        f"{base_url}/v1/clipper/redeem",
                        headers={"Host": f"127.0.0.1:{port}", "Origin": "https://evil.example"},
                        json={"code": code, "origin": ext_origin},
                    )
                    self.assertEqual(evil_r.status_code, 403)

                    # Legitimate extension origin redeems code once -> receives scoped clipper_* token
                    redeem_r = client.post(
                        f"{base_url}/v1/clipper/redeem",
                        headers={
                            "Host": f"127.0.0.1:{port}",
                            "Origin": ext_origin,
                            "Sec-Fetch-Site": "cross-site",
                        },
                        json={"code": code, "origin": ext_origin, "label": "Edge Dev"},
                    )
                    self.assertEqual(redeem_r.status_code, 201, redeem_r.text)
                    clipper_tok = redeem_r.json()["token"]
                    self.assertTrue(clipper_tok.startswith("clipper_"))

                    # Replay of single-use code MUST fail with 403!
                    replay_r = client.post(
                        f"{base_url}/v1/clipper/redeem",
                        headers={"Host": f"127.0.0.1:{port}", "Origin": ext_origin},
                        json={"code": code, "origin": ext_origin},
                    )
                    self.assertEqual(replay_r.status_code, 403)

                    # Per-origin lockout isolation: rogue extension exhausting its 5 attempts MUST NOT lock out another extension!
                    rogue_origin = "chrome-extension://pppppppppppppppppppppppppppppppp"
                    code_res2 = sdk_owner.create_clipper_pairing_code(label="Firefox Clipper", ttl_seconds=120)
                    for _ in range(6):
                        r_bad = client.post(
                            f"{base_url}/v1/clipper/redeem",
                            headers={"Host": f"127.0.0.1:{port}", "Origin": rogue_origin},
                            json={"code": "SBC-2222-2222-2222", "origin": rogue_origin},
                        )
                    self.assertEqual(r_bad.status_code, 429)
                    ff_origin = "moz-extension://12345678-1234-1234-1234-1234567890ab"
                    ff_ok = client.post(
                        f"{base_url}/v1/clipper/redeem",
                        headers={"Host": f"127.0.0.1:{port}", "Origin": ff_origin},
                        json={"code": str(code_res2["code"]), "origin": ff_origin},
                    )
                    self.assertEqual(ff_ok.status_code, 201, ff_ok.text)

                    # Aggregate per-IP lockout: rotating spoofed random extension Origins MUST trigger 429 at 20 attempts!
                    last_rot = None
                    for idx in range(20):
                        hex16 = f"{idx:032x}".translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
                        spoof_o = f"chrome-extension://{hex16}"
                        posted = None
                        transport_error: Exception | None = None
                        for _attempt in range(4):
                            try:
                                posted = client.post(
                                    f"{base_url}/v1/clipper/redeem",
                                    headers={"Host": f"127.0.0.1:{port}", "Origin": spoof_o},
                                    json={"code": "SBC-3333-3333-3333", "origin": spoof_o},
                                )
                                break
                            except httpx.TransportError as exc:
                                transport_error = exc
                        if posted is None:
                            raise transport_error or RuntimeError("clipper redeem request failed")
                        last_rot = posted
                    assert last_rot is not None
                    self.assertEqual(last_rot.status_code, 429)

                    # Clip web page with raw_html fallback via paired extension token
                    clip_r = client.post(
                        f"{base_url}/v1/clipper/clip",
                        headers={
                            "Host": f"127.0.0.1:{port}",
                            "Origin": ext_origin,
                            "Authorization": f"Bearer {clipper_tok}",
                            "Sec-Fetch-Site": "cross-site",
                        },
                        json={
                            "url": "https://example.org/spec",
                            "title": "Architecture Spec",
                            "raw_html": noisy_html,
                            "destination": "note",
                            "tags": ["project:clip"],
                        },
                    )
                    self.assertEqual(clip_r.status_code, 201, clip_r.text)
                    saved_note = (brain / "notes" / clip_r.json()["filename"]).read_text(encoding="utf-8")
                    self.assertIn("Deterministic Memory Architecture", saved_note)
            finally:
                server.shutdown()
                server.server_close()

            # 3.2 Two-column PDF reading-order de-interleaving + Markdown pipe table reconstruction
            two_col_pdf = Path(tmp) / "two_col_and_table.pdf"
            two_col_pdf.write_bytes(
                build_positioned_pdf(
                    [
                        # Page 1: 2-column academic layout at identical Y coordinates!
                        [
                            (50.0, 720.0, "Left Column Alpha Line One"),
                            (330.0, 720.0, "Right Column Beta Line One"),
                            (50.0, 700.0, "Left Column Alpha Line Two"),
                            (330.0, 700.0, "Right Column Beta Line Two"),
                            (50.0, 680.0, "Left Column Alpha Line Three"),
                            (330.0, 680.0, "Right Column Beta Line Three"),
                            (50.0, 660.0, "Left Column Alpha Line Four"),
                            (330.0, 660.0, "Right Column Beta Line Four"),
                        ],
                        # Page 2: Multi-column aligned benchmark table!
                        [
                            (60.0, 720.0, "System"),
                            (240.0, 720.0, "RecallAt5"),
                            (400.0, 720.0, "TrapFPR"),
                            (60.0, 700.0, "SecondBrain"),
                            (240.0, 700.0, "1.000"),
                            (400.0, 700.0, "0.000"),
                            (60.0, 680.0, "NaiveRAG"),
                            (240.0, 680.0, "0.650"),
                            (400.0, 680.0, "0.400"),
                        ],
                    ]
                )
            )
            extracted_2col = extract_pdf(two_col_pdf)
            md_2col = extracted_2col.markdown
            # Left column MUST appear completely before Right column (zero interleaved lines!)
            self.assertLess(md_2col.index("Left Column Alpha Line Four"), md_2col.index("Right Column Beta Line One"))
            # Page 2 table reconstructed as GFM pipe table!
            self.assertIn("| System | RecallAt5 | TrapFPR |", md_2col)

            # Policy-gated Ollama Vision OCR fallback test (enabled vs policy-disabled)
            from doc_extractors import _run_local_ocr_on_png
            from host_adapters import HostConsentError

            class _FakeOllamaClient:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat(self, *args: object, **kwargs: object) -> dict[str, object]:
                    return {"message": {"content": "Vision OCR transcribed clause 42."}}

            with (
                patch("shutil.which", return_value=None),
                patch.dict(os.environ, {"BRAIN_TESSERACT_CMD": ""}, clear=False),
                patch("ollama.Client", _FakeOllamaClient),
                patch("ollama_utils._resolve_local_ollama_rule_and_url", return_value=(object(), "http://127.0.0.1:11434")),
            ):
                vis_txt, vis_eng = _run_local_ocr_on_png(b"\x89PNG\r\n\x1a\n", 1, allow_vision_ocr=True)
                self.assertEqual(vis_txt, "Vision OCR transcribed clause 42.")
                self.assertTrue(str(vis_eng).startswith("ollama-vision:"))

            with (
                patch("shutil.which", return_value=None),
                patch.dict(os.environ, {"BRAIN_TESSERACT_CMD": ""}, clear=False),
                patch("ollama.Client", _FakeOllamaClient),
                patch("ollama_utils._resolve_local_ollama_rule_and_url", side_effect=HostConsentError("disabled")),
            ):
                dis_txt, dis_eng = _run_local_ocr_on_png(b"\x89PNG\r\n\x1a\n", 1, allow_vision_ocr=True)
                self.assertEqual(dis_txt, "")
                self.assertIsNone(dis_eng)
            self.assertIn("| SecondBrain | 1.000 | 0.000 |", md_2col)

            # Scanned image-only PDF -> local OCR fallback -> exact UTF-8 byte-span verification!
            scanned_pdf = Path(tmp) / "scanned_contract.pdf"
            scanned_pdf.write_bytes(build_scanned_image_pdf(width=120, height=60))
            ocr_doc = extract_pdf(
                scanned_pdf,
                ocr_fn=lambda png_bytes: "Scanned Clause 9: Backup encryption keys rotate every 90 days." if png_bytes.startswith(b"\x89PNG") else "",
            )
            self.assertIn("[page 1]", ocr_doc.markdown)
            self.assertIn("Scanned Clause 9: Backup encryption keys rotate every 90 days.", ocr_doc.markdown)
            self.assertEqual(ocr_doc.extractor, "pypdf-layout+ocr")

            # Save extracted OCR markdown into 0-raw/ and prove byte-offset source anchor verification!
            ocr_raw_path = brain / "0-raw" / "scanned-extracted.md"
            ocr_raw_path.write_text(ocr_doc.markdown, encoding="utf-8")
            from atom_mutator import apply_review_transition, content_sha256
            from evidence import find_evidence_anchor

            ocr_quote = "Scanned Clause 9: Backup encryption keys rotate every 90 days."
            ocr_draft = (
                "---\nid: ocr-clause-9\ntype: atom\nmemory_type: semantic\nepistemic_label: claim\n"
                "sources:\n  - 0-raw/scanned-extracted.md\ncontexts:\n  - project:ocr\n"
                "claim_state: candidate\nreview_status: pending\ncitation_integrity: anchored\n---\n\n"
                f"## Claim\n{ocr_quote}\n\n## Evidence\n{ocr_quote}\n"
            )
            anc = find_evidence_anchor(ocr_draft, str(brain))
            parts = ocr_draft.split("---", 2)
            ocr_atom = brain / "2-atoms" / "ocr-clause-9.md"
            ocr_atom.write_text(f"---{parts[1]}" + "".join(f"{k}: {v}\n" for k, v in anc.items()) + f"---{parts[2]}", encoding="utf-8")
            apply_review_transition(
                atom_path=str(ocr_atom),
                action="approve",
                expected_sha256=content_sha256(ocr_atom.read_bytes()),
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
            )
            audit_rep = audit_sources(atoms_dir=str(brain / "2-atoms"), brain_dir=str(brain))
            ocr_rep = next(r for r in audit_rep if r["atom_id"] == "ocr-clause-9.md")
            self.assertEqual(ocr_rep["anchor_status"], "current")
            self.assertTrue(ocr_rep["approval_current"])

    def test_track4_s3_sigv4_dpapi_keychain_autosync_3way_conflicts_and_ui_parity(self) -> None:
        from atom_mutator import approved_revision_is_current, content_sha256
        from index_manifest import index_manifest_is_current, load_index_manifest
        from keychain_store import KeychainError, delete_secret, load_secret, store_secret
        from lexical_store import rebuild_lexical_index
        import rebuild_vectors as rv_mod
        from vault_sync import (
            SyncConflictError,
            configure_auto_sync,
            get_sync_conflict_detail,
            init_sync,
            join_sync,
            migrate_sync_key_to_keychain,
            resolve_sync_conflict,
            run_auto_sync_cycle,
            sign_s3_sigv4_headers,
            stop_all_auto_sync_schedulers,
            sync_config_path,
            sync_gc,
            sync_pull,
            sync_push,
            sync_status,
            sync_verify,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sec_a = tmp_path / "sec_a"
            sec_b = tmp_path / "sec_b"
            vault_a = tmp_path / "vault_a"
            vault_b = tmp_path / "vault_b"

            # 4.1 Native OS Keychain / Windows DPAPI round-trip + tamper detection
            secret_raw = os.urandom(32)
            meta = store_secret("second-brain.test", "acct-1", secret_raw, state_dir=sec_a)
            self.assertEqual(load_secret("second-brain.test", "acct-1", state_dir=sec_a), secret_raw)
            if sys.platform == "win32":
                self.assertEqual(meta["backend"], "windows_dpapi")
                dpapi_files = list((sec_a / "keychain").glob("*.dpapi"))
                self.assertEqual(len(dpapi_files), 1)
                on_disk = dpapi_files[0].read_bytes()
                self.assertNotIn(secret_raw, on_disk)
                # Flip 1 byte in DPAPI ciphertext -> MUST fail closed with KeychainError!
                tampered = bytearray(on_disk)
                tampered[-1] ^= 0xFF
                dpapi_files[0].write_bytes(bytes(tampered))
                with self.assertRaises(KeychainError):
                    load_secret("second-brain.test", "acct-1", state_dir=sec_a)
            delete_secret("second-brain.test", "acct-1", state_dir=sec_a)
            with self.assertRaises(KeychainError):
                load_secret("second-brain.test", "acct-1", state_dir=sec_a, backend="tampered_backend")
            # Pinned native backend must never fall through to unencrypted .key file!
            (sec_a / "keychain").mkdir(parents=True, exist_ok=True)
            self.assertIsNone(load_secret("second-brain.test", "acct-1", state_dir=sec_a, backend="linux_secret_service"))

            # 4.2 Strict RFC S3/R2 SigV4 Mock Object Store Transport (verifies AWS4-HMAC-SHA256 on EVERY request!)
            from vault_sync import SyncSecurityError, _parse_s3_list_objects_xml

            # >4 KB comment prolog before <!DOCTYPE MUST still be rejected fail-closed!
            padded_xxe = b'<?xml version="1.0"?><!-- ' + (b"A" * 4200) + b' --><!DOCTYPE lol [<!ENTITY x "y">]><ListBucketResult/>'
            with self.assertRaises(SyncSecurityError):
                _parse_s3_list_objects_xml(padded_xxe)
            # '+' in query string and x-amz-* extra headers MUST be canonicalized per AWS SigV4
            plus_hdrs = sign_s3_sigv4_headers(
                "GET",
                "https://my-bucket.s3.us-east-1.amazonaws.com/?list-type=2&continuation-token=abc+def",
                b"",
                access_key="AKIAIOSFODNN7EXAMPLE",
                secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                region="us-east-1",
                extra_headers={"x-amz-Expected-Bucket-Owner": "123456789012"},
            )
            self.assertIn("x-amz-expected-bucket-owner", plus_hdrs["Authorization"])

            s3_objects: dict[str, bytes] = {}
            test_ak = "AKIAIOSFODNN7EXAMPLE"
            test_sk = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            test_region = "auto"

            def _s3_handler(request: httpx.Request) -> httpx.Response:
                auth_hdr = request.headers.get("Authorization", "")
                amz_date = request.headers.get("x-amz-date", "")
                body_bytes = request.read()
                expected = sign_s3_sigv4_headers(
                    request.method,
                    str(request.url),
                    body_bytes,
                    access_key=test_ak,
                    secret_key=test_sk,
                    region=test_region,
                    amz_date=amz_date,
                )
                if not hmac.compare_digest(auth_hdr, expected["Authorization"]):
                    return httpx.Response(403, text="<Error><Code>SignatureDoesNotMatch</Code></Error>")
                if request.headers.get("x-amz-content-sha256") != hashlib.sha256(body_bytes).hexdigest():
                    return httpx.Response(400, text="<Error><Code>XAmzContentSHA256Mismatch</Code></Error>")

                parsed = urlparse(str(request.url))
                qmap = dict(parse_qsl(parsed.query))
                # Bucket prefix: /my-r2-bucket/...
                rel_path = unquote(parsed.path).lstrip("/")
                if rel_path.startswith("my-r2-bucket"):
                    key = rel_path[len("my-r2-bucket") :].lstrip("/")
                else:
                    key = rel_path

                if request.method == "GET" and qmap.get("list-type") == "2":
                    pref = qmap.get("prefix", "")
                    matched = sorted(k for k in s3_objects if k.startswith(pref))
                    contents_xml = "".join(f"<Contents><Key>{k}</Key><Size>{len(s3_objects[k])}</Size></Contents>" for k in matched)
                    xml_doc = (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        f"<IsTruncated>false</IsTruncated>{contents_xml}</ListBucketResult>"
                    )
                    return httpx.Response(200, content=xml_doc.encode("utf-8"), headers={"Content-Type": "application/xml"})

                if request.method == "HEAD":
                    return httpx.Response(200 if key in s3_objects else 404)
                if request.method == "GET":
                    if key not in s3_objects:
                        return httpx.Response(404)
                    return httpx.Response(200, content=s3_objects[key], headers={"Content-Length": str(len(s3_objects[key]))})
                if request.method == "PUT":
                    s3_objects[key] = body_bytes
                    return httpx.Response(200)
                if request.method == "DELETE":
                    existed = s3_objects.pop(key, None) is not None
                    return httpx.Response(204 if existed else 404)
                return httpx.Response(405)

            mock_s3 = httpx.MockTransport(_s3_handler)
            s3_url = "https://acct123.r2.cloudflarestorage.com/my-r2-bucket"

            _seed_verified_atom(
                vault_a,
                filename="cloud-rule.md",
                claim="Zero-knowledge cloud sync signs every S3 request with SigV4.",
                project="project:cloud",
                approve=True,
            )

            with patch.dict(
                os.environ,
                {"SB_R2_ACCESS_KEY": test_ak, "SB_R2_SECRET_KEY": test_sk},
                clear=False,
            ):
                init_res = init_sync(
                    s3_url,
                    backend_type="http_object",
                    brain_dir=vault_a,
                    device_name="devA",
                    use_keychain=True,
                    auth_mode="s3_sigv4",
                    s3_region="auto",
                    s3_access_key_env="SB_R2_ACCESS_KEY",
                    s3_secret_key_env="SB_R2_SECRET_KEY",
                    state_dir=sec_a,
                    http_transport=mock_s3,
                    skip_dns_check_for_mock_transport=True,
                )
                self.assertEqual(init_res["kdf_mode"], "os_keychain")
                self.assertTrue(init_res["keychain_sealed"])
                rec_key = str(init_res["recovery_key_b64"])

                # Verify vault-<slug>.json on disk contains ZERO plaintext master_key_b64 and ZERO raw S3 secret!
                cfg_a_raw = sync_config_path(vault_a, state_dir=sec_a).read_text(encoding="utf-8")
                cfg_a_json = json.loads(cfg_a_raw)
                self.assertIsNone(cfg_a_json["master_key_b64"])
                self.assertNotIn(rec_key, cfg_a_raw)
                self.assertNotIn(test_sk, cfg_a_raw)

                push1 = sync_push(
                    brain_dir=vault_a,
                    state_dir=sec_a,
                    http_transport=mock_s3,
                    skip_dns_check_for_mock_transport=True,
                )
                self.assertEqual(push1["epoch"], 1)

                # Seed Vault B with an active cosine Chroma index so sync_pull tests incremental vector refresh!
                for sub in ("0-raw/archive", "2-atoms", ".sidecar", ".chroma", "notes"):
                    (vault_b / sub).mkdir(parents=True, exist_ok=True)
                _seed_verified_atom(
                    vault_b,
                    filename="local-b-seed.md",
                    claim="Local device B baseline rule.",
                    project="project:cloud",
                    approve=True,
                )
                rebuild_lexical_index(str(vault_b / "2-atoms"), str(vault_b / ".sidecar" / "lexical.sqlite3"))
                with (
                    patch.object(rv_mod, "embed_text", side_effect=lambda t, model=None: _deterministic_vec(t)),
                    patch("ollama_utils.embed_text", side_effect=lambda t, model=None: _deterministic_vec(t)),
                ):
                    rv_mod.rebuild_vectors(
                        atoms_dir=str(vault_b / "2-atoms"),
                        store_path=str(vault_b / ".chroma"),
                    )

                # Join Vault B initially in keyfile mode, then test migrate_sync_key_to_keychain!
                join_sync(
                    s3_url,
                    backend_type="http_object",
                    brain_dir=vault_b,
                    device_name="devB",
                    recovery_key_b64=rec_key,
                    use_keychain=False,
                    auth_mode="s3_sigv4",
                    s3_region="auto",
                    s3_access_key_env="SB_R2_ACCESS_KEY",
                    s3_secret_key_env="SB_R2_SECRET_KEY",
                    state_dir=sec_b,
                    http_transport=mock_s3,
                    skip_dns_check_for_mock_transport=True,
                )
                mig_res = migrate_sync_key_to_keychain(brain_dir=vault_b, state_dir=sec_b)
                # An isolated state_dir never writes the login keychain. Linux and macOS
                # then seal into the restricted keyfile and report keychain_sealed False.
                # Windows DPAPI still reports True. Either way the JSON master key is gone.
                backend = str(mig_res["keychain_backend"])
                self.assertIn(
                    backend,
                    {"windows_dpapi", "macos_keychain", "linux_secret_service", "posix_restricted_keyfile"},
                )
                if backend == "posix_restricted_keyfile":
                    self.assertFalse(mig_res["keychain_sealed"])
                else:
                    self.assertTrue(mig_res["keychain_sealed"])
                self.assertIsNone(json.loads(sync_config_path(vault_b, state_dir=sec_b).read_text(encoding="utf-8"))["master_key_b64"])

                with (
                    patch.object(rv_mod, "embed_text", side_effect=lambda t, model=None: _deterministic_vec(t)),
                    patch("ollama_utils.embed_text", side_effect=lambda t, model=None: _deterministic_vec(t)),
                ):
                    pull_b = sync_pull(
                        brain_dir=vault_b,
                        state_dir=sec_b,
                        http_transport=mock_s3,
                        skip_dns_check_for_mock_transport=True,
                    )
                self.assertTrue(pull_b["vectors_updated"])
                self.assertTrue(
                    index_manifest_is_current(
                        load_index_manifest(str(vault_b / ".chroma")),
                        str(vault_b / "2-atoms"),
                    )
                )

                ver_res = sync_verify(
                    brain_dir=vault_b,
                    state_dir=sec_b,
                    http_transport=mock_s3,
                    skip_dns_check_for_mock_transport=True,
                )
                self.assertTrue(ver_res["valid"], ver_res["issues"])

                # Inject an unreferenced orphan ciphertext blob on S3 and run sync_gc!
                s3_objects["objects/aa/" + ("a" * 64) + ".enc"] = b"orphan-ciphertext"
                gc_dry = sync_gc(
                    brain_dir=vault_a,
                    dry_run=True,
                    state_dir=sec_a,
                    http_transport=mock_s3,
                    skip_dns_check_for_mock_transport=True,
                )
                self.assertEqual(gc_dry["deleted_blobs"], 1)
                self.assertIn("objects/aa/" + ("a" * 64) + ".enc", s3_objects)

                gc_real = sync_gc(
                    brain_dir=vault_a,
                    dry_run=False,
                    state_dir=sec_a,
                    http_transport=mock_s3,
                    skip_dns_check_for_mock_transport=True,
                )
                self.assertEqual(gc_real["deleted_blobs"], 1)
                self.assertNotIn("objects/aa/" + ("a" * 64) + ".enc", s3_objects)

                # 4.3 AutoSyncScheduler cycle + automatic conflict pause + Interactive 3-Way Conflict Resolver
                try:
                    # Push Vault B's local-b-seed.md via AutoSync cycle!
                    configure_auto_sync(
                        enabled=True,
                        interval_seconds=60,
                        start_background=False,
                        brain_dir=vault_b,
                        state_dir=sec_b,
                    )
                    cyc_b = run_auto_sync_cycle(
                        brain_dir=vault_b,
                        state_dir=sec_b,
                        http_transport=mock_s3,
                        skip_dns_check_for_mock_transport=True,
                    )
                    self.assertEqual(cyc_b["status"], "synced")
                    self.assertTrue(cyc_b["pushed"])

                    # Pull epoch 2 into Vault A so both devices share identical common ancestor on cloud-rule.md & shared-note.md
                    (vault_a / "notes").mkdir(parents=True, exist_ok=True)
                    (vault_a / "notes" / "shared-roadmap.md").write_text("# Roadmap\n\nBase ancestor line.\n", encoding="utf-8")
                    sync_pull(
                        brain_dir=vault_a,
                        state_dir=sec_a,
                        http_transport=mock_s3,
                        skip_dns_check_for_mock_transport=True,
                    )
                    sync_push(
                        brain_dir=vault_a,
                        state_dir=sec_a,
                        http_transport=mock_s3,
                        skip_dns_check_for_mock_transport=True,
                    )
                    sync_pull(
                        brain_dir=vault_b,
                        state_dir=sec_b,
                        http_transport=mock_s3,
                        skip_dns_check_for_mock_transport=True,
                    )

                    # Create divergent edits on BOTH shared-roadmap.md AND canonical 2-atoms/cloud-rule.md!
                    (vault_a / "notes" / "shared-roadmap.md").write_text("# Roadmap\n\nDevice A milestone.\n", encoding="utf-8")
                    atom_a_txt = (vault_a / "2-atoms" / "cloud-rule.md").read_text(encoding="utf-8")
                    (vault_a / "2-atoms" / "cloud-rule.md").write_text(
                        atom_a_txt.replace("SigV4.", "SigV4 with pinned TLS."), encoding="utf-8"
                    )
                    sync_push(
                        brain_dir=vault_a,
                        state_dir=sec_a,
                        http_transport=mock_s3,
                        skip_dns_check_for_mock_transport=True,
                    )

                    # Edit locally on Vault B before pulling -> triggers 2 conflicts!
                    (vault_b / "notes" / "shared-roadmap.md").write_text("# Roadmap\n\nDevice B milestone.\n", encoding="utf-8")
                    atom_b_txt = (vault_b / "2-atoms" / "cloud-rule.md").read_text(encoding="utf-8")
                    (vault_b / "2-atoms" / "cloud-rule.md").write_text(
                        atom_b_txt.replace("SigV4.", "SigV4 with R2 region auto."), encoding="utf-8"
                    )

                    # Auto-sync on Vault B pulls, detects conflicts, and pauses without pushing!
                    cyc_conflict = run_auto_sync_cycle(
                        brain_dir=vault_b,
                        state_dir=sec_b,
                        http_transport=mock_s3,
                        skip_dns_check_for_mock_transport=True,
                    )
                    self.assertEqual(cyc_conflict["status"], "paused_conflicts")
                    self.assertFalse(cyc_conflict["pushed"])
                    self.assertEqual(sync_status(brain_dir=vault_b, state_dir=sec_b)["conflict_count"], 2)

                    # Inspect 3-way conflict detail on shared-roadmap.md (verifies base_content + unified_diff!)
                    note_det = get_sync_conflict_detail("notes/shared-roadmap.md", brain_dir=vault_b)
                    self.assertIn("Base ancestor line.", str(note_det["base_content"]))
                    self.assertIn("-Device B milestone.", note_det["unified_diff"])
                    self.assertIn("+Device A milestone.", note_det["unified_diff"])

                    # Stale CAS sha256 MUST raise SyncConflictError!
                    with self.assertRaises(SyncConflictError):
                        resolve_sync_conflict(
                            "notes/shared-roadmap.md",
                            resolution="keep_local",
                            expected_local_sha256="0" * 64,
                            brain_dir=vault_b,
                        )

                    # Resolve note conflict via keep_both -> creates peer sibling note!
                    res_both = resolve_sync_conflict(
                        "notes/shared-roadmap.md",
                        resolution="keep_both",
                        expected_local_sha256=note_det["current_local_sha256"],
                        brain_dir=vault_b,
                    )
                    self.assertIsNotNone(res_both["sibling_rel_path"])
                    self.assertTrue((vault_b / str(res_both["sibling_rel_path"])).is_file())

                    # Resolve canonical atom conflict via manual_merge -> MUST demote unapproved merged text to pending!
                    atom_det = get_sync_conflict_detail("2-atoms/cloud-rule.md", brain_dir=vault_b)
                    self.assertTrue(atom_det["is_canonical_atom"])
                    merged_atom_md = atom_det["local_content"].replace(
                        "review_status: accepted", "review_status: accepted"
                    ).replace("SigV4 with R2 region auto.", "SigV4 with pinned TLS and R2 region auto.")
                    res_merge = resolve_sync_conflict(
                        atom_det["conflict_id"],
                        resolution="manual_merge",
                        merged_content=merged_atom_md,
                        expected_local_sha256=atom_det["current_local_sha256"],
                        brain_dir=vault_b,
                    )
                    self.assertTrue(res_merge["demoted_unapproved_canonical"])
                    self.assertEqual(res_merge["remaining_conflicts"], 0)
                    merged_disk = (vault_b / "2-atoms" / "cloud-rule.md").read_text(encoding="utf-8")
                    self.assertIn("review_status: pending", merged_disk)
                    self.assertFalse(
                        approved_revision_is_current(
                            str(vault_b / "2-atoms" / "cloud-rule.md"),
                            brain_dir=str(vault_b),
                            atoms_dir=str(vault_b / "2-atoms"),
                        )
                    )
                    # Prior canonical atom revision was backed up in 2-atoms/.history/!
                    self.assertTrue(len(list((vault_b / "2-atoms" / ".history").glob("cloud-rule.sync-*.md"))) >= 1)
                finally:
                    stop_all_auto_sync_schedulers()

        # 4.4 Static Zero-XSS & 3-Surface UI Parity Verification
        js_targets = [
            REPO_ROOT / "desktop" / "renderer" / "app.js",
            REPO_ROOT / "desktop" / "renderer" / "chat_state.js",
            REPO_ROOT / "extensions" / "browser-clipper" / "popup.js",
            REPO_ROOT / "extensions" / "browser-clipper" / "background.js",
            REPO_ROOT / "extensions" / "browser-clipper" / "lib" / "clipper-client.js",
            REPO_ROOT / "extensions" / "browser-clipper" / "lib" / "readability-markdown.js",
            REPO_ROOT / "extensions" / "vscode-second-brain" / "extension.js",
        ]
        for js_file in js_targets:
            src = js_file.read_text(encoding="utf-8")
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                self.assertNotIn(forbidden, src, f"Forbidden DOM sink {forbidden} found in {js_file}")

        desktop_html = (REPO_ROOT / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
        for dom_id in (
            "chatThreadSelect",
            "chatNewThreadBtn",
            "chatPinTurnBtn",
            "syncAuthModeSelect",
            "syncUseKeychainCheck",
            "syncAutoEnableBtn",
            "clipperPairCodeBtn",
            "graphAutoLinkBtn",
        ):
            self.assertIn(dom_id, desktop_html)

        vscode_pkg = (REPO_ROOT / "extensions" / "vscode-second-brain" / "package.json").read_text(encoding="utf-8")
        for cmd_id in (
            "secondBrain.newChatThread",
            "secondBrain.pinChatTurn",
            "secondBrain.autoLinkGraph",
            "secondBrain.resolveSyncConflicts",
        ):
            self.assertIn(cmd_id, vscode_pkg)

        dash_src = (REPO_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        for marker in (
            "Persistent Conversation Thread",
            "Auto-Link Graph Atoms",
            "s3_sigv4",
            "Interactive 3-Way Sync Conflict Resolver",
            "Generate 1-Click Pairing Code",
        ):
            self.assertIn(marker, dash_src)

        import second_brain

        ver = second_brain.__version__
        self.assertGreaterEqual(tuple(int(x) for x in ver.split(".")[:3]), (0, 5, 0))
        self.assertEqual(json.loads((REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))["version"], ver)
        self.assertEqual(json.loads(vscode_pkg)["version"], ver)
        self.assertEqual(
            json.loads((REPO_ROOT / "extensions" / "browser-clipper" / "manifest.json").read_text(encoding="utf-8"))["version"],
            ver,
        )
        _ = content_sha256


if __name__ == "__main__":
    unittest.main()
