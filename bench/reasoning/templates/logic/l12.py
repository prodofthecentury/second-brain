import json as _json
TEMPLATE_ID = "l12"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "A prize is in box 1, 2 or 3. Exactly one label is true. Box 1 says the prize is in 1. Box 2 says the prize is in 1. Box 3 says the prize is in 2. Which box has the prize?", "answer_format_hint": "Give the box number only.", "answer": "2", "answer_type": "integer", "difficulty": 2, "tags": ["boxes"], "solver": "SOLUTIONS = []\\nfor prize in (1, 2, 3):\\n    labels = [prize == 1, prize == 1, prize == 2]\\n    if sum(labels) == 1:\\n        SOLUTIONS.append(prize)\\n"}, {"question": "A prize is in box 1, 2 or 3. Exactly one label is true. Box 1 says the prize is in 1. Box 2 says the prize is in 1. Box 3 says the prize is in 3. Which box has the prize?", "answer_format_hint": "Give the box number only.", "answer": "3", "answer_type": "integer", "difficulty": 2, "tags": ["boxes"], "solver": "SOLUTIONS = []\\nfor prize in (1, 2, 3):\\n    labels = [prize == 1, prize == 1, prize == 3]\\n    if sum(labels) == 1:\\n        SOLUTIONS.append(prize)\\n"}]')

def sample(rng):
    return {"index": rng.randrange(2)}

def render(params):
    return WORLDS[params["index"]]["question"]

def solve(params):
    return WORLDS[params["index"]]["answer"]

def build_item(instance):
    world = WORLDS[instance]
    return {
        "id": f"logic-dev-{TEMPLATE_ID}-{instance}",
        "template_id": TEMPLATE_ID,
        "instance": instance,
        "suite": "logic",
        "split": "dev",
        "question": world["question"],
        "answer_format_hint": world["answer_format_hint"],
        "answer": world["answer"],
        "answer_type": world["answer_type"],
        "approx_ok": None,
        "answerable": True,
        "difficulty": world["difficulty"],
        "tags": world["tags"],
        "solver": world["solver"],
        "expected_tool": None,
        "provenance": {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None},
    }
