import json
TEMPLATE_ID = "c03"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c03-0", "template_id": "c03", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function parse_duration(text) that converts a duration like \'1h30m\', \'45s\' or \'2h\' into total seconds. Units are h, m, s, each optional but in that order, case-insensitive, surrounding spaces ignored. Raise ValueError for an empty string, a unit with no number, unknown units, wrong order, or a negative sign.", "entry_point": "parse_duration", "signature": "def parse_duration(text: str) -> int:", "visible_tests": "assert parse_duration(\\"1h30m\\") == 5400\\nassert parse_duration(\\"45s\\") == 45\\n", "hidden_tests": "assert parse_duration(\\"2h\\") == 7200\\nassert parse_duration(\\"90m\\") == 5400\\nassert parse_duration(\\"1h0m5s\\") == 3605\\nassert parse_duration(\\" 3M \\") == 180\\nfor bad in [\\"\\", \\"h\\", \\"5x\\", \\"1m1h\\", \\"-5s\\"]:\\n    try:\\n        parse_duration(bad)\\n    except ValueError:\\n        pass\\n    else:\\n        raise AssertionError(\\"expected ValueError for \\" + repr(bad))\\n", "reference_solution": "import re\\ndef parse_duration(text: str) -> int:\\n    t = text.strip().lower()\\n    m = re.fullmatch(r\\"(?:(\\\\d+)h)?(?:(\\\\d+)m)?(?:(\\\\d+)s)?\\", t)\\n    if not t or not m or not any(m.groups()):\\n        raise ValueError(text)\\n    h, mi, s = (int(g) if g else 0 for g in m.groups())\\n    return h * 3600 + mi * 60 + s\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["parsing", "error-handling", "regex"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
