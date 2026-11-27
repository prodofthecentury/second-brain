import json
TEMPLATE_ID = "c20"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c20-0", "template_id": "c20", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function fib(n) that returns the nth Fibonacci number, with fib(0) = 0 and fib(1) = 1. n is a non-negative integer no larger than 30.", "entry_point": "fib", "signature": "def fib(n: int) -> int:", "visible_tests": "assert fib(0) == 0\\nassert fib(6) == 8\\n", "hidden_tests": "assert fib(1) == 1\\nassert fib(10) == 55\\n", "reference_solution": "def fib(n: int) -> int:\\n    a, b = 0, 1\\n    for _ in range(n):\\n        a, b = b, a + b\\n    return a\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["algorithm"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
