import json
TEMPLATE_ID = "c15"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c15-0", "template_id": "c15", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function first_int(text) that returns the first non-negative integer in text, reading consecutive digits. Raise ValueError when text contains no digit.", "entry_point": "first_int", "signature": "def first_int(text: str) -> int:", "visible_tests": "assert first_int(\\"id 42 ok\\") == 42\\nassert first_int(\\"7\\") == 7\\n", "hidden_tests": "assert first_int(\\"room 108b\\") == 108\\ntry:\\n    first_int(\\"none\\")\\nexcept ValueError:\\n    pass\\nelse:\\n    raise AssertionError(\\"expected ValueError\\")\\n", "reference_solution": "def first_int(text: str) -> int:\\n    digits = []\\n    started = False\\n    for ch in text:\\n        if ch.isdigit():\\n            digits.append(ch)\\n            started = True\\n        elif started:\\n            break\\n    if not digits:\\n        raise ValueError(text)\\n    return int(\\"\\".join(digits))\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["parsing"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
