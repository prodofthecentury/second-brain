import json as _json
TEMPLATE_ID = "l09"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Two people, A and B. Knights tell the truth and knaves lie. A says: \'B is a knight.\' B says: \'A and I are different types.\' What is each person?", "answer_format_hint": "Answer as A: knight|knave, B: knight|knave.", "answer": {"A": "knave", "B": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor a, b in product([True, False], repeat=2):\\n    # A: \\"B is a knight\\". B: \\"A and I are different types\\".\\n    if a == b and b == (a != b):\\n        SOLUTIONS.append({\\"A\\": \\"knight\\" if a else \\"knave\\", \\"B\\": \\"knight\\" if b else \\"knave\\"})\\n"}, {"question": "Two people, C and D. Knights tell the truth and knaves lie. C says: \'D is a knave.\' D says: \'C and I are the same type.\' What is each person?", "answer_format_hint": "Answer as C: knight|knave, D: knight|knave.", "answer": {"C": "knight", "D": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor a, b in product([True, False], repeat=2):\\n    # C: \\"D is a knave\\". D: \\"C and I are the same type\\".\\n    if a == (not b) and b == (a == b):\\n        SOLUTIONS.append({\\"C\\": \\"knight\\" if a else \\"knave\\", \\"D\\": \\"knight\\" if b else \\"knave\\"})\\n"}]')

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
