import json as _json
TEMPLATE_ID = "m06"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Does the night kiln need a large vent?", "answer": "no", "answer_type": "yesno", "aliases": ["no", "No"], "tags": ["memory-negation"], "hops": 1, "gold_support": ["m06-0-vent"], "distractors": ["m06-0-where"], "notes": [{"id": "m06-0-vent", "claim": "The night kiln does not need a large vent."}, {"id": "m06-0-where", "claim": "The night kiln stands in the east shed."}]}, {"question": "Who is the site dentist?", "answer": "Dr. Hale", "answer_type": "entity", "aliases": ["Dr. Hale", "Hale"], "tags": ["wrong-entity"], "hops": 1, "gold_support": ["m06-1-dentist"], "distractors": ["m06-1-hours"], "notes": [{"id": "m06-1-dentist", "claim": "The site dentist is Dr. Hale."}, {"id": "m06-1-hours", "claim": "The site clinic opens at 8."}]}]')

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
