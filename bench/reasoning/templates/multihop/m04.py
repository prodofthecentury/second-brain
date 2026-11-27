import json as _json
TEMPLATE_ID = "m04"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "How many GB of Atlas full backups are produced across the retention window?", "answer": "168", "answer_type": "integer", "tags": ["answer-not-in-notes"], "hops": 3, "gold_support": ["atlas-backup-schedule", "atlas-backup-size", "atlas-retention"], "distractors": ["atlas-host"], "notes": [{"id": "atlas-backup-schedule", "claim": "The Atlas server takes a full backup every 6 hours."}, {"id": "atlas-backup-size", "claim": "Each Atlas full backup is 14 GB."}, {"id": "atlas-retention", "claim": "Atlas backups are kept for 3 days and then deleted."}, {"id": "atlas-host", "claim": "The Atlas server stands in the Hamilton office."}]}, {"question": "How many GB of Beacon full backups are produced across the retention window?", "answer": "90", "answer_type": "integer", "tags": ["answer-not-in-notes"], "hops": 3, "gold_support": ["m04-1-schedule", "m04-1-size", "m04-1-keep"], "distractors": ["m04-1-host"], "notes": [{"id": "m04-1-schedule", "claim": "The Beacon server takes a full backup every 8 hours."}, {"id": "m04-1-size", "claim": "Each Beacon full backup is 10 GB."}, {"id": "m04-1-keep", "claim": "Beacon backups are kept for 3 days and then deleted."}, {"id": "m04-1-host", "claim": "The Beacon server stands in the Dunedin office."}]}]')

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
