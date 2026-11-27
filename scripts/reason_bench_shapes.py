"""Independent gold formulas for public math items.

These formulas do not import the template modules. ``validate`` runs them
against the stored answer and the question text.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, Callable


class ShapeError(ValueError):
    pass


def _n(params: dict[str, Any], *names: str) -> list[int]:
    return [int(params[name]) for name in names]


def _f(*parts: int) -> Fraction:
    value: Fraction | int = parts[0]
    return Fraction(value)


FORMULAS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "bakery_bags": lambda p: (lambda rolls: (rolls - (rolls * 3) // 4) // 6)(p["trays"] * p["rolls"]),
    "half_then_add": lambda p: p["boxes"] * p["each"] // 2 + p["extra"],
    "share_fraction": lambda p: Fraction(p["pizzas"], p["people"]),
    "change_note": lambda p: p["paid"] - p["count"] * p["price"],
    "lowest_terms": lambda p: Fraction(p["n"], p["d"]),
    "sum_fractions": lambda p: Fraction(p["a"], p["b"]) + Fraction(p["c"], p["d"]),
    "pupils_bus": lambda p: p["pupils"] - (Fraction(p["pupils"], 2) + p["extra"]) - p["left_out"],
    "boxes_all_but": lambda p: p["boxes"] * p["each"] // 2 + p["extra"],
    "fax_boxes": lambda p: p["boxes"] * p["each"],
    "percent_twice": lambda p: Fraction(p["price"] * (100 - p["pct"]), 100) * Fraction(100 - p["pct"], 100),
    "fraction_diff": lambda p: Fraction(p["a"], p["b"]) - Fraction(p["c"], p["d"]),
    "fraction_of_sum": lambda p: Fraction(p["n"], p["a"] + p["b"]),
    "mixed_number": lambda p: p["whole"] + Fraction(p["n"], p["d"]),
    "fraction_product": lambda p: Fraction(p["a"], p["b"]) * Fraction(p["c"], p["d"]),
    "remainder_mod": lambda p: p["a"] % p["b"],
    "ratio_part": lambda p: p["total"] * p["num"] // (p["num"] + p["den"]),
    "inclusive_span": lambda p: abs(int(p["b"]) - int(p["a"])) + 1,
    "calls_apart": lambda p: p["gap"],
    "half_odd_fraction": lambda p: Fraction(p["boxes"] * p["each"], 2),
    "boxes_removed": lambda p: p["boxes"] * p["each"] - p["removed"],
    "rectangle_area": lambda p: p["length"] * p["width"],
    "rectangle_perimeter": lambda p: 2 * (p["length"] + p["width"]),
    "speed_time": lambda p: Fraction(p["distance"], p["speed"]),
    "unit_price": lambda p: p["count"] * p["price"],
    "tax_amount": lambda p: Fraction(p["price"] * p["pct"], 100),
    "discount_fee": lambda p: Fraction(p["price"] * (100 - p["pct"]), 100) + p["fee"],
    "resale_profit": lambda p: p["sold"] * p["sell"] - p["bought"] * p["buy"],
    "floor_bags": lambda p: p["items"] // p["per"],
    "age_gap": lambda p: p["older"] - p["younger"],
    "tile_count": lambda p: (p["length"] // p["tile"]) * (p["width"] // p["tile"]),
    "work_sum_fraction": lambda p: Fraction(1, p["a"]) + Fraction(1, p["b"]),
    "two_step_sum": lambda p: p["a"] + p["b"] + p["c"],
    "two_step_diff": lambda p: p["a"] - p["b"] - p["c"],
    "double_then_add": lambda p: p["a"] * 2 + p["b"],
    "triple_minus": lambda p: p["a"] * 3 - p["b"],
    "average_two": lambda p: (p["a"] + p["b"]) // 2,
    "weighted_two": lambda p: (p["a"] * p["wa"] + p["b"] * p["wb"]) // (p["wa"] + p["wb"]),
    "pages_left": lambda p: p["pages"] - p["per"] * p["days"],
    "inventory": lambda p: p["start"] + p["arrived"] - p["sold"],
    "split_tip": lambda p: (p["bill"] + p["bill"] * p["pct"] // 100) // p["people"],
    "map_scale": lambda p: p["cm"] * p["scale"],
    "fuel_left": lambda p: p["tank"] - p["per"] * p["hours"],
    "elevation": lambda p: p["start"] + p["up"] - p["down"],
    "reading_days": lambda p: (p["pages"] + p["per"] - 1) // p["per"],
    "queue_left": lambda p: p["waiting"] + p["joined"] - p["served"],
    "group_remainder": lambda p: p["students"] % p["size"],
    "dozen_plus": lambda p: p["dozen"] * 12 + p["extra"],
    "hours_to_minutes": lambda p: p["hours"] * 60 + p["minutes"],
    "square_diff": lambda p: p["a"] * p["a"] - p["b"] * p["b"],
    "product_plus": lambda p: p["a"] * p["b"] + p["c"],
    "quotient_floor": lambda p: p["a"] // p["b"],
    "successive_add": lambda p: p["a"] + p["b"] + p["b"],
    "percent_of": lambda p: p["base"] * p["pct"] // 100,
    "complement_percent": lambda p: p["whole"] - Fraction(p["whole"] * p["pct"], 100),
    "min_of_two_sums": lambda p: min(p["a"] + p["b"], p["c"] + p["d"]),
    "max_gap": lambda p: max(p["a"], p["b"]) - min(p["a"], p["b"]),
    "nested_half": lambda p: (p["a"] // 2) // 2,
    "add_then_half": lambda p: (p["a"] + p["b"]) // 2,
    "subtract_distractor": lambda p: p["held"] - p["removed"],
    "pair_sum": lambda p: p["left"] + p["right"],
}


def question_skeleton(text: str) -> str:
    masked = re.sub(r"\d+", "#", text or "")
    masked = re.sub(r"\s+", " ", masked).strip().casefold()
    return masked


def silent_floor(shape: str, params: dict[str, Any]) -> bool:
    """True when an integer reading would hide a non-integral exact value."""
    if shape == "pupils_bus" and int(params["pupils"]) % 2:
        return True
    if shape == "tax_amount":
        return Fraction(int(params["price"]) * int(params["pct"]), 100).denominator != 1
    if shape == "percent_twice":
        step = Fraction(int(params["price"]) * (100 - int(params["pct"])), 100)
        return (step * Fraction(100 - int(params["pct"]), 100)).denominator != 1
    if shape == "discount_fee":
        return Fraction(int(params["price"]) * (100 - int(params["pct"])), 100).denominator != 1
    if shape == "complement_percent":
        return Fraction(int(params["whole"]) * int(params["pct"]), 100).denominator != 1
    if shape == "speed_time":
        return Fraction(int(params["distance"]), int(params["speed"])).denominator != 1
    if shape in {"percent_of", "split_tip"}:
        return Fraction(int(params.get("base", params.get("bill", 0))) * int(params["pct"]), 100).denominator != 1
    return False


def independent_value(item: dict[str, Any]) -> Any:
    shape = str(item.get("shape") or "")
    if shape not in FORMULAS:
        raise ShapeError(f"{item.get('id')} has no independent shape {shape!r}")
    params = dict(item.get("shape_params") or {})
    question = str(item.get("question") or "")
    if shape != "missing_info":
        for name, value in params.items():
            token = str(value)
            if token and token not in question and token.lstrip("0") not in question:
                raise ShapeError(f"{item.get('id')} question omits shape param {name}={value}")
    return FORMULAS[shape](params)


def values_match(got: Any, gold: Any) -> bool:
    if got is None or gold is None:
        return got is None and gold is None
    if isinstance(got, int) and not isinstance(got, bool):
        return str(got) == str(gold) or got == gold
    if isinstance(got, Fraction):
        text = str(gold)
        if "/" in text:
            num, den = text.split("/", 1)
            return got == Fraction(int(num), int(den))
        return got == Fraction(str(gold))
    return str(got) == str(gold)
