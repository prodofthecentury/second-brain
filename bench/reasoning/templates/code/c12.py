import json
TEMPLATE_ID = "c12"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c12-0", "template_id": "c12", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function parse_pair(text) that parses \'key=value\' into a tuple (key, value) after stripping spaces. Raise ValueError when there is not exactly one equals sign or either side is empty.", "entry_point": "parse_pair", "signature": "def parse_pair(text: str) -> tuple:", "visible_tests": "assert parse_pair(\\"a=b\\") == (\\"a\\", \\"b\\")\\nassert parse_pair(\\" name = ada \\") == (\\"name\\", \\"ada\\")\\n", "hidden_tests": "assert parse_pair(\\"x=1\\") == (\\"x\\", \\"1\\")\\nfor bad in [\\"\\", \\"a\\", \\"a=\\", \\"=b\\", \\"a=b=c\\"]:\\n    try:\\n        parse_pair(bad)\\n    except ValueError:\\n        pass\\n    else:\\n        raise AssertionError(\\"expected ValueError for \\" + repr(bad))\\n", "reference_solution": "def parse_pair(text: str):\\n    if text.count(\\"=\\") != 1:\\n        raise ValueError(text)\\n    key, value = text.split(\\"=\\")\\n    key, value = key.strip(), value.strip()\\n    if not key or not value:\\n        raise ValueError(text)\\n    return key, value\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["parsing"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
