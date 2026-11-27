import json
TEMPLATE_ID = "c05"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c05-0", "template_id": "c05", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function reverse_words(text) that reverses the order of whitespace-separated words and keeps a single space between them.", "entry_point": "reverse_words", "signature": "def reverse_words(text: str) -> str:", "visible_tests": "assert reverse_words(\\"one two three\\") == \\"three two one\\"\\nassert reverse_words(\\"solo\\") == \\"solo\\"\\n", "hidden_tests": "assert reverse_words(\\"\\") == \\"\\"\\nassert reverse_words(\\"a  b\\") == \\"b a\\"\\n", "reference_solution": "def reverse_words(text: str) -> str:\\n    return \\" \\".join(reversed(text.split()))\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["string"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
