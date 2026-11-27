import json as _json
TEMPLATE_ID = "l03"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "I am a three-digit number. My digits strictly increase from left to right, they add up to 15, my middle digit is 5, and I am divisible by 7. What number am I?", "answer_format_hint": "Give the number only.", "answer": "357", "answer_type": "integer", "difficulty": 1, "tags": ["number-constraint"], "solver": "SOLUTIONS = [n for n in range(100, 1000) if (lambda d: d[0] < d[1] < d[2] and sum(d) == 15 and d[1] == 5 and n % 7 == 0)([int(x) for x in str(n)])]\\n"}, {"question": "I am a three-digit number. My digits strictly increase from left to right, they add up to 13, my middle digit is 4, and I am divisible by 4. What number am I?", "answer_format_hint": "Give the number only.", "answer": "148", "answer_type": "integer", "difficulty": 1, "tags": ["number-constraint"], "solver": "SOLUTIONS = [n for n in range(100, 1000) if (lambda d: d[0] < d[1] < d[2] and sum(d) == 13 and d[1] == 4 and n % 4 == 0)([int(x) for x in str(n)])]\\n"}]')

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
