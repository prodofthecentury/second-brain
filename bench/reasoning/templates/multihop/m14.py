import json as _json
TEMPLATE_ID = "m14"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Which city hosts the printer that the Hamilton office uses?", "answer": "Hamilton", "answer_type": "entity", "aliases": ["Hamilton"], "tags": ["bridge"], "hops": 2, "gold_support": ["m14-0-use", "m14-0-host"], "distractors": ["m14-0-other"], "notes": [{"id": "m14-0-use", "claim": "The Hamilton office uses the River printer."}, {"id": "m14-0-host", "claim": "The River printer is hosted in Hamilton."}, {"id": "m14-0-other", "claim": "The Coast printer is hosted in Tauranga."}]}, {"question": "Which city hosts the printer that the Dunedin office uses?", "answer": "Dunedin", "answer_type": "entity", "aliases": ["Dunedin"], "tags": ["bridge"], "hops": 2, "gold_support": ["m14-1-use", "m14-1-host"], "distractors": ["m14-1-other"], "notes": [{"id": "m14-1-use", "claim": "The Dunedin office uses the Harbour printer."}, {"id": "m14-1-host", "claim": "The Harbour printer is hosted in Dunedin."}, {"id": "m14-1-other", "claim": "The Hill printer is hosted in Queenstown."}]}]')

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
