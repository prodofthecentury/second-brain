import json
TEMPLATE_ID = "c04"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c04-0", "template_id": "c04", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function count_vowels(text) that returns how many vowels aeiou, case-insensitive, occur in text.", "entry_point": "count_vowels", "signature": "def count_vowels(text: str) -> int:", "visible_tests": "assert count_vowels(\\"Apple\\") == 2\\nassert count_vowels(\\"\\") == 0\\n", "hidden_tests": "assert count_vowels(\\"rhythm\\") == 0\\nassert count_vowels(\\"AEIOU\\") == 5\\nassert count_vowels(\\"Queue\\") == 4\\n", "reference_solution": "def count_vowels(text: str) -> int:\\n    return sum(1 for ch in text.lower() if ch in \\"aeiou\\")\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["string"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
