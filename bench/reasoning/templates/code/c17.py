import json
TEMPLATE_ID = "c17"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c17-0", "template_id": "c17", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function binary_search(nums, target) that returns the index of target in a sorted list of integers, or -1 when it is absent.", "entry_point": "binary_search", "signature": "def binary_search(nums: list[int], target: int) -> int:", "visible_tests": "assert binary_search([1, 3, 5], 3) == 1\\nassert binary_search([1, 3, 5], 4) == -1\\n", "hidden_tests": "assert binary_search([], 1) == -1\\nassert binary_search([2], 2) == 0\\nassert binary_search([1, 2, 3, 4], 4) == 3\\n", "reference_solution": "def binary_search(nums, target):\\n    lo, hi = 0, len(nums) - 1\\n    while lo <= hi:\\n        mid = (lo + hi) // 2\\n        if nums[mid] == target:\\n            return mid\\n        if nums[mid] < target:\\n            lo = mid + 1\\n        else:\\n            hi = mid - 1\\n    return -1\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["algorithm"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
