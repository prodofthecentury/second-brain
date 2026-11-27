import json
TEMPLATE_ID = "c09"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c09-0", "template_id": "c09", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function flatten_once(rows) that concatenates a list of lists into one list, in order. It does not flatten deeper than one level.", "entry_point": "flatten_once", "signature": "def flatten_once(rows: list) -> list:", "visible_tests": "assert flatten_once([[1, 2], [3]]) == [1, 2, 3]\\nassert flatten_once([]) == []\\n", "hidden_tests": "assert flatten_once([[], [4], []]) == [4]\\nassert flatten_once([[[1]], [2]]) == [[1], 2]\\n", "reference_solution": "def flatten_once(rows):\\n    out = []\\n    for row in rows:\\n        out.extend(row)\\n    return out\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["list"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
