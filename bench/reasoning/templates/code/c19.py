import json
TEMPLATE_ID = "c19"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c19-0", "template_id": "c19", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function max_sum(nums) that returns the maximum sum of any non-empty contiguous subarray. nums contains at least one integer.", "entry_point": "max_sum", "signature": "def max_sum(nums: list[int]) -> int:", "visible_tests": "assert max_sum([1, -2, 3, 4]) == 7\\nassert max_sum([-5, -1]) == -1\\n", "hidden_tests": "assert max_sum([5]) == 5\\nassert max_sum([-2, 1, -3, 4, -1, 2, 1, -5, 4]) == 6\\n", "reference_solution": "def max_sum(nums):\\n    best = nums[0]\\n    running = nums[0]\\n    for value in nums[1:]:\\n        running = value if running < 0 else running + value\\n        if running > best:\\n            best = running\\n    return best\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["algorithm"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
