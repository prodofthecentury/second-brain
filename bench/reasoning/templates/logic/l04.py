import json as _json
TEMPLATE_ID = "l04"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Five runners P, Q, R, S, T finish a race with no ties. Q finishes before S. R finishes immediately after P. T is neither first nor last. S finishes before P. T finishes before S. What is the finishing order?", "answer_format_hint": "Give the finishing order as letters separated by commas, first to last.", "answer": ["Q", "T", "S", "P", "R"], "answer_type": "sequence", "difficulty": 2, "tags": ["ordering"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor p in permutations(\'PQRST\'):\\n    pos = {n: i for i, n in enumerate(p)}\\n    if pos[\'Q\'] < pos[\'S\'] and pos[\'R\'] == pos[\'P\'] + 1 and pos[\'T\'] not in (0, 4) and pos[\'S\'] < pos[\'P\'] and pos[\'T\'] < pos[\'S\']:\\n        SOLUTIONS.append(list(p))\\n"}, {"question": "Five runners U, V, W, X, Y finish a race with no ties. V finishes before X. W finishes immediately after U. Y is neither first nor last. X finishes before U. Y finishes before X. What is the finishing order?", "answer_format_hint": "Give the finishing order as letters separated by commas, first to last.", "answer": ["V", "Y", "X", "U", "W"], "answer_type": "sequence", "difficulty": 2, "tags": ["ordering"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor p in permutations(\'UVWXY\'):\\n    pos = {n: i for i, n in enumerate(p)}\\n    if pos[\'V\'] < pos[\'X\'] and pos[\'W\'] == pos[\'U\'] + 1 and pos[\'Y\'] not in (0, 4) and pos[\'X\'] < pos[\'U\'] and pos[\'Y\'] < pos[\'X\']:\\n        SOLUTIONS.append(list(p))\\n"}]')

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
