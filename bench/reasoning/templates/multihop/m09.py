import json as _json
TEMPLATE_ID = "m09"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "How many GB of Willow full backups are produced across the retention window?", "answer": "72", "answer_type": "integer", "tags": ["answer-not-in-notes"], "hops": 3, "gold_support": ["m09-0-every", "m09-0-size", "m09-0-keep"], "distractors": ["m09-0-where"], "notes": [{"id": "m09-0-every", "claim": "The Willow server takes a full backup every 6 hours."}, {"id": "m09-0-size", "claim": "Each Willow full backup is 9 GB."}, {"id": "m09-0-keep", "claim": "Willow backups are kept for 2 days and then deleted."}, {"id": "m09-0-where", "claim": "The Willow server stands in a locked cupboard."}]}, {"question": "How many GB of Willow South full backups are produced across the retention window?", "answer": "60", "answer_type": "integer", "tags": ["answer-not-in-notes"], "hops": 3, "gold_support": ["m09-1-every", "m09-1-size", "m09-1-keep"], "distractors": ["m09-1-where"], "notes": [{"id": "m09-1-every", "claim": "The Willow South server takes a full backup every 8 hours."}, {"id": "m09-1-size", "claim": "Each Willow South full backup is 10 GB."}, {"id": "m09-1-keep", "claim": "Willow South backups are kept for 2 days and then deleted."}, {"id": "m09-1-where", "claim": "The Willow South server stands in a locked cupboard."}]}]')

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
