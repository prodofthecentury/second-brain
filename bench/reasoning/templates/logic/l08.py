import json as _json
TEMPLATE_ID = "l08"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Ann, Bo and Cy each use a different room: Red, Blue or Green. Ann uses the Red room. Bo uses the Blue room. Which room does each person use?", "answer_format_hint": "Answer as Ann: room, Bo: room, Cy: room.", "answer": {"Ann": "Red", "Bo": "Blue", "Cy": "Green"}, "answer_type": "assignment", "difficulty": 1, "tags": ["assignment"], "solver": "SOLUTIONS = []\\npeople = [\'Ann\', \'Bo\', \'Cy\']\\nrooms = [\'Red\', \'Blue\', \'Green\']\\nfrom itertools import permutations\\nfor assign in permutations(rooms):\\n    got = dict(zip(people, assign))\\n    if got[\'Ann\'] == \'Red\' and got[\'Bo\'] == \'Blue\' and got[\'Cy\'] == \'Green\':\\n        SOLUTIONS.append(got)\\n"}, {"question": "Ann, Bo and Cy each use a different room: Red, Blue or Green. Ann uses the Red room. Bo uses the Green room. Which room does each person use?", "answer_format_hint": "Answer as Ann: room, Bo: room, Cy: room.", "answer": {"Ann": "Red", "Bo": "Green", "Cy": "Blue"}, "answer_type": "assignment", "difficulty": 1, "tags": ["assignment"], "solver": "SOLUTIONS = []\\npeople = [\'Ann\', \'Bo\', \'Cy\']\\nrooms = [\'Red\', \'Blue\', \'Green\']\\nfrom itertools import permutations\\nfor assign in permutations(rooms):\\n    got = dict(zip(people, assign))\\n    if got[\'Ann\'] == \'Red\' and got[\'Bo\'] == \'Green\' and got[\'Cy\'] == \'Blue\':\\n        SOLUTIONS.append(got)\\n"}]')

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
