TEMPLATE_ID = 't008'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['template-trap', 'boxes-of', 'all-but', 'more-than-half']
DIFFICULTY = 2
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = "pens"
STEPS = 3
TOOL_TRAP = True
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}

def sample(rng):
    return {"boxes": rng.randrange(2, 10) * 2, "each": rng.randrange(2, 8) * 2, "extra": rng.randint(1, 6)}

def pinned(instance):
    if instance == 0:
        return {"boxes": 12, "each": 8, "extra": 5}
    return None

def render(params):
    if params == {"boxes": 12, "each": 8, "extra": 5}:
        return "A shop has 12 boxes of 8 pens. It sold all but 5 more than half of them. How many are left?"
    return (
        f"A shop has {params['boxes']} boxes of {params['each']} pens. "
        f"It sold all but {params['extra']} more than half of them. How many are left?"
    )

def solve(params):
    total = params["boxes"] * params["each"]
    return total // 2 + params["extra"]

def reference_for(params):
    return f"ANSWER = {params['boxes']} * {params['each']} // 2 + {params['extra']}"

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
