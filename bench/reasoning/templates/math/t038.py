TEMPLATE_ID = 't038'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['quantity']
DIFFICULTY = 1
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = 'seeds'
STEPS = 1
TOOL_TRAP = False
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = True
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}
FRAME = 'A tray has {count} seeds. {used} fail to sprout. How many seeds sprout?'

def sample(rng):
    count = rng.randint(12, 40)
    used = rng.randint(1, 8)
    return {"count": count, "used": used}

def pinned(instance):
    return {"count": 23, "used": 2}

def render(params):
    return FRAME.format(count=params["count"], used=params["used"])

def render_missing_info(params):
    return FRAME.format(count="some", used=params["used"]).replace("some", "some")

def solve(params):
    return params["count"] - params["used"]

def reference_for(params):
    return f"ANSWER = {params['count']} - {params['used']}"

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
