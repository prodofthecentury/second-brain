import json
TEMPLATE_ID = "c01"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c01-0", "template_id": "c01", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function run_length_encode(s) that returns the run-length encoding of s: each maximal run of the same character becomes the character followed by the run length. The empty string encodes to the empty string. Case matters.", "entry_point": "run_length_encode", "signature": "def run_length_encode(s: str) -> str:", "visible_tests": "assert run_length_encode(\\"aaabcc\\") == \\"a3b1c2\\"\\nassert run_length_encode(\\"x\\") == \\"x1\\"\\n", "hidden_tests": "assert run_length_encode(\\"\\") == \\"\\"\\nassert run_length_encode(\\"abab\\") == \\"a1b1a1b1\\"\\nassert run_length_encode(\\"zzzzzzzzzzzz\\") == \\"z12\\"\\nassert run_length_encode(\\"AAa\\") == \\"A2a1\\"\\nassert run_length_encode(\\"  !\\") == \\" 2!1\\"\\n", "reference_solution": "def run_length_encode(s: str) -> str:\\n    out = []\\n    i = 0\\n    while i < len(s):\\n        j = i\\n        while j < len(s) and s[j] == s[i]:\\n            j += 1\\n        out.append(s[i] + str(j - i))\\n        i = j\\n    return \\"\\".join(out)\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["string"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
