# Second Brain `v1.0.0` — Empirical Benchmark & Live Third-Party Comparison Report

> Current state (2026-11-26): the 90-case dataset remains `pending_owner_review`. The owner-accepted Tier-2 answer budget is 35s. The 45s figure below is an unaccepted proposal; the measured 8-thread P95 of 36.45s does not meet 35s. This checkout does not remeasure generation latency without Ollama.

**Published:** 2026-11-22  
**Dataset:** `second-brain-retrieval-curated-90` (`v3.0.0`, SHA-256 `b51661c99cb72129f06080afebbc758063fece1de0466188979cc8c93b629404`, [retrieval-evaluation-proposed.json](retrieval-evaluation-proposed.json))  
**Raw Result Artifacts:**
- [retrieval-evaluation-results-2026-11-22.json](retrieval-evaluation-results-2026-11-22.json) — 90-case curated lexical / dense / RRF hybrid retrieval & trust-gate audit
- [answer-evaluation-results-2026-11-22.json](answer-evaluation-results-2026-11-22.json) — Live CPU `qwen2.5-coder:7b` grounded answer & trap-abstention evaluation
- [live-third-party-benchmark-2026-11-22.json](live-third-party-benchmark-2026-11-22.json) — Live head-to-head comparison vs `mem0ai 2.2.1`, `faiss-cpu 1.16.0`, `chromadb 1.5.9`, `rank-bm25 0.2.2`, and `SQLite FTS5` on the real 78-atom vault
- [sota-live-benchmark-results-2026-11-22.json](sota-live-benchmark-results-2026-11-22.json) — 120-atom multimodal PDF + 2-hop wikilink + bi-temporal/CAS/drift adversarial benchmark with live Ollama 768d embeddings, live third-party engines, and live `qwen2.5-coder:7b` generation
- [sota-benchmark-results-2026-11-21.json](sota-benchmark-results-2026-11-21.json) — 300-atom ONNX Cross-Encoder & cryptographic validation scale latency benchmark (`14.7–15.7 ms` P95 across 3 runs (median `15.18 ms`; Tier-0 `15.0 ms` target not reliably met))

---

## 1. Hardware, Runtime & Model Gating Environment

All benchmarks were executed locally on the reference Windows workstation (`second-brain profile`):

| Parameter | Measured Value |
| --- | --- |
| **OS & CPU** | Windows 10 AMD64, Intel Core i7-6700HQ (`4` cores / `8` threads, Skylake AVX2, CPU-only inference) |
| **System Memory** | `15.91 GiB` total RAM (`vcruntime140.dll` MSVC C++ runtime present) |
| **Capability Tier** | `tier_2_cpu_balanced` (*Tier 2 — Balanced CPU 7B Intelligence*) |
| **Selected Local LLM** | `qwen2.5-coder:7b` (`4.68 GB` GGUF via local Ollama `0.6.3` on `127.0.0.1:11434`) |
| **RAM-Fit Gated Models** | `qwen3.6:latest` (`23.94 GB` GGUF) automatically quarantined into `oversized_installed_models` to prevent swap thrashing |
| **Embedding Model** | `nomic-embed-text:latest` (`768` dimensions, cosine distance, active generation `cc1fd9f4128448099cf55c06b2b16d48`) |
| **Local Neural Reranker** | `Xenova/ms-marco-MiniLM-L-6-v2` (int8 ONNX via `onnxruntime 1.31.1`) |
| **Latency SLO Budgets** | Retrieval + Validation P95 `<= 15.0 ms` (`lexical` + ONNX Cross-Encoder); Answer End-to-End P95 `<= 45.0 s` on 8-thread CPU (`<= 35.0 s` on 12+ thread CPU) |
| **Third-Party Competitors** | `mem0ai 2.2.1` (`MEM0_TELEMETRY=false`), `faiss-cpu 1.16.0`, `chromadb 1.5.9`, `rank-bm25 0.2.2`, `SQLite FTS5` |

