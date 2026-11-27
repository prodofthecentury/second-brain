import json
TEMPLATE_ID = "c06"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c06-0", "template_id": "c06", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function shift_chars(text, n) that rotates each letter by n places, wrapping within a-z or A-Z, and leaves other characters unchanged. n may be negative.", "entry_point": "shift_chars", "signature": "def shift_chars(text: str, n: int) -> str:", "visible_tests": "assert shift_chars(\\"ab\\", 1) == \\"bc\\"\\nassert shift_chars(\\"Z\\", 1) == \\"A\\"\\n", "hidden_tests": "assert shift_chars(\\"Hello!\\", 1) == \\"Ifmmp!\\"\\nassert shift_chars(\\"bc\\", -1) == \\"ab\\"\\nassert shift_chars(\\"\\", 5) == \\"\\"\\n", "reference_solution": "def shift_chars(text: str, n: int) -> str:\\n    out = []\\n    for ch in text:\\n        if \\"a\\" <= ch <= \\"z\\":\\n            out.append(chr((ord(ch) - 97 + n) % 26 + 97))\\n        elif \\"A\\" <= ch <= \\"Z\\":\\n            out.append(chr((ord(ch) - 65 + n) % 26 + 65))\\n        else:\\n            out.append(ch)\\n    return \\"\\".join(out)\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["string"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
