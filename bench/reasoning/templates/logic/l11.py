import json as _json
TEMPLATE_ID = "l11"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Five runners A, B, C, D, E finish a race with no ties. B finishes before D. C finishes immediately after A. E is neither first nor last. D finishes before A. E finishes before D. What is the finishing order?", "answer_format_hint": "Give the finishing order as letters separated by commas, first to last.", "answer": ["B", "E", "D", "A", "C"], "answer_type": "sequence", "difficulty": 2, "tags": ["ordering"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor p in permutations(\'ABCDE\'):\\n    pos = {n: i for i, n in enumerate(p)}\\n    if pos[\'B\'] < pos[\'D\'] and pos[\'C\'] == pos[\'A\'] + 1 and pos[\'E\'] not in (0, 4) and pos[\'D\'] < pos[\'A\'] and pos[\'E\'] < pos[\'D\']:\\n        SOLUTIONS.append(list(p))\\n"}, {"question": "Five runners P, Q, R, S, T finish a race with no ties. Q finishes before S. R finishes immediately after P. T is neither first nor last. S finishes before P. T finishes before S. What is the finishing order?", "answer_format_hint": "Give the finishing order as letters separated by commas, first to last.", "answer": ["Q", "T", "S", "P", "R"], "answer_type": "sequence", "difficulty": 2, "tags": ["ordering"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor p in permutations(\'PQRST\'):\\n    pos = {n: i for i, n in enumerate(p)}\\n    if pos[\'Q\'] < pos[\'S\'] and pos[\'R\'] == pos[\'P\'] + 1 and pos[\'T\'] not in (0, 4) and pos[\'S\'] < pos[\'P\'] and pos[\'T\'] < pos[\'S\']:\\n        SOLUTIONS.append(list(p))\\n"}]')

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
