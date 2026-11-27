import json
TEMPLATE_ID = "c07"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c07-0", "template_id": "c07", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function is_palindrome(text) that returns True when the letters and digits, ignoring case and other characters, read the same forwards and backwards.", "entry_point": "is_palindrome", "signature": "def is_palindrome(text: str) -> bool:", "visible_tests": "assert is_palindrome(\\"A man, a plan, a canal: Panama\\") is True\\nassert is_palindrome(\\"ab\\") is False\\n", "hidden_tests": "assert is_palindrome(\\"\\") is True\\nassert is_palindrome(\\"No lemon, no melon\\") is True\\nassert is_palindrome(\\"race a car\\") is False\\n", "reference_solution": "def is_palindrome(text: str) -> bool:\\n    cleaned = \\"\\".join(ch.lower() for ch in text if ch.isalnum())\\n    return cleaned == cleaned[::-1]\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 1, "tags": ["string"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
