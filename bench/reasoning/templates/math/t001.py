TEMPLATE_ID = 't001'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['fraction-of', 'floor-division']
DIFFICULTY = 2
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = "bags"
STEPS = 4
TOOL_TRAP = False
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}

def sample(rng):
    return {"trays": rng.randint(8, 20), "rolls": rng.randint(12, 24)}

def pinned(instance):
    if instance == 0:
        return {"trays": 14, "rolls": 18}
    return None

def render(params):
    return (
        f"A bakery bakes {params['trays']} trays of {params['rolls']} rolls each morning. "
        "It sells 3/4 of the rolls and donates the rest to a shelter in bags of 6. "
        "How many full bags does the shelter get?"
    )

def solve(params):
    rolls = params["trays"] * params["rolls"]
    left = rolls - (rolls * 3) // 4
    return left // 6

def reference_for(params):
    return (
        f"rolls = {params['trays']} * {params['rolls']}\n"
        "left = rolls - rolls * 3 // 4\n"
        "ANSWER = left // 6"
    )

def build_item(instance):
    import random
    params = pinned(instance) if pinned(instance) is not None else sample(random.Random(f"{TEMPLATE_ID}:{instance}"))
    missing = instance == 1 and globals().get("MISSING_ON_INSTANCE_1")
    if missing:
        question = render_missing_info(params)
        answer = None
        answer_type = "unanswerable"
        answerable = False
        reference = "ANSWER = None"
        tags = list(TAGS) + ["missing-info"]
    else:
        question = render(params)
        answer = solve(params)
        answer_type = ANSWER_TYPE
        answerable = True
        reference = reference_for(params)
        tags = list(TAGS)
    if answer_type == "fraction":
        answer = str(answer)
    elif answer_type == "integer" and answer is not None:
        answer = str(int(answer))
    elif answer_type == "decimal" and answer is not None:
        answer = str(answer)
    return {
        "id": f"math-dev-{TEMPLATE_ID}-{instance}",
        "template_id": TEMPLATE_ID,
        "instance": instance,
        "suite": "math",
        "split": "dev",
        "question": question,
        "answer": answer,
        "answer_type": answer_type,
        "approx_ok": None,
        "answerable": answerable,
        "decimals": None,
        "unit": UNIT,
        "steps": STEPS,
        "tool_trap": TOOL_TRAP,
        "expected_tool": EXPECTED_TOOL if not missing else None,
        "difficulty": DIFFICULTY,
        "tags": tags,
        "provenance": PROVENANCE,
        "reference": reference,
    }
