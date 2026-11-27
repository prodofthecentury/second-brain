TEMPLATE_ID = 't009'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['template-trap', 'leading-zero']
DIFFICULTY = 1
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = "pens"
STEPS = 1
TOOL_TRAP = True
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}

def sample(rng):
    return {"boxes": rng.choice([8, 10, 12]), "each": rng.choice([6, 8])}

def pinned(instance):
    if instance == 0:
        return {"boxes": 12, "each": 8}
    return None

def render(params):
    if params == {"boxes": 12, "each": 8}:
        return "The office fax is 09-377 1234. A shop sold 12 boxes of 8 pens. How many pens were sold?"
    return f"The office fax is 09-377 1234. A shop sold {params['boxes']} boxes of {params['each']} pens. How many pens were sold?"

def solve(params):
    return params["boxes"] * params["each"]

def reference_for(params):
    return f"ANSWER = {params['boxes']} * {params['each']}"

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
