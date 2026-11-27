import json as _json
TEMPLATE_ID = "l05"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Knights always tell the truth and knaves always lie. A says: \'A and B are the same type.\' B says: \'A and B are different types.\' C says: \'A is a knave.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as A: knight|knave, B: knight|knave, C: knight|knave.", "answer": {"A": "knave", "B": "knight", "C": "knight"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor a, b, c in product([True, False], repeat=3):\\n    # A: \\"A and B are the same type\\". B: \\"A and B are different types\\". C: \\"A is a knave\\".\\n    if a == (a == b) and b == (a != b) and c == (not a):\\n        SOLUTIONS.append({\\"A\\": \\"knight\\" if a else \\"knave\\", \\"B\\": \\"knight\\" if b else \\"knave\\", \\"C\\": \\"knight\\" if c else \\"knave\\"})\\n"}, {"question": "Knights always tell the truth and knaves always lie. D says: \'D and E are the same type.\' E says: \'D and E are different types.\' F says: \'D is a knave.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as D: knight|knave, E: knight|knave, F: knight|knave.", "answer": {"D": "knave", "E": "knight", "F": "knight"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor d, e, f in product([True, False], repeat=3):\\n    if d == (d == e) and e == (d != e) and f == (not d):\\n        SOLUTIONS.append({\\"D\\": \\"knight\\" if d else \\"knave\\", \\"E\\": \\"knight\\" if e else \\"knave\\", \\"F\\": \\"knight\\" if f else \\"knave\\"})\\n"}]')

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
