TEMPLATE_ID = 't007'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ["template-trap", 'all-but', 'more-than-half', 'count']
DIFFICULTY = 2
INSTANCES = 2
SKIP_NUMBER_CHECK = False
SHAPE = 'pupils_bus'
UNIT = None
STEPS = 1
TOOL_TRAP = True
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = False
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}
PINNED = {'pupils': 40, 'extra': 5, 'left_out': 3}
RANGES = [('pupils', 20, 40), ('extra', 1, 4), ('left_out', 1, 3)]
STEM = 'A class has {pupils} pupils. {extra} more than half of them walk to school. All but {left_out} of the rest take the bus. How many take the bus?'
EXPR = "int(params['pupils'] - (__import__('fractions').Fraction(params['pupils'], 2) + params['extra']) - params['left_out'])"

def sample(rng):
    for _ in range(400):
        params = {name: rng.randint(lo, hi) for name, lo, hi in RANGES}
        if SHAPE in {"half_then_add", "boxes_all_but"} and (params["boxes"] * params["each"]) % 2:
            params["each"] += 1
        if SHAPE == "pupils_bus" and params["pupils"] % 2:
            params["pupils"] += 1
        if SHAPE == "ratio_part" and params["total"] % (params["num"] + params["den"]):
            continue
        if SHAPE in {"tax_amount", "percent_of", "complement_percent", "discount_fee", "split_tip", "percent_twice"}:
            if SHAPE == "percent_twice" and params["price"] % 100:
                continue
        value = solve(params)
        if ANSWER_TYPE == "integer":
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return params
        else:
            return params
    raise RuntimeError("sample failed for " + TEMPLATE_ID)

def pinned(instance):
    if instance == 0 and PINNED is not None:
        return dict(PINNED)
    return None

def render(params):
    return STEM.format(**params)

def render_missing_info(params):
    # Drop the first numeric parameter and say it is unknown.
    name = RANGES[0][0]
    shown = dict(params)
    shown[name] = "some"
    return STEM.format(**shown)

def solve(params):
    return eval(EXPR)

def reference_for(params):
    value = solve(params)
    if ANSWER_TYPE == "fraction":
        from fractions import Fraction
        value = Fraction(value)
        return f"from fractions import Fraction\nANSWER = Fraction({value.numerator}, {value.denominator})"
    return f"ANSWER = {int(value)}"

def build_item(instance):
    import random
    params = pinned(instance) if pinned(instance) is not None else sample(random.Random(f"{TEMPLATE_ID}:{instance}"))
    missing = bool(MISSING_ON_INSTANCE_1 and instance == 1)
    if missing:
        question = render_missing_info(params)
        answer = None
        answer_type = "unanswerable"
        answerable = False
        reference = "ANSWER = None"
        tags = list(TAGS) + ["missing-info"]
        shape = "missing_info"
    else:
        question = render(params)
        value = solve(params)
        answer_type = ANSWER_TYPE
        answerable = True
        if answer_type == "fraction":
            from fractions import Fraction
            answer = str(Fraction(value))
        else:
            answer = str(int(value))
        reference = reference_for(params)
        tags = list(TAGS)
        shape = SHAPE
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
        "difficulty": DIFFICULTY,
        "tags": tags,
        "shape": shape,
        "shape_params": {k: params[k] for k, *_ in RANGES},
        "expected_tool": None,
        "provenance": PROVENANCE,
        "reference": reference,
        "decimals": None,
        "unit": None,
        "steps": STEPS,
        "tool_trap": TOOL_TRAP,
    }