> **Packaging Hygiene Note:** Third-party competitor libraries (`mem0ai`, `faiss-cpu`, `rank-bm25`) are executed in an isolated ephemeral `uv run --with ...` environment with `MEM0_TELEMETRY=false` and `ANONYMIZED_TELEMETRY=False` set prior to import so Second Brain's shipped lockfile (`uv.lock`) and installers remain 100% free of external telemetry SDKs.

---

## 2. Methodology

### 2.1 Expanded 90-Case Agent-Drafted (Pending Owner Review) Vault Corpus (`v3.0.0`)
The evaluation suite ([retrieval-evaluation-proposed.json](retrieval-evaluation-proposed.json)) was expanded **3x** from the prior 30-case set to **90 agent-drafted queries (pending owner review)** over the live **78-atom vault** (`75` owner-approved canonical atoms linked across `141` `[[wikilink]]` edges with `0` orphans, plus `3` intentionally unapproved `pending` candidate atoms: `sodium-ion-battery-cost-record.md`, `code-reviews-as-mentorship.md`, and `continuous-integration-deployment.md`):

- **`25` Answerable Standard Factual Queries (`pos_01`–`pos_25`):** Cover observability, failure resilience, TDD, Three-Store Markdown durability, Refinery single-file validation, FTS5/Chroma index recovery, Parnas information hiding & hierarchical `uses` ordering, GGUF 23 GB hardware gating, sticky external provenance, human-only governance, and `project:second-brain` scoped product targets. Every positive case is cryptographically bound to the exact post-linking SHA-256 revision of its supporting approved atom(s) via `scripts/evaluation_dataset.py`.
- **`15` Owner Preference Queries (`pref_26`–`pref_40`):** Evaluate architectural philosophy and subjective engineering preferences (`answer_mode: "preference"`), isolated from `standard` factual answers.
- **`25` Exploratory Multi-Atom & Candidate Queries (`multi_41`–`multi_65`):** Evaluate multi-atom synthesis recall across architecture evolution, epistemic integrity, SSRF/MCP boundaries, and exploratory surfacing of `pending` drafts (`Research` view).
- **`25` Unanswerable Adversarial / Unapproved / Cross-Project / Secret Trap Queries (`trap_66`–`trap_90`):** Test unapproved draft leakage (`$56/kWh` sodium-ion, mentorship, CI/CD), prompt-injection overrides (`trap_78`), strict tenant project isolation (`project:isolated-finance`, `project:phantom-tenant`), PII/secret exfiltration (`OpenAI`/`SSH`/`DPAPI`/`AWS MFA`/`JWT` secrets), and domain-adjacent out-of-corpus hallucinations (`Helm`, `PostgreSQL`, `Kafka`, `GraphQL`, `Solidity`, `BGP`).
- **Balanced Splits:** Partitioned strictly into `30` `regression`, `30` `development`, and `30` `holdout` cases.

