import json
TEMPLATE_ID = "c18"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c18-0", "template_id": "c18", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function balanced(text) that returns True when round parentheses are balanced and properly nested. Other characters are ignored.", "entry_point": "balanced", "signature": "def balanced(text: str) -> bool:", "visible_tests": "assert balanced(\\"(a(b)c)\\") is True\\nassert balanced(\\")(\\") is False\\n", "hidden_tests": "assert balanced(\\"\\") is True\\nassert balanced(\\"(()\\") is False\\nassert balanced(\\"no parens\\") is True\\n", "reference_solution": "def balanced(text: str) -> bool:\\n    depth = 0\\n    for ch in text:\\n        if ch == \\"(\\":\\n            depth += 1\\n        elif ch == \\")\\":\\n            depth -= 1\\n            if depth < 0:\\n                return False\\n    return depth == 0\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["algorithm"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
