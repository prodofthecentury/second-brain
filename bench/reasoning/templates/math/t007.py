TEMPLATE_ID = 't007'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['all-but', 'more-than-half']
DIFFICULTY = 2
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = "pupils"
STEPS = 3
TOOL_TRAP = True
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}

def sample(rng):
    pupils = rng.randrange(10, 30) * 2
    extra = rng.randint(1, 4)
    left_out = rng.randint(1, 4)
    return {"pupils": pupils, "extra": extra, "left_out": left_out}

def pinned(instance):
    if instance == 0:
        return {"pupils": 40, "extra": 5, "left_out": 3}
    return None

def render(params):
    if params == {"pupils": 40, "extra": 5, "left_out": 3}:
        return "A class has 40 pupils. 5 more than half of them walk to school. All but 3 of the rest take the bus. How many take the bus?"
    return (
        f"A class has {params['pupils']} pupils. {params['extra']} more than half of them walk to school. "
        f"All but {params['left_out']} of the rest take the bus. How many take the bus?"
    )

def solve(params):
    walk = params["pupils"] // 2 + params["extra"]
    rest = params["pupils"] - walk
    return rest - params["left_out"]

def reference_for(params):
    return (
        f"walk = {params['pupils']} // 2 + {params['extra']}\n"
        f"rest = {params['pupils']} - walk\n"
        f"ANSWER = rest - {params['left_out']}"
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
