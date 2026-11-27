import json as _json
TEMPLATE_ID = "l12"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "On an island, knights always tell the truth and knaves always lie. P says: \'Q is a knave.\' Q says: \'R is a knave.\' R says: \'P and Q are both knaves.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as P: knight|knave, Q: knight|knave, R: knight|knave.", "answer": {"P": "knave", "Q": "knight", "R": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor bits in product([True, False], repeat=3):\\n    vals = dict(zip(\'PQR\', bits))\\n    if vals[\'P\'] == (not vals[\'Q\']) and vals[\'Q\'] == (not vals[\'R\']) and vals[\'R\'] == ((not vals[\'P\']) and (not vals[\'Q\'])):\\n        SOLUTIONS.append({k: (\'knight\' if vals[k] else \'knave\') for k in \'PQR\'})\\n"}, {"question": "On an island, knights always tell the truth and knaves always lie. S says: \'T is a knave.\' T says: \'U is a knave.\' U says: \'S and T are both knaves.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as S: knight|knave, T: knight|knave, U: knight|knave.", "answer": {"S": "knave", "T": "knight", "U": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor bits in product([True, False], repeat=3):\\n    vals = dict(zip(\'STU\', bits))\\n    if vals[\'S\'] == (not vals[\'T\']) and vals[\'T\'] == (not vals[\'U\']) and vals[\'U\'] == ((not vals[\'S\']) and (not vals[\'T\'])):\\n        SOLUTIONS.append({k: (\'knight\' if vals[k] else \'knave\') for k in \'STU\'})\\n"}]')

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
