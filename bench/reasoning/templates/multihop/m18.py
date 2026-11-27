import json as _json
TEMPLATE_ID = "m18"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "How many spare chairs are in the Hamilton meeting rooms altogether?", "answer": "11", "answer_type": "integer", "tags": ["bridge"], "hops": 2, "gold_support": ["m18-0-north", "m18-0-south"], "distractors": ["m18-0-tables"], "notes": [{"id": "m18-0-north", "claim": "The Hamilton north meeting room has 6 spare chairs."}, {"id": "m18-0-south", "claim": "The Hamilton south meeting room has 5 spare chairs."}, {"id": "m18-0-tables", "claim": "The Hamilton meeting rooms share 2 tables."}]}, {"question": "How many spare chairs are in the Dunedin meeting rooms altogether?", "answer": "9", "answer_type": "integer", "tags": ["bridge"], "hops": 2, "gold_support": ["m18-1-north", "m18-1-south"], "distractors": ["m18-1-tables"], "notes": [{"id": "m18-1-north", "claim": "The Dunedin north meeting room has 4 spare chairs."}, {"id": "m18-1-south", "claim": "The Dunedin south meeting room has 5 spare chairs."}, {"id": "m18-1-tables", "claim": "The Dunedin meeting rooms share 3 tables."}]}]')

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
