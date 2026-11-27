import json as _json
TEMPLATE_ID = "m12"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "On which date does the Kestrel handover review fall?", "answer": "2026-03-15", "answer_type": "date", "tags": ["date-arithmetic"], "hops": 2, "gold_support": ["m12-0-start", "m12-0-span"], "distractors": ["m12-0-place"], "notes": [{"id": "m12-0-start", "claim": "The Kestrel handover was recorded on 2026-03-01."}, {"id": "m12-0-span", "claim": "The Kestrel handover review is held 14 days after the handover."}, {"id": "m12-0-place", "claim": "The Kestrel handover review is held in Hamilton."}]}, {"question": "On which date does the Tui handover review fall?", "answer": "2026-04-08", "answer_type": "date", "tags": ["date-arithmetic"], "hops": 2, "gold_support": ["m12-1-start", "m12-1-span"], "distractors": ["m12-1-place"], "notes": [{"id": "m12-1-start", "claim": "The Tui handover was recorded on 2026-04-01."}, {"id": "m12-1-span", "claim": "The Tui handover review is held 7 days after the handover."}, {"id": "m12-1-place", "claim": "The Tui handover review is held in Dunedin."}]}]')

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
