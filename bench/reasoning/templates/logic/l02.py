import json as _json
TEMPLATE_ID = "l02"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "On an island, knights always tell the truth and knaves always lie. A says: \'B is a knave.\' B says: \'C is a knave.\' C says: \'A and B are both knaves.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as A: knight|knave, B: knight|knave, C: knight|knave.", "answer": {"A": "knave", "B": "knight", "C": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor a, b, c in product([True, False], repeat=3):\\n    if a == (not b) and b == (not c) and c == ((not a) and (not b)):\\n        SOLUTIONS.append({k: (\'knight\' if v else \'knave\') for k, v in zip(\'ABC\', (a, b, c))})\\n"}, {"question": "On an island, knights always tell the truth and knaves always lie. D says: \'E is a knave.\' E says: \'F is a knave.\' F says: \'D and E are both knaves.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as D: knight|knave, E: knight|knave, F: knight|knave.", "answer": {"D": "knave", "E": "knight", "F": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor bits in product([True, False], repeat=3):\\n    vals = dict(zip(\'DEF\', bits))\\n    if vals[\'D\'] == (not vals[\'E\']) and vals[\'E\'] == (not vals[\'F\']) and vals[\'F\'] == ((not vals[\'D\']) and (not vals[\'E\'])):\\n        SOLUTIONS.append({k: (\'knight\' if vals[k] else \'knave\') for k in \'DEF\'})\\n"}]')

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
        "provenance": {'source': 'original', 'derived_from': None, 'license': 'CC0-1.0', 'author': 'grok-bot', 'created': '2026-11-27', 'llm_assisted': True, 'seed': None},
    }
