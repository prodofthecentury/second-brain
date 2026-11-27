import json
TEMPLATE_ID = "c10"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c10-0", "template_id": "c10", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function invert_dict(mapping) that returns a new dict with keys and values swapped. Values are unique.", "entry_point": "invert_dict", "signature": "def invert_dict(mapping: dict) -> dict:", "visible_tests": "assert invert_dict({\\"a\\": 1, \\"b\\": 2}) == {1: \\"a\\", 2: \\"b\\"}\\nassert invert_dict({}) == {}\\n", "hidden_tests": "assert invert_dict({\\"x\\": \\"y\\"}) == {\\"y\\": \\"x\\"}\\n", "reference_solution": "def invert_dict(mapping):\\n    return {value: key for key, value in mapping.items()}\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["dict"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
