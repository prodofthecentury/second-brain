import json as _json
TEMPLATE_ID = "m06"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Which office does the current lead of Project Miro work from?", "answer": "New Plymouth", "answer_type": "entity", "aliases": ["New Plymouth", "New Plymouth office"], "tags": ["temporal-supersession"], "hops": 2, "gold_support": ["m06-0-lead", "m06-0-office"], "distractors": ["m06-0-old", "m06-0-old-office"], "notes": [{"id": "m06-0-old", "claim": "the previous lead led Project Miro until 2026-02-01.", "valid_from": "2025-06-01", "valid_until": "2026-02-01"}, {"id": "m06-0-lead", "claim": "Since 2026-02-01 Project Miro has been led by Sione Ata.", "valid_from": "2026-02-01"}, {"id": "m06-0-office", "claim": "Sione Ata works from the New Plymouth office."}, {"id": "m06-0-old-office", "claim": "the previous lead works from the old office."}]}, {"question": "Which office does the current lead of Project Totara work from?", "answer": "Invercargill", "answer_type": "entity", "aliases": ["Invercargill", "Invercargill office"], "tags": ["temporal-supersession"], "hops": 2, "gold_support": ["m06-1-lead", "m06-1-office"], "distractors": ["m06-1-old", "m06-1-old-office"], "notes": [{"id": "m06-1-old", "claim": "the earlier lead led Project Totara until 2026-02-01.", "valid_from": "2025-06-01", "valid_until": "2026-02-01"}, {"id": "m06-1-lead", "claim": "Since 2026-02-01 Project Totara has been led by Lani Chen.", "valid_from": "2026-02-01"}, {"id": "m06-1-office", "claim": "Lani Chen works from the Invercargill office."}, {"id": "m06-1-old-office", "claim": "the earlier lead works from the former office."}]}]')

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
