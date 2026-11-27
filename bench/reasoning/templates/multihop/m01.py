import json as _json
TEMPLATE_ID = "m01"
SUITE = "multihop"
ANSWER_TYPE = "entity"
INSTANCES = 2
SKIP_NUMBER_CHECK = True
WORLDS = _json.loads('[{"question": "Which office does the current lead of Project Kestrel work from?", "answer": "Hamilton", "answer_type": "entity", "aliases": ["Hamilton", "Hamilton office", "the Hamilton office"], "tags": ["temporal-supersession"], "hops": 2, "gold_support": ["kestrel-lead-2026", "ana-folau-office"], "distractors": ["kestrel-lead-2025", "tom-reid-office"], "support_spans": {"kestrel-lead-2026": "led by Ana Folau", "ana-folau-office": "works from the Hamilton office"}, "notes": [{"id": "kestrel-lead-2025", "claim": "Tom Reid led Project Kestrel from January 2025 until he handed it over on 2026-03-01.", "valid_from": "2025-01-01", "valid_until": "2026-03-01"}, {"id": "kestrel-lead-2026", "claim": "Since 2026-03-01 Project Kestrel has been led by Ana Folau.", "valid_from": "2026-03-01"}, {"id": "ana-folau-office", "claim": "Ana Folau works from the Hamilton office."}, {"id": "tom-reid-office", "claim": "Tom Reid works from the Wellington office."}]}, {"question": "Which office does the current lead of Project Tui work from?", "answer": "Dunedin", "answer_type": "entity", "aliases": ["Dunedin", "Dunedin office"], "tags": ["temporal-supersession"], "hops": 2, "gold_support": ["m01-1-lead", "m01-1-office"], "distractors": ["m01-1-old-lead", "m01-1-old-office"], "notes": [{"id": "m01-1-old-lead", "claim": "Ngaio Berg led Project Tui until she handed it over on 2026-04-01.", "valid_from": "2025-01-01", "valid_until": "2026-04-01"}, {"id": "m01-1-lead", "claim": "Since 2026-04-01 Project Tui has been led by Hemi Ruru.", "valid_from": "2026-04-01"}, {"id": "m01-1-office", "claim": "Hemi Ruru works from the Dunedin office."}, {"id": "m01-1-old-office", "claim": "Ngaio Berg works from the Nelson office."}]}]')

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
