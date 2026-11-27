import json
TEMPLATE_ID = "c08"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c08-0", "template_id": "c08", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function unique_keep(items) that returns the items with duplicates removed, preserving the first occurrence and the original order.", "entry_point": "unique_keep", "signature": "def unique_keep(items: list) -> list:", "visible_tests": "assert unique_keep([1, 2, 1, 3]) == [1, 2, 3]\\nassert unique_keep([]) == []\\n", "hidden_tests": "assert unique_keep([\\"a\\", \\"b\\", \\"a\\"]) == [\\"a\\", \\"b\\"]\\nassert unique_keep([1, 1, 1]) == [1]\\n", "reference_solution": "def unique_keep(items):\\n    seen = []\\n    for item in items:\\n        if item not in seen:\\n            seen.append(item)\\n    return seen\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["list"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
