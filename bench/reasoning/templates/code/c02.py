import json
TEMPLATE_ID = "c02"
SUITE = "code"
INSTANCES = 1
ITEM = json.loads('{"id": "code-dev-c02-0", "template_id": "c02", "instance": 0, "suite": "code", "split": "dev", "question": "Write a function merge_intervals(intervals) that takes a list of [start, end] integer pairs (start <= end, any order) and returns the merged list sorted by start. Intervals that overlap or touch (end == next start) are merged.", "entry_point": "merge_intervals", "signature": "def merge_intervals(intervals: list[list[int]]) -> list[list[int]]:", "visible_tests": "assert merge_intervals([[1, 3], [2, 6], [8, 10]]) == [[1, 6], [8, 10]]\\nassert merge_intervals([[1, 4], [4, 5]]) == [[1, 5]]\\n", "hidden_tests": "assert merge_intervals([]) == []\\nassert merge_intervals([[5, 7]]) == [[5, 7]]\\nassert merge_intervals([[8, 10], [1, 3], [2, 6]]) == [[1, 6], [8, 10]]\\nassert merge_intervals([[1, 10], [2, 3], [4, 5]]) == [[1, 10]]\\nassert merge_intervals([[1, 2], [3, 4]]) == [[1, 2], [3, 4]]\\nassert merge_intervals([[-3, -1], [-2, 0]]) == [[-3, 0]]\\n", "reference_solution": "def merge_intervals(intervals):\\n    result = []\\n    for start, end in sorted(intervals):\\n        if result and start <= result[-1][1]:\\n            result[-1][1] = max(result[-1][1], end)\\n        else:\\n            result.append([start, end])\\n    return result\\n", "timeout_s": 10, "answer": null, "answer_type": "code", "answerable": true, "approx_ok": null, "difficulty": 2, "tags": ["list", "algorithm", "unsorted-input"], "expected_tool": null, "provenance": {"source": "original", "derived_from": null, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": true, "seed": null}}')

def build_item(instance):
    if instance != 0:
        raise ValueError("code templates have one instance")
    return dict(ITEM)
