TEMPLATE_ID = 't032'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['distractor-numbers']
DIFFICULTY = 1
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = "marbles"
STEPS = 2
TOOL_TRAP = False
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}

def sample(rng):
    held = rng.randint(20, 60)
    removed = rng.randint(1, 9)
    old = rng.randint(70, 90)
    return {"held": held, "removed": removed, "old": old}

def pinned(instance):
    if instance == 0:
        return {"held": 35, "removed": 8, "old": 85}
    return None

def render(params):
    return (
        f"A jar holds {params['held']} marbles. The faded label still says {params['old']}, which is an old stock count. "
        f"{params['removed']} marbles are removed. How many marbles are left?"
    )

def solve(params):
    return params["held"] - params["removed"]

def reference_for(params):
    return f"ANSWER = {params['held']} - {params['removed']}"

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
