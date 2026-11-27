import json as _json
TEMPLATE_ID = "l10"
SUITE = "logic"
ANSWER_TYPE = "sequence"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "A meeting is on a weekday from Monday to Friday. It is after Tuesday and before Friday, and the previous weekday is Tuesday. Which day is the meeting?", "answer_format_hint": "Give the day as Mon, Tue, Wed, Thu or Fri.", "answer": "Wed", "answer_type": "entity", "aliases": ["Wed", "Wednesday"], "difficulty": 1, "tags": ["scheduling"], "solver": "days = [\\"Mon\\", \\"Tue\\", \\"Wed\\", \\"Thu\\", \\"Fri\\"]\\nSOLUTIONS = [day for i, day in enumerate(days) if i > 1 and i < 4 and days[i-1] == \\"Tue\\"]\\n"}, {"question": "A rehearsal is on a weekday from Monday to Friday. It is before Thursday and after Monday, and the next weekday is Thursday. Which day is the rehearsal?", "answer_format_hint": "Give the day as Mon, Tue, Wed, Thu or Fri.", "answer": "Wed", "answer_type": "entity", "aliases": ["Wed"], "difficulty": 1, "tags": ["scheduling"], "solver": "days = [\\"Mon\\", \\"Tue\\", \\"Wed\\", \\"Thu\\", \\"Fri\\"]\\nSOLUTIONS = [day for i, day in enumerate(days) if i < 3 and i > 0 and days[i+1] == \\"Thu\\"]\\n"}]')

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
