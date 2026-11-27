import json as _json
TEMPLATE_ID = "l04"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Four speakers W, X, Y and Z go in a line. Y is first. W speaks before X. Z speaks immediately after X. W is not last. What is the order?", "answer_format_hint": "Give the order as letters separated by commas, first to last.", "answer": ["Y", "W", "X", "Z"], "answer_type": "sequence", "difficulty": 2, "tags": ["ordering"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor p in permutations(\\"WXYZ\\"):\\n    pos = {n: i for i, n in enumerate(p)}\\n    if pos[\\"W\\"] < pos[\\"X\\"] and pos[\\"Y\\"] == 0 and pos[\\"Z\\"] == pos[\\"X\\"] + 1 and pos[\\"W\\"] != 3:\\n        SOLUTIONS.append(list(p))\\n"}, {"question": "Four speakers P, Q, R and S go in a line. R is last. P speaks before Q. S speaks immediately after P. Q is not first. What is the order?", "answer_format_hint": "Give the order as letters separated by commas, first to last.", "answer": ["P", "S", "Q", "R"], "answer_type": "sequence", "difficulty": 2, "tags": ["ordering"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor p in permutations(\\"PQRS\\"):\\n    pos = {n: i for i, n in enumerate(p)}\\n    if pos[\\"P\\"] < pos[\\"Q\\"] and pos[\\"R\\"] == 3 and pos[\\"S\\"] == pos[\\"P\\"] + 1 and pos[\\"Q\\"] != 0:\\n        SOLUTIONS.append(list(p))\\n"}]')

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
