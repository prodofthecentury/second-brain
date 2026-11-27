import json as _json
TEMPLATE_ID = "m19"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Who manages the person that approves Hamilton travel?", "answer": "Mere Collins", "answer_type": "entity", "aliases": ["Mere Collins"], "tags": ["bridge"], "hops": 2, "gold_support": ["m19-0-approver", "m19-0-manager"], "distractors": ["m19-0-other"], "notes": [{"id": "m19-0-approver", "claim": "Hamilton travel is approved by Tai Martin."}, {"id": "m19-0-manager", "claim": "Tai Martin is managed by Mere Collins."}, {"id": "m19-0-other", "claim": "Dunedin travel is approved by Ruth Ben."}]}, {"question": "Who manages the person that approves Dunedin travel?", "answer": "Hana Scott", "answer_type": "entity", "aliases": ["Hana Scott"], "tags": ["bridge"], "hops": 2, "gold_support": ["m19-1-approver", "m19-1-manager"], "distractors": ["m19-1-other"], "notes": [{"id": "m19-1-approver", "claim": "Dunedin travel is approved by Ruth Ben."}, {"id": "m19-1-manager", "claim": "Ruth Ben is managed by Hana Scott."}, {"id": "m19-1-other", "claim": "Napier travel is approved by Iwi Cole."}]}]')

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
