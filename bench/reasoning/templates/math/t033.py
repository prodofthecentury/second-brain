TEMPLATE_ID = 't033'
SUITE = "math"
ANSWER_TYPE = 'integer'
TAGS = ['count']
DIFFICULTY = 1
INSTANCES = 2
SKIP_NUMBER_CHECK = False
SHAPE = 'two_step_diff'
UNIT = None
STEPS = 1
TOOL_TRAP = False
EXPECTED_TOOL = None
MISSING_ON_INSTANCE_1 = True
PROVENANCE = {"source": "original", "derived_from": None, "license": "CC0-1.0", "author": "grok-bot", "created": "2026-11-27", "llm_assisted": True, "seed": None}
PINNED = None
RANGES = [('a', 30, 80), ('b', 2, 10), ('c', 2, 10)]
STEM = 'A tank starts with {a} litres, loses {b} litres, then loses {c} litres. How many litres remain?'
EXPR = "params['a'] - params['b'] - params['c']"

def sample(rng):
    for _ in range(400):
        params = {name: rng.randint(lo, hi) for name, lo, hi in RANGES}
        if SHAPE in {"half_then_add", "boxes_all_but"} and (params["boxes"] * params["each"]) % 2:
            params["each"] += 1
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
