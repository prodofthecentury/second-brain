import json as _json
TEMPLATE_ID = "l11"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Two digits are different, the smaller first. They add to 9 and multiply to 18. What are the two digits in order?", "answer_format_hint": "Give the digits separated by a comma, smaller first.", "answer": ["3", "6"], "answer_type": "sequence", "difficulty": 1, "tags": ["number-constraint"], "solver": "SOLUTIONS = [(a, b) for a in range(1, 10) for b in range(1, 10) if a < b and a + b == 9 and a * b == 18]\\nSOLUTIONS = [list(map(str, pair)) for pair in SOLUTIONS]\\n"}, {"question": "Two digits are different, the smaller first. They add to 10 and multiply to 21. What are the two digits in order?", "answer_format_hint": "Give the digits separated by a comma, smaller first.", "answer": ["3", "7"], "answer_type": "sequence", "difficulty": 1, "tags": ["number-constraint"], "solver": "SOLUTIONS = [(a, b) for a in range(1, 10) for b in range(1, 10) if a < b and a + b == 10 and a * b == 21]\\nSOLUTIONS = [list(map(str, pair)) for pair in SOLUTIONS]\\n"}]')

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
