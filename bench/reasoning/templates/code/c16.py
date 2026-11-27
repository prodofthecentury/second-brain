import json
TEMPLATE_ID = "c16"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c16-0", "template_id": "c16", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function gcd(a, b) that returns the greatest common divisor of two non-negative integers. gcd(0, 0) returns 0.", "entry_point": "gcd", "signature": "def gcd(a: int, b: int) -> int:", "visible_tests": "assert gcd(12, 8) == 4\\nassert gcd(7, 1) == 1\\n", "hidden_tests": "assert gcd(0, 5) == 5\\nassert gcd(0, 0) == 0\\nassert gcd(54, 24) == 6\\n", "reference_solution": "def gcd(a: int, b: int) -> int:\\n    while b:\\n        a, b = b, a % b\\n    return a\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["algorithm"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
