import json
TEMPLATE_ID = "c11"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c11-0", "template_id": "c11", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function group_lengths(words) that returns a dict mapping each word length to the words of that length, in input order.", "entry_point": "group_lengths", "signature": "def group_lengths(words: list[str]) -> dict:", "visible_tests": "assert group_lengths([\\"a\\", \\"bb\\", \\"c\\"]) == {1: [\\"a\\", \\"c\\"], 2: [\\"bb\\"]}\\nassert group_lengths([]) == {}\\n", "hidden_tests": "assert group_lengths([\\"cat\\", \\"dog\\"]) == {3: [\\"cat\\", \\"dog\\"]}\\n", "reference_solution": "def group_lengths(words):\\n    grouped = {}\\n    for word in words:\\n        grouped.setdefault(len(word), []).append(word)\\n    return grouped\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["list"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
