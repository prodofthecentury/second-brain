import json as _json
TEMPLATE_ID = "l07"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "On an island, knights always tell the truth and knaves always lie. J says: \'K is a knave.\' K says: \'L is a knave.\' L says: \'J and K are both knaves.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as J: knight|knave, K: knight|knave, L: knight|knave.", "answer": {"J": "knave", "K": "knight", "L": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor bits in product([True, False], repeat=3):\\n    vals = dict(zip(\'JKL\', bits))\\n    if vals[\'J\'] == (not vals[\'K\']) and vals[\'K\'] == (not vals[\'L\']) and vals[\'L\'] == ((not vals[\'J\']) and (not vals[\'K\'])):\\n        SOLUTIONS.append({k: (\'knight\' if vals[k] else \'knave\') for k in \'JKL\'})\\n"}, {"question": "On an island, knights always tell the truth and knaves always lie. M says: \'N is a knave.\' N says: \'O is a knave.\' O says: \'M and N are both knaves.\' Who is a knight and who is a knave?", "answer_format_hint": "Answer as M: knight|knave, N: knight|knave, O: knight|knave.", "answer": {"M": "knave", "N": "knight", "O": "knave"}, "answer_type": "assignment", "difficulty": 2, "tags": ["knights-knaves"], "solver": "from itertools import product\\nSOLUTIONS = []\\nfor bits in product([True, False], repeat=3):\\n    vals = dict(zip(\'MNO\', bits))\\n    if vals[\'M\'] == (not vals[\'N\']) and vals[\'N\'] == (not vals[\'O\']) and vals[\'O\'] == ((not vals[\'M\']) and (not vals[\'N\'])):\\n        SOLUTIONS.append({k: (\'knight\' if vals[k] else \'knave\') for k in \'MNO\'})\\n"}]')

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
