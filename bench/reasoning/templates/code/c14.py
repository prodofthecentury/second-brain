import json
TEMPLATE_ID = "c14"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c14-0", "template_id": "c14", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function parse_version(text) that parses \'major.minor\' into a tuple of two non-negative integers. Raise ValueError for any other shape.", "entry_point": "parse_version", "signature": "def parse_version(text: str) -> tuple:", "visible_tests": "assert parse_version(\\"1.2\\") == (1, 2)\\nassert parse_version(\\"10.0\\") == (10, 0)\\n", "hidden_tests": "assert parse_version(\\"0.3\\") == (0, 3)\\nfor bad in [\\"1\\", \\"1.2.3\\", \\"a.b\\", \\"\\", \\"1.\\", \\".2\\"]:\\n    try:\\n        parse_version(bad)\\n    except ValueError:\\n        pass\\n    else:\\n        raise AssertionError(\\"expected ValueError for \\" + repr(bad))\\n", "reference_solution": "def parse_version(text: str):\\n    parts = text.split(\\".\\")\\n    if len(parts) != 2 or any(not part.isdigit() for part in parts):\\n        raise ValueError(text)\\n    return int(parts[0]), int(parts[1])\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["parsing"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
