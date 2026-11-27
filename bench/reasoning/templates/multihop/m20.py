import json as _json
TEMPLATE_ID = "m20"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Is the Hamilton guest network password stored in these notes?", "answer": "no", "answer_type": "yesno", "tags": ["bridge"], "hops": 1, "gold_support": ["m20-0-wifi"], "distractors": ["m20-0-name"], "notes": [{"id": "m20-0-wifi", "claim": "The Hamilton guest network password is not stored in these notes."}, {"id": "m20-0-name", "claim": "The Hamilton guest network is named KH-Guest."}]}, {"question": "Is the Dunedin guest network password stored in these notes?", "answer": "no", "answer_type": "yesno", "tags": ["bridge"], "hops": 1, "gold_support": ["m20-1-wifi"], "distractors": ["m20-1-name"], "notes": [{"id": "m20-1-wifi", "claim": "The Dunedin guest network password is not stored in these notes."}, {"id": "m20-1-name", "claim": "The Dunedin guest network is named DN-Guest."}]}]')

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
