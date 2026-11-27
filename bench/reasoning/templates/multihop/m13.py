import json as _json
TEMPLATE_ID = "m13"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "How many days sit between the archive freeze and the archive reopen?", "answer": "10", "answer_type": "integer", "tags": ["date-arithmetic"], "hops": 2, "gold_support": ["m13-0-freeze", "m13-0-open"], "distractors": ["m13-0-place"], "notes": [{"id": "m13-0-freeze", "claim": "The archive freeze starts on 2026-05-01."}, {"id": "m13-0-open", "claim": "The archive reopen is on 2026-05-11."}, {"id": "m13-0-place", "claim": "The archive is in the Wellington office."}]}, {"question": "How many days sit between the depot shutdown and the depot reopen?", "answer": "4", "answer_type": "integer", "tags": ["date-arithmetic"], "hops": 2, "gold_support": ["m13-1-freeze", "m13-1-open"], "distractors": ["m13-1-place"], "notes": [{"id": "m13-1-freeze", "claim": "The depot shutdown starts on 2026-06-02."}, {"id": "m13-1-open", "claim": "The depot reopen is on 2026-06-06."}, {"id": "m13-1-place", "claim": "The depot is in the Napier office."}]}]')

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
