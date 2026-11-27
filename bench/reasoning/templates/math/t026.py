TEMPLATE_ID = 't026'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['template-trap', 'boxes-of', 'more-than-half']
DIFFICULTY = 2
INSTANCES = 2
SKIP_NUMBER_CHECK = False


UNIT = "items"
STEPS = 2
TOOL_TRAP = True
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}
KIND = 0

def sample(rng):
    params = {"a": rng.randint(3, 12), "b": rng.randint(max(4, 3), 14)}
    if KIND == 0:
        params["c"] = rng.randint(2, 7)
    return params

def pinned(instance):
    if instance == 0:
        params = {"a": 13, "b": 14}
        if KIND == 0:
            params["c"] = 3
        return params
    return None

def render(params):
    if KIND == 0:
        return (
            f"A storeroom packs {params['a']} boxes of {params['b']} mugs. "
            f"It sells half of the mugs and then gets {params['c']} more mugs delivered. How many mugs are there now?"
        )
    if KIND == 1:
        return (
            f"The reminders were {params['a']} times {params['b']} minutes apart. "
            f"How many minutes apart were the reminders?"
        )
    return (
        f"The changelog covers versions {params['a']}-{params['b']}. Counting both ends, how many version numbers is that?"
    )

def solve(params):
    if KIND == 0:
        return params["a"] * params["b"] // 2 + params["c"]
    if KIND == 1:
        return params["b"]
    return params["b"] - params["a"] + 1

def reference_for(params):
    if KIND == 0:
        return f"ANSWER = {params['a']} * {params['b']} // 2 + {params['c']}"
    if KIND == 1:
        return f"ANSWER = {params['b']}"
    return f"ANSWER = {params['b']} - {params['a']} + 1"

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
