import json as _json
TEMPLATE_ID = "m03"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "What is the Wi-Fi password for the Hamilton office?", "answer": null, "answer_type": "unanswerable", "tags": ["unanswerable"], "hops": 2, "gold_support": ["hamilton-office-wifi"], "distractors": ["ana-folau-office-wifi"], "support_spans": {"hamilton-office-wifi": "not stored in these notes"}, "notes": [{"id": "hamilton-office-wifi", "claim": "The Hamilton office guest Wi-Fi network is named KH-Guest; the password is held by reception and is not stored in these notes."}, {"id": "ana-folau-office-wifi", "claim": "Ana Folau works from the Hamilton office."}]}, {"question": "What is the door code for the Dunedin archive?", "answer": null, "answer_type": "unanswerable", "tags": ["unanswerable"], "hops": 1, "gold_support": ["m03-1-door"], "distractors": ["m03-1-hours"], "notes": [{"id": "m03-1-door", "claim": "The Dunedin archive door code is held by the site lead and is not stored in these notes."}, {"id": "m03-1-hours", "claim": "The Dunedin archive is open from 9 am to 4 pm."}]}]')

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
