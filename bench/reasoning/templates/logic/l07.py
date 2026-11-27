import json as _json
TEMPLATE_ID = "l07"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Ann, Bo and Cy sit in left, middle and right seats, one each. Ann is not on the right. Bo is not on the left and not in the middle. Cy sits to the right of Ann. Who sits where?", "answer_format_hint": "Answer as Ann: seat, Bo: seat, Cy: seat.", "answer": {"Ann": "left", "Bo": "right", "Cy": "middle"}, "answer_type": "assignment", "difficulty": 2, "tags": ["assignment"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor a, b, c in permutations([\\"left\\", \\"middle\\", \\"right\\"]):\\n    # Ann is not right. Bo is not left. Cy is right of Ann. Bo is not middle.\\n    pos = {\\"Ann\\": a, \\"Bo\\": b, \\"Cy\\": c}\\n    order = {\\"left\\": 0, \\"middle\\": 1, \\"right\\": 2}\\n    if pos[\\"Ann\\"] != \\"right\\" and pos[\\"Bo\\"] != \\"left\\" and pos[\\"Bo\\"] != \\"middle\\" and order[pos[\\"Cy\\"]] > order[pos[\\"Ann\\"]] and len({a, b, c}) == 3:\\n        SOLUTIONS.append({\\"Ann\\": a, \\"Bo\\": b, \\"Cy\\": c})\\n"}, {"question": "Dee, Eve and Fay sit in left, middle and right seats, one each. Dee is not on the left. Eve is not on the right and not in the middle. Fay sits to the left of Dee. Who sits where?", "answer_format_hint": "Answer as Dee: seat, Eve: seat, Fay: seat.", "answer": {"Dee": "right", "Eve": "left", "Fay": "middle"}, "answer_type": "assignment", "difficulty": 2, "tags": ["assignment"], "solver": "from itertools import permutations\\nSOLUTIONS = []\\nfor a, b, c in permutations([\\"left\\", \\"middle\\", \\"right\\"]):\\n    pos = {\\"Dee\\": a, \\"Eve\\": b, \\"Fay\\": c}\\n    order = {\\"left\\": 0, \\"middle\\": 1, \\"right\\": 2}\\n    if pos[\\"Dee\\"] != \\"left\\" and pos[\\"Eve\\"] != \\"right\\" and pos[\\"Eve\\"] != \\"middle\\" and order[pos[\\"Fay\\"]] < order[pos[\\"Dee\\"]] and len({a, b, c}) == 3:\\n        SOLUTIONS.append({\\"Dee\\": a, \\"Eve\\": b, \\"Fay\\": c})\\n"}]')

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
