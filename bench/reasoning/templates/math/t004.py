TEMPLATE_ID = 't004'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['change']
DIFFICULTY = 1
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = "dollars"
STEPS = 2
TOOL_TRAP = False
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = True
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}

def sample(rng):
    return {"count": rng.randint(2, 12), "price": rng.choice([2, 3, 4]), "paid": rng.choice([20, 30, 40])}

def pinned(instance):
    return {"count": 8, "price": 2, "paid": 20}

def render(params):
    return f"Mere bought {params['count']} apples at ${params['price']} each and paid with a ${params['paid']} note. How much change did she get, in dollars?"

def render_missing_info(params):
    return "Mere bought some apples at $0.40 each and paid with a $10 note. How much change did she get, in dollars?"

def solve(params):
    return params["paid"] - params["count"] * params["price"]

def reference_for(params):
    return f"ANSWER = {params['paid']} - {params['count']} * {params['price']}"

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
