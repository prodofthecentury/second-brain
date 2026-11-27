import json as _json
TEMPLATE_ID = "m17"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Which team owns the ledger service?", "answer": "Finance", "answer_type": "entity", "aliases": ["Finance", "Finance team"], "tags": ["bridge"], "hops": 2, "gold_support": ["m17-0-service", "m17-0-owner"], "distractors": ["m17-0-other"], "notes": [{"id": "m17-0-service", "claim": "The ledger service is owned by the Quill squad."}, {"id": "m17-0-owner", "claim": "The Quill squad sits inside the Finance team."}, {"id": "m17-0-other", "claim": "The Pixel squad sits inside the Design team."}]}, {"question": "Which team owns the roster service?", "answer": "People", "answer_type": "entity", "aliases": ["People", "People team"], "tags": ["bridge"], "hops": 2, "gold_support": ["m17-1-service", "m17-1-owner"], "distractors": ["m17-1-other"], "notes": [{"id": "m17-1-service", "claim": "The roster service is owned by the Waka squad."}, {"id": "m17-1-owner", "claim": "The Waka squad sits inside the People team."}, {"id": "m17-1-other", "claim": "The Kite squad sits inside the Marketing team."}]}]')

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
