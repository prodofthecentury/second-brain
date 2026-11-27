import json
TEMPLATE_ID = "c13"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c13-0", "template_id": "c13", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function parse_positive_ints(text) that splits on commas and returns the positive integers. Raise ValueError for an empty token, a non-integer, zero, or a negative number.", "entry_point": "parse_positive_ints", "signature": "def parse_positive_ints(text: str) -> list[int]:", "visible_tests": "assert parse_positive_ints(\\"1, 2, 3\\") == [1, 2, 3]\\nassert parse_positive_ints(\\"4\\") == [4]\\n", "hidden_tests": "assert parse_positive_ints(\\"10,11\\") == [10, 11]\\nfor bad in [\\"\\", \\"1,,2\\", \\"x\\", \\"0\\", \\"-3\\", \\"1, 0\\"]:\\n    try:\\n        parse_positive_ints(bad)\\n    except ValueError:\\n        pass\\n    else:\\n        raise AssertionError(\\"expected ValueError for \\" + repr(bad))\\n", "reference_solution": "def parse_positive_ints(text: str):\\n    if not text.strip():\\n        raise ValueError(text)\\n    values = []\\n    for part in text.split(\\",\\"):\\n        token = part.strip()\\n        if not token or not token.isdigit() or int(token) <= 0:\\n            raise ValueError(text)\\n        values.append(int(token))\\n    return values\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["parsing"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
