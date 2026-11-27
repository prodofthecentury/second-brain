import json as _json
TEMPLATE_ID = "m16"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "What salary does the Hamilton office lead receive?", "answer": null, "answer_type": "unanswerable", "tags": ["unanswerable"], "hops": 1, "gold_support": ["m16-0-pay"], "distractors": ["m16-0-role"], "notes": [{"id": "m16-0-pay", "claim": "Pay figures for the Hamilton office lead are confidential and are not stored in these notes."}, {"id": "m16-0-role", "claim": "The Hamilton office lead approves visitor access."}]}, {"question": "Which vendor services the private staff lift?", "answer": null, "answer_type": "unanswerable", "tags": ["unanswerable"], "hops": 1, "gold_support": ["m16-1-lift"], "distractors": ["m16-1-floor"], "notes": [{"id": "m16-1-lift", "claim": "The private staff lift is serviced under a contract that is not named in these notes."}, {"id": "m16-1-floor", "claim": "The private staff lift serves floors 2 and 3."}]}]')

def sample(rng):
    return {"index": rng.randrange(2)}

def render(params):
    return WORLDS[params["index"]]["question"]

def solve(params):
    world = WORLDS[params["index"]]
    if world["answer"] is None:
        return None
    if world["answer_type"] == "integer":
        return int(world["answer"])
    return world["answer"]

def build_item(instance):
    import json as _json
    world = WORLDS[instance]
    answer = world["answer"]
    if answer is None:
        reference = "ANSWER = None"
    elif world["answer_type"] == "integer":
        reference = f"ANSWER = {int(answer)}"
    else:
        reference = "ANSWER = " + _json.dumps(answer)
    item = {
        "id": f"multihop-dev-{TEMPLATE_ID}-{instance}",
        "template_id": TEMPLATE_ID,
        "instance": instance,
        "suite": "multihop",
        "split": "dev",
        "question": world["question"],
        "answer": None if answer is None else (int(answer) if world["answer_type"] == "integer" else answer),
        "answer_type": world["answer_type"],
        "approx_ok": None,
        "answerable": answer is not None,
        "hops": world["hops"],
        "gold_support": list(world["gold_support"]),
        "distractors": list(world["distractors"]),
        "notes": world["notes"],
        "expected_tool": None,
        "difficulty": 2 if world["hops"] > 1 else 1,
        "tags": list(world["tags"]),
        "provenance": {'source': 'original', 'derived_from': None, 'license': 'CC0-1.0', 'author': 'grok-bot', 'created': '2026-11-27', 'llm_assisted': True, 'seed': None},
        "reference": reference,
    }
    if world.get("aliases"):
        item["aliases"] = list(world["aliases"])
    if world.get("support_spans"):
        item["support_spans"] = world["support_spans"]
    if world["answer_type"] == "integer" and answer is not None:
        item["answer"] = str(int(answer))
    return item