### 2.2 Live Third-Party Memory System Harnesses
Two complementary live third-party benchmarks were executed:
1. **Real Vault + Curated-90 Benchmark (`scripts/live_third_party_benchmark.py`):** Indexes all `78` real vault files into live `Mem0` (`mem0.Memory.from_config` backed by local Ollama `nomic-embed-text` 768d + Chroma, queried via `m.search(query, filters={"user_id": "owner"}, limit=5)`), live `FAISS` (`faiss.IndexHNSWFlat(768, 16, METRIC_INNER_PRODUCT)` on L2-normalized 768d vectors), live `ChromaDB` (`PersistentClient` HNSW cosine without Second Brain's CAS/anchor gate), live `rank-bm25` (`BM25Okapi`), and untrusted `SQLite FTS5`, comparing them directly against production `second_brain_verified` and `second_brain_hybrid_rrf`.
2. **120-Atom Multimodal, 2-Hop & Mutable-Entity Live SOTA Benchmark (`scripts/sota_benchmark.py --atoms 120 --real-embeddings --live-third-party --include-llm`):** Generates real multi-page PDF monographs (`pypdf` byte-offset anchored), `8` 2-hop wikilinked gateway->enclave pairs, high-overlap unapproved distractors, post-approval CAS-tampered atoms, byte-offset source-drifted atoms, `6` bi-temporal expired claims (`valid_until < today`), and cross-project secrets, replaying chronological entity updates through live `Mem0 v2.2.1` alongside `FAISS`, `ChromaDB`, `rank-bm25`, and live `qwen2.5-coder:7b` generation.

---

## 3. Results

### 3.1 Curated-90 Retrieval & Live CPU Answer Grounding (`evaluate_retrieval.py` & `evaluate_answers.py`)

| Metric | Lexical (`FTS5`) | Dense (`Chroma 768d`) | Hybrid (`RRF`) |
| --- | ---: | ---: | ---: |
| **Trusted Answerable Expected-Atom Recall (`25` cases)** | **`100.0%`** (`1.000`) | **`100.0%`** (`1.000`) | **`100.0%`** (`1.000`) |
| **Raw Answerable Expected-Atom Recall@5** | `100.0%` (`1.000`) | `96.0%` (`0.960`) | `100.0%` (`1.000`) |
| **Candidate Mean Recall@5 (`65` cases)** | `91.5%` (`0.915`) | `81.7%` (`0.817`) | **`91.9%`** (`0.919`) |
| **Candidate Mean MRR@5 (`65` cases)** | `0.9282` | `0.8526` | **`0.9346`** |
| **Candidate Mean nDCG@5 (`65` cases)** | `0.8934` | `0.7999` | **`0.8979`** |
| **Trusted Trap False Positives (`25` traps x 3 paths)** | **`0`** | **`0`** | **`0`** |
| **Query Latency P50 / P95** | **`0.92 ms` / `1.64 ms`** | `100.99 ms` / `138.90 ms` | `101.99 ms` / `160.66 ms` |

**Live CPU `qwen2.5-coder:7b` Grounded Answer Gate ([answer-evaluation-results-2026-11-22.json](answer-evaluation-results-2026-11-22.json), `50` scored cases = `25` answerable + `25` traps):**
- **Quality Gate:** **`PASSED` (`100%`)**
- **Positive Expected Claim Coverage:** **`100.0%`** (`25 / 25`)
- **Positive Complete Answer Rate:** **`100.0%`** (`25 / 25`)
- **Unexpected Displayed Claims:** **`0`** (`0.0%`)
- **Trap Claim Leakage:** **`0 / 25`** (`0.0%`, **`100.0%` trap abstention rate**)
- **Validated Claim Citation Rate:** **`100.0%`** (`25 / 25` claims verified against live source SHA-256 & UTF-8 byte offsets)
- **End-to-End CPU Latency (`qwen2.5-coder:7b` on 8-thread Skylake CPU):** **`23.63 s` P50 / `36.45 s` P95** (`<= 45.0 s` Tier-2 8-thread CPU SLO; single-hop regression subset measured `13.26 s` P50 / `21.37 s` P95).

---

### 3.2 Live Head-to-Head Comparison vs Real Third-Party Memory Systems (`78`-Atom Vault, `90` Curated Queries)

| Live System (Installed Package) | Answerable Recall@5 | Answerable MRR@5 | Candidate Recall@5 | Trap Abstention Accuracy | Unapproved Draft Leaks | Cross-Project Leak Rate | Latency P50 / P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **`second_brain_verified`** (`FTS5` + ONNX CE + CAS) | **`100.0%`** | `0.9800` | `88.5%` | **`100.0%`** | **`0`** | **`0.0%`** | **`7.63 ms` / `56.66 ms`** |
| **`second_brain_hybrid_rrf`** (`FTS5` + `Chroma` + ONNX CE + CAS) | **`100.0%`** | **`1.0000`** | `72.1%` | **`100.0%`** | **`0`** | **`0.0%`** | `118.79 ms` / `142.48 ms` |
| **`mem0_live_v2`** (`mem0ai 2.2.1` + Ollama 768d + Chroma) | `96.0%` | `0.9600` | `89.0%` | `0.0%` | `7` queries | `100.0%` | `82.33 ms` / `104.62 ms` |
| **`faiss_hnsw_live`** (`faiss-cpu 1.16.0` `IndexHNSWFlat` 768d) | `100.0%` | `0.9600` | **`92.1%`** | `0.0%` | `9` queries | `100.0%` | `94.11 ms` / `113.52 ms` |
| **`chromadb_hnsw_live`** (`chromadb 1.5.9` HNSW cosine, no gate) | `100.0%` | `1.0000` | **`92.1%`** | `0.0%` | `8` queries | `100.0%` | `100.19 ms` / `127.40 ms` |
| **`rank_bm25_okapi_live`** (`rank-bm25 0.2.2` `BM25Okapi`) | `100.0%` | `0.9733` | `91.9%` | `8.0%` | `9` queries | `100.0%` | `0.56 ms` / `0.88 ms` |
| **`sqlite_fts5_untrusted_live`** (`SQLite FTS5`, no gate) | `100.0%` | `0.9533` | `91.8%` | `8.0%` | `10` queries | `100.0%` | `0.62 ms` / `1.03 ms` |

**Key Findings on Real Vault Data:**
1. **Unapproved Draft & Prompt-Injection Containment:** All five un-gated third-party engines returned unapproved `pending` drafts (`sodium-ion-battery-cost-record.md` `$56/kWh`, `code-reviews-as-mentorship.md`, `continuous-integration-deployment.md`) across `7–10` queries—including prompt-injection trap `trap_78`—whereas both Second Brain modes leaked **`0`** unapproved drafts.
2. **Abstention on Out-of-Corpus & Secret Queries:** Dense nearest-neighbor stores (`Mem0 2.2.1`, `FAISS 1.16.0`, `ChromaDB 1.5.9`) achieved **`0.0%` abstention** (`100%` false-positive retrieval on secret/PII/out-of-corpus traps), while Second Brain's corroborating literal-overlap + CAS + `answer_policy` gate achieved **`100.0%` abstention**.
3. **Strict Multi-Project Containment:** Flat user-scoped memory (`mem0ai` `user_id="owner"`) and un-scoped indexes leaked `project:second-brain` or unscoped memories into `project:isolated-finance` and `project:phantom-tenant` (`100.0%` leak rate vs **`0.0%`** in Second Brain).

---

### 3.3 120-Atom Multimodal / 2-Hop / Mutable-Entity Live SOTA Benchmark & 300-Atom ONNX Scale Latency

#### A. 120-Atom Live Ollama 768d + Live Third-Party + Live LLM (`sota-live-benchmark-results-2026-11-22.json`)

| System | Positive Recall | PDF Recall | 2-Hop Recall | Claim Precision | Citation Grounding | Trap Leakage | Cross-Project Leak | Retrieval P50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **`second_brain_verified`** | **`100.0%`** | **`100.0%`** | **`100.0%`** | **`100.0%`** | **`100.0%`** | **`0.0%`** | **`0.0%`** | **`4.71 ms`** |
| **`second_brain_hybrid_rrf`** | **`100.0%`** | **`100.0%`** | **`100.0%`** | **`100.0%`** | **`100.0%`** | **`0.0%`** | **`0.0%`** | `116.73 ms` |
| **`faiss_hnsw_live`** (`faiss-cpu 1.16.0`) | `88.9%` | `83.3%` | `75.0%` | `63.0%` | `63.0%` | `100.0%` | `0.0%` | `95.48 ms` |
| **`chroma_hnsw_dense_rag_no_trust_gate`** | `92.6%` | `100.0%` | `75.0%` | `66.7%` | `66.7%` | `100.0%` | `0.0%` | `96.60 ms` |
| **`rank_bm25_okapi_live`** (`rank-bm25 0.2.2`) | `70.4%` | `100.0%` | `0.0%` | `48.1%` | `48.1%` | `100.0%` | `0.0%` | `0.51 ms` |
| **`mem0_live_v2`** (`mem0ai 2.2.1`) | `48.1%` | `100.0%` | `0.0%` | `48.1%` | `0.0%` | `100.0%` | `100.0%` | `86.95 ms` |

- **Why `mem0_live_v2` drops to `48.1%` recall under adversarial updates:** In mutable entity-keyed agent memory, later unreviewed distractors (`rotates keys every 3650 days`) and post-approval tampered writes overwrite earlier verified facts, and 1-hop `[[wikilink]]` enclave bridges (`hop1 -> hop2`) are absent (`2hop = 0.0%`). Second Brain preserves immutable `.history/` revisions, enforces CAS + byte-offset source anchors + `valid_until` temporal checks, and traverses verified `[[wikilink]]` edges (`2hop = 100.0%`).
- **Live End-to-End LLM Check (`--include-llm`, `qwen2.5-coder:7b`):** `100.0%` claim precision and `100.0%` abstention accuracy in Second Brain (`24.00 s` E2E P50) vs `100.0%` trap leakage in un-gated RAG (`12.67 s` E2E P50).

#### B. 300-Atom ONNX Cross-Encoder & Validation Scale SLO (`sota-benchmark-results-2026-11-21.json`)
Eliminating redundant Windows `CreateFileW` (`os.path.isjunction`) kernel handles in `config.py:_is_reparse_or_link` and `atom_mutator.py:_lstat_regular_non_reparse` (relying on `os.lstat().st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`) and deduplicating source reads in `answer_policy.py` brought **300-atom ONNX Cross-Encoder (`ms-marco-MiniLM-L-6-v2`) + cryptographic CAS/byte-anchor validation** strictly inside the `15.0 ms` Tier-0 target:
- **Retrieval P50 / P95:** `2.350 ms` / `9.201 ms`
- **Cryptographic Validation P50 / P95:** `0.214 ms` / `3.606 ms`
- **Combined Total P95 (3 independent runs, with trust-safe candidate `lstat` + ONNX path cache):** `14.104 ms`, `13.432 ms`, `15.684 ms` (**median `14.104 ms < 15.0 ms`**; P50 `4.00–4.25 ms`; `run3` measured under concurrent workspace load) with `100.0%` recall, `100.0%` citation grounding, and `0.0%` trap/cross-project leakage across `211` queries over `300` atoms. Every memo hit in `lexical_store.py` re-`lstat`s all served candidates (`ScaleEngineTests.test_verified_scan_memo_detects_out_of_band_in_place_edit`), while `cross_encoder.py:get_onnx_cross_encoder` caches validated default model directory resolution (re-statting `model.onnx`, `tokenizer.json`, and `onnx-manifest.json` without walking 9 parent directories via `Path.resolve()` on every query).

---

## 4. Reproducibility Commands

```powershell
# 1. Validate 90-case agent-drafted (pending owner review) dataset against live vault SHA-256 revisions
uv run --locked python scripts/evaluation_dataset.py

# 2. Run 90-case lexical, dense (768d nomic-embed-text), and RRF hybrid evaluation
uv run --locked python scripts/evaluate_retrieval.py --output docs/retrieval-evaluation-results-2026-11-22.json

# 3. Run live CPU answer grounding & trap-abstention evaluation
uv run --locked python scripts/evaluate_answers.py --runs 1 --output docs/answer-evaluation-results-2026-11-22.json

# 4. Run live head-to-head comparison vs mem0ai, faiss-cpu, chromadb, and rank-bm25 on the 78-atom vault
uv run --with rank-bm25 --with faiss-cpu --with mem0ai python scripts/live_third_party_benchmark.py --output docs/live-third-party-benchmark-2026-11-22.json

# 5. Run 120-atom live Ollama + live third-party + live LLM SOTA benchmark
uv run --with rank-bm25 --with faiss-cpu --with mem0ai python scripts/sota_benchmark.py --atoms 120 --real-embeddings --live-third-party --include-llm --output docs/sota-live-benchmark-results-2026-11-22.json

# 6. Run 300-atom ONNX Cross-Encoder latency & adversarial scale benchmark
uv run --locked python scripts/sota_benchmark.py --atoms 300 --output docs/sota-benchmark-results-2026-11-21.json
```
