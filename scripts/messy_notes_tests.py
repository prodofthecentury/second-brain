"""Messy-note distillation and the shared capture policy."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.capture_policy import load_policy, policy_markdown, rejection_reasons
from scripts.ollama_refinery import draft_memory_atoms


def approved_claim(document: str) -> str | None:
    """Return the claim only when this drafted atom would pass approval."""
    from scripts.evidence import claim_entailed_by_evidence
    from scripts.memory_review import section_text

    if "## Claim\n" not in document or "## Evidence\n" not in document:
        return None
    body = document.split("---\n\n", 1)[-1]
    claim = section_text(body, "Claim")
    evidence = section_text(body, "Evidence")
    context = section_text(body, "Context")
    if not claim or not evidence:
        return None
    from scripts.evidence import fact_portion

    if rejection_reasons(fact_portion(claim, context)):
        return None
    if not claim_entailed_by_evidence(claim, evidence, context):
        return None
    return claim


def score_messy(by_note: dict[str, list[str]], facts: list[dict]) -> tuple[float, float]:
    approved: dict[str, list[str]] = {}
    for note, documents in by_note.items():
        approved[note] = [claim for document in documents if (claim := approved_claim(document))]
    hits = 0
    for fact in facts:
        claims = approved.get(str(fact["note"]), [])
        matched = any(
            all(token in claim for token in fact["must"])
            and not any(token in claim for token in fact.get("must_not") or [])
            for claim in claims
        )
        hits += int(matched)
    recall = hits / len(facts) if facts else 0.0
    atoms = [(note, claim) for note, claims in approved.items() for claim in claims]
    matched_atoms = 0
    for note, claim in atoms:
        if any(
            str(fact["note"]) == note
            and all(token in claim for token in fact["must"])
            and not any(token in claim for token in fact.get("must_not") or [])
            for fact in facts
        ):
            matched_atoms += 1
    precision = matched_atoms / len(atoms) if atoms else 0.0
    return recall, precision


def _fixture() -> dict:
    path = Path(__file__).resolve().parents[1] / "docs" / "messy-fixtures.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _distill_note(text: str, filename: str) -> list[str]:
    with patch(
        "scripts.ollama_refinery.chat_with_fallback",
        return_value={"message": {"content": '{ "atoms": [ ] }'}},
    ):
        return draft_memory_atoms(text, filename, "Copy evidence verbatim.")


class MessyFixtureTests(unittest.TestCase):
    def test_windows_path_does_not_crash_the_replacement(self) -> None:
        docs = _distill_note(
            "The nightly copy lands on D:\\BrainBackups.\n",
            "0-raw/backup.md",
        )
        self.assertTrue(any("D:\\BrainBackups" in doc for doc in docs))

    def test_abbreviations_stay_inside_one_sentence(self) -> None:
        from scripts.sentence_spans import split_sentences

        sentences = split_sentences("Saw Dr. Hale at 9:30 on 27 Nov 2026. J. R. Hale signed.")
        self.assertTrue(any("Dr. Hale" in sentence and "9:30" in sentence for sentence in sentences))
        self.assertFalse(any(sentence.startswith("Hale") for sentence in sentences))
        self.assertTrue(any("J. R. Hale" in sentence for sentence in sentences))

    def test_empty_model_output_covers_the_messy_ground_truth(self) -> None:
        fixture = _fixture()
        self.assertEqual(len(fixture["notes"]), 10)
        self.assertEqual(len(fixture["facts"]), 38)
        by_note: dict[str, list[str]] = {}
        for note in fixture["notes"]:
            by_note[note["id"]] = _distill_note(note["text"], note["filename"])
            for document in by_note[note["id"]]:
                evidence = document.split("## Evidence\n", 1)[1].split("\n##", 1)[0].strip()
                self.assertIn(evidence, note["text"])
                self.assertNotIn("I feel worn out", document)
                self.assertNotIn("The hint is the", document)
                self.assertNotIn("should grab", document)
        recall, precision = score_messy(by_note, fixture["facts"])
        self.assertEqual(recall, 1.0, by_note)
        self.assertGreaterEqual(precision, 0.95)

    def test_model_atoms_do_not_hide_uncovered_facts(self) -> None:
        source = "Daily backups run at 02:00. A 70B Hermes model needs a large GPU."
        recorded = (
            '{"atoms":[{"title":"Backups","claim":"Daily backups run at 02:00.",'
            '"evidence":"Daily backups run at 02:00.","why":"Ops.","memory_type":"semantic"}]}'
        )
        with patch("scripts.ollama_refinery.chat_with_fallback", return_value={"message": {"content": recorded}}):
            drafted = draft_memory_atoms(source, "0-raw/02.md", "rules")
        joined = "\n".join(drafted)
        self.assertIn("02:00", joined)
        self.assertIn("70B", joined)

    def test_chat_strips_invented_details_and_mid_name_citations(self) -> None:
        from scripts.streaming_chat import answer_with_citations

        verified = [
            {
                "id": "V1",
                "claim": "Saw Dr. Hale at 9:30 on 27 Nov 2026.",
                "evidence": "Saw Dr. Hale at 9:30 on 27 Nov 2026.",
                "title": "Dentist",
            },
            {
                "id": "V2",
                "claim": "coral-bay.local: bind 192.168.1.40",
                "evidence": "bind 192.168.1.40",
                "title": "coral-bay.local",
            },
        ]
        answer = answer_with_citations(
            "Dr. [V1] Hale is at 9:30. The host is 10.0.0.10 [V2]. The standup is on Thursday [V1]. [V1]",
            "When did we see Dr. Hale?",
            verified,
        )
        self.assertIn("Dr. Hale", answer)
        self.assertIn("9:30", answer)
        self.assertNotIn("Dr. [V1]", answer)
        self.assertNotIn("[V1] [V1]", answer)
        self.assertNotIn("10.0.0.10", answer)
        self.assertNotIn("Thursday", answer)
        self.assertEqual(answer.count("[V1]"), 1)

    def test_split_name_no_longer_hides_the_dentist_date(self) -> None:
        from scripts.streaming_chat import answer_with_citations

        verified = [{
            "id": "V1",
            "claim": "Saw Dr. Hale at 9:30 on 27 Nov 2026.",
            "evidence": "Saw Dr. Hale at 9:30 on 27 Nov 2026.",
            "title": "Dentist visit with Dr. Hale",
        }]
        answer = answer_with_citations(
            "There is no information about the dentist.",
            "When did we see Dr. Hale at the dentist?",
            verified,
        )
        self.assertIn("9:30", answer)
        self.assertIn("Dr. Hale", answer)
        self.assertNotIn("no information", answer.casefold())


class SyntheticCaptureTests(unittest.TestCase):
    """Unseen notes. These are not the laptop held-out set."""

    def test_new_topics_approve_facts_and_drop_mood_secrets_and_braces(self) -> None:
        note = "\n".join([
            "kiln log 4 Apr 2026",
            "Attendees: Nia, Omar, Tess.",
            "Next meeting Thu 16 Apr 2026.",
            "Rate limit 30/90 on the glaze API.",
            "- [ ] Nia: order feldspar by 16 Apr 2026.",
            "I keep forgetting the sponge.",
            "feeling restless and a bit flat today.",
            "I walked to the quay anyway and the wind helped a little.",
            "api key sk-test123",
            "The studio password is swordfish.",
            "host kiln-01 {",
            "fixed-address 10.4.4.8",
            "}",
            "Visited Ft. Bragg on 2 Mar 2026.",
            "| Glaze | Cone |",
            "| --- | --- |",
            "| Shino | 10 |",
            "The shelf fee was $40, plot 9 is Nia's, and the library hold expires 3 May 2026.",
            "Pick up the feldspar should grab.",
            "Tried the 12V fan. Too loud, ~40 dB at one metre.",
        ])
        docs = _distill_note(note, "0-raw/kiln-log.md")
        claims = [approved_claim(document) for document in docs]
        claims = [claim for claim in claims if claim]
        blob = "\n".join(claims)
        self.assertIn("10.4.4.8", blob)
        self.assertIn("Ft. Bragg", blob)
        self.assertIn("4 Apr 2026", blob)
        self.assertIn("Shino", blob)
        self.assertIn("feldspar", blob)
        self.assertIn("$40", blob)
        self.assertIn("plot 9", blob)
        self.assertIn("3 May 2026", blob)
        self.assertIn("12V", blob)
        self.assertIn("40 dB", blob)
        self.assertNotIn("sk-test123", blob)
        self.assertNotIn("swordfish", blob)
        self.assertNotIn("restless", blob.casefold())
        self.assertNotIn("forgetting", blob.casefold())
        self.assertNotIn("quay", blob.casefold())
        self.assertFalse(any(claim.rstrip().endswith("{") for claim in claims))
        self.assertFalse(any(claim.strip().startswith("|") for claim in claims))
        self.assertNotIn("should grab", blob)

    def test_mt_style_abbreviation_stays_one_sentence(self) -> None:
        from scripts.sentence_spans import split_sentences

        sentences = split_sentences("The hike reached Ft. Bragg before noon. Tess signed the log. The trail crossed Mt. Rowan before dusk. OK. The kiln was cool.")
        self.assertTrue(any("Ft. Bragg" in sentence and "noon" in sentence for sentence in sentences))
        self.assertTrue(any("Mt. Rowan" in sentence and "dusk" in sentence for sentence in sentences))
        self.assertFalse(any(sentence.strip().startswith("Bragg") for sentence in sentences))
        self.assertTrue(any(sentence.strip() == "OK." or sentence.strip().startswith("OK.") for sentence in sentences))
        self.assertTrue(any(sentence.strip().startswith("The kiln") for sentence in sentences))

    def test_chat_drops_an_uncited_address_and_keeps_a_normalised_date(self) -> None:
        from scripts.streaming_chat import answer_with_citations

        verified = [{
            "id": "V1",
            "claim": "The firing review is on 3 Apr 2026 with Tess.",
            "evidence": "The firing review is on 3 Apr 2026 with Tess.",
            "title": "Firing",
        }]
        answer = answer_with_citations(
            "The review is the 3rd of April 2026 with Tess [V1]. The kiln controller is at 10.9.9.9.",
            "When is the firing review?",
            verified,
        )
        self.assertIn("Tess", answer)
        self.assertIn("2026", answer)
        self.assertNotIn("10.9.9.9", answer)


class CapturePolicyTests(unittest.TestCase):
    def test_policy_files_agree(self) -> None:
        policy = load_policy()
        markdown = policy_markdown()
        self.assertEqual(policy["max_claim_words"], 80)
        self.assertIn("80 words", markdown)
        self.assertIn("verbatim", markdown.casefold())
        self.assertIn("tentative", markdown.casefold())
        self.assertIn(policy["examples"]["good"][0], markdown)
        self.assertIn(policy["examples"]["bad"][0], markdown)
        self.assertEqual(rejection_reasons(policy["examples"]["good"][0]), [])
        for bad in policy["examples"]["bad"]:
            self.assertTrue(rejection_reasons(bad), bad)

    def test_bad_submissions_name_the_reason_and_a_good_one_passes(self) -> None:
        self.assertTrue(any(reason.startswith("mood:") for reason in rejection_reasons("I feel worn out by backups.")))
        self.assertTrue(any(reason.startswith("secret:") for reason in rejection_reasons("password: hunter2")))
        self.assertTrue(any(reason.startswith("fragment:") for reason in rejection_reasons("Rotation to.")))
        self.assertTrue(any(reason.startswith("relative-date:") for reason in rejection_reasons("Ship the beta tomorrow.")))
        self.assertEqual(rejection_reasons("The dashboard listens on port 8501."), [])

    def test_mcp_exposes_the_policy_and_rejects_a_bad_remember(self) -> None:
        import scripts.config as config
        import scripts.mcp_server as mcp_server
        import scripts.notes_store as notes_store
        from mcp import types

        token = "capture-policy-token-123456"
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            for folder in ("0-raw", "0-raw/inbox", "0-raw/archive", "2-atoms", "notes", ".sidecar"):
                (brain / folder).mkdir(parents=True, exist_ok=True)
            note_paths = notes_store.NotePaths.for_brain(str(brain))
            env = {
                "BRAIN_VAULT_DIR": str(brain),
                "BRAIN_MCP_BEARER_TOKEN": token,
            }
            meta = {"authorization": f"Bearer {token}"}

            async def scenario() -> None:
                with patch.dict(os.environ, env, clear=False):
                    with patch.object(mcp_server, "BRAIN_DIR", str(brain)), \
                            patch.object(mcp_server, "RAW_DIR", str(brain / "0-raw")), \
                            patch.object(mcp_server, "INBOX_DIR", str(brain / "0-raw" / "inbox")), \
                            patch.object(mcp_server, "ATOMS_DIR", str(brain / "2-atoms")), \
                            patch.object(mcp_server, "MCP_AUDIT_PATH", str(brain / "0-raw" / "archive" / "mcp-audit.jsonl")), \
                            patch.object(config, "BRAIN_DIR", str(brain)), \
                            patch.object(config, "NOTES_DIR", str(brain / "notes")), \
                            patch.object(notes_store, "DEFAULT_PATHS", note_paths):
                        params = types.ReadResourceRequestParams(uri="brain://policy/capture", _meta=meta)
                        listed = await mcp_server.handle_list_resources(None, params)
                        self.assertEqual([str(item.uri) for item in listed.resources], ["brain://policy/capture"])
                        read = await mcp_server.handle_read_resource(None, params)
                        self.assertIn("What qualifies", read.contents[0].text)
                        bad = await mcp_server.handle_call_tool(
                            None,
                            types.CallToolRequestParams(
                                name="remember",
                                arguments={"content": "I feel worn out by backups.", "title": "Mood"},
                                _meta=meta,
                            ),
                        )
                        self.assertTrue(bad.is_error)
                        self.assertIn("mood:", bad.content[0].text)
                        good = await mcp_server.handle_call_tool(
                            None,
                            types.CallToolRequestParams(
                                name="add_to_inbox",
                                arguments={"content": "The dashboard listens on port 8501."},
                                _meta=meta,
                            ),
                        )
                        self.assertFalse(good.is_error)
                        self.assertIn("quarantined inbox", good.content[0].text)

            asyncio.run(scenario())
