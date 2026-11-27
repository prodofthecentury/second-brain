import json as _json
TEMPLATE_ID = "l08"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "I am a whole number from 1 to 20. Exactly three of these are true: I am even, I am greater than 10, I am divisible by 3, I am less than 8. I am also divisible by 4. What number am I?", "answer_format_hint": "Give the number only.", "answer": "12", "answer_type": "integer", "difficulty": 2, "tags": ["number-constraint"], "solver": "SOLUTIONS = [n for n in range(1, 21) if sum([n % 2 == 0, n > 10, n % 3 == 0, n < 8]) == 3 and n % 4 == 0]\\n"}, {"question": "I am a whole number from 1 to 20. Exactly three of these are true: I am odd, I am greater than 9, I am divisible by 5, I am less than 16. I am also divisible by 11. What number am I?", "answer_format_hint": "Give the number only.", "answer": "11", "answer_type": "integer", "difficulty": 2, "tags": ["number-constraint"], "solver": "SOLUTIONS = [n for n in range(1, 21) if sum([n % 2 == 1, n > 9, n % 5 == 0, n < 16]) == 3 and n % 11 == 0]\\n"}]')

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
