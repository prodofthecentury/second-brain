TEMPLATE_ID = 't006'
SUITE = "math"
ANSWER_TYPE = 'fraction'
TAGS = ['fraction-exact']
DIFFICULTY = 1
INSTANCES = 2
SKIP_NUMBER_CHECK = False


from fractions import Fraction
UNIT = None
STEPS = 1
TOOL_TRAP = True
EXPECTED_TOOL = "any"
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}

def sample(rng):
    return {"a": 1, "b": rng.choice([3, 5]), "c": 1, "d": rng.choice([6, 10])}

def pinned(instance):
    if instance == 0:
        return {"a": 1, "b": 3, "c": 1, "d": 6}
    return None

def render(params):
    if (params["a"], params["b"], params["c"], params["d"]) == (1, 3, 1, 6):
        return "What is 1/3 + 1/6?"
    return f"What is {params['a']}/{params['b']} + {params['c']}/{params['d']}?"

def solve(params):
    return Fraction(params["a"], params["b"]) + Fraction(params["c"], params["d"])

def reference_for(params):
    return (
        "from fractions import Fraction\n"
        f"ANSWER = Fraction({params['a']}, {params['b']}) + Fraction({params['c']}, {params['d']})"
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
