from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Iterable


SEMANTIC_SIGNATURE_VERSION = "meal-semantic-v1"

PROTEIN_TERMS = {
    "chicken": ("鸡胸", "鸡腿", "鸡肉", "chicken"),
    "egg": ("鸡蛋", "蛋液", "滑蛋", "蛋花", "egg"),
    "shrimp": ("虾仁", "虾", "shrimp"),
    "white_fish": ("龙利鱼", "巴沙鱼", "鳕鱼", "鱼柳", "whitefish"),
    "beef": ("牛肉", "beef"),
    "pork": ("猪肉", "里脊", "pork"),
    "tofu": ("豆腐", "豆干", "tofu"),
    "dairy": ("酸奶", "牛奶", "奶酪", "yogurt", "milk"),
}
VEGETABLE_TERMS = {
    "cucumber": ("黄瓜", "cucumber"),
    "tomato": ("番茄", "西红柿", "tomato"),
    "broccoli": ("西兰花", "broccoli"),
    "mushroom": ("杏鲍菇", "蘑菇", "香菇", "mushroom"),
    "pepper": ("彩椒", "青椒", "辣椒", "pepper"),
    "leafy_green": ("小白菜", "菠菜", "生菜", "油菜", "leafygreen"),
}
FLAVOR_TERMS = {
    "garlic": ("蒜香", "蒜末", "大蒜", "garlic"),
    "ginger_scallion": ("姜葱", "姜片", "葱白", "葱绿", "gingerscallion"),
    "black_pepper": ("黑胡椒", "blackpepper"),
    "vinegar_sour": ("醋香", "香醋", "陈醋", "vinegar"),
    "chili": ("辣椒酱", "小米椒", "酸辣", "chili"),
    "tomato_umami": ("番茄", "西红柿", "tomato"),
    "miso": ("味增", "miso"),
}
TECHNIQUE_TERMS = {
    "stir_fry": ("翻炒", "快炒", "滑蛋", "炒至", "stirfry"),
    "simmer": ("煮开", "汤面", "汤中", "炖煮", "simmer", "soup"),
    "covered_braise": ("盖锅", "焖熟", "焖制", "braise"),
    "steam": ("蒸蛋", "清蒸", "steam"),
    "cold_mix": ("凉拌", "拍黄瓜", "拌醋", "coldmix"),
    "assemble": ("组装", "面包夹", "三明治", "assemble", "sandwich"),
}


def _text_values(meal: dict) -> list[str]:
    recipe = meal.get("recipe_card") or {}
    values = [str((recipe.get("title") or ""))]
    values.extend(str(item) for item in meal.get("foods") or [])
    for field in ("ingredients", "seasonings", "steps"):
        for item in recipe.get(field) or []:
            if not isinstance(item, dict):
                continue
            values.extend(str(value) for value in item.values() if isinstance(value, (str, int, float)))
    guidance = meal.get("eat_out_guidance") or {}
    values.extend(str(value) for value in guidance.values() if isinstance(value, str))
    return values


def normalize_text(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?", "", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*(?:g|kg|ml|l|克|千克|毫升|升|枚|个|拳|分钟|茶匙|汤匙)", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def _tags(text: str, vocabulary: dict[str, tuple[str, ...]]) -> list[str]:
    normalized = normalize_text(text)
    return sorted(
        key for key, terms in vocabulary.items()
        if any(normalize_text(term) in normalized for term in terms)
    )


def _named_items(meal: dict, field: str) -> list[str]:
    recipe = meal.get("recipe_card") or {}
    return sorted({
        normalize_text(item.get("name"))
        for item in recipe.get(field) or []
        if isinstance(item, dict) and normalize_text(item.get("name"))
    })


def semantic_signature(meal: dict) -> dict:
    recipe = meal.get("recipe_card") or {}
    all_text = " ".join(_text_values(meal))
    title = normalize_text(recipe.get("title") or " ".join(meal.get("foods") or []))
    ingredients = _named_items(meal, "ingredients")
    seasonings = _named_items(meal, "seasonings")
    steps = [
        normalize_text(item.get("instruction"))
        for item in recipe.get("steps") or []
        if isinstance(item, dict) and normalize_text(item.get("instruction"))
    ]
    guidance = meal.get("eat_out_guidance") or {}
    content = {
        "mode": str(meal.get("mode") or ""),
        "title": title,
        "foods": sorted(normalize_text(item) for item in meal.get("foods") or []),
        "ingredients": ingredients,
        "seasonings": seasonings,
        "steps": steps,
        "guidance": sorted(normalize_text(value) for value in guidance.values() if isinstance(value, str)),
    }
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "version": SEMANTIC_SIGNATURE_VERSION,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "title": title,
        "ingredients": ingredients,
        "proteins": _tags(all_text, PROTEIN_TERMS),
        "vegetables": _tags(all_text, VEGETABLE_TERMS),
        "flavors": _tags(all_text, FLAVOR_TERMS),
        "techniques": _tags(all_text, TECHNIQUE_TERMS),
        "content_text": normalize_text(" ".join(_text_values(meal))),
    }


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set, right_set = set(left), set(right)
    if not left_set and not right_set:
        return 1.0
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def compare_signatures(current: dict, previous: dict, *, home_cook: bool) -> dict:
    title_similarity = SequenceMatcher(None, current.get("title", ""), previous.get("title", "")).ratio()
    content_similarity = SequenceMatcher(
        None, current.get("content_text", ""), previous.get("content_text", "")
    ).ratio()
    ingredient_similarity = _jaccard(current.get("ingredients") or [], previous.get("ingredients") or [])
    has_ingredients = bool(current.get("ingredients") and previous.get("ingredients"))
    exact = bool(
        current.get("fingerprint") == previous.get("fingerprint")
        or (has_ingredients and title_similarity >= 0.92 and ingredient_similarity >= 0.75)
        or (has_ingredients and content_similarity >= 0.94 and ingredient_similarity >= 0.65)
    )
    dimensions = {
        "protein": _jaccard(current.get("proteins") or [], previous.get("proteins") or []),
        "vegetable": _jaccard(current.get("vegetables") or [], previous.get("vegetables") or []),
        "flavor": _jaccard(current.get("flavors") or [], previous.get("flavors") or []),
        "technique": _jaccard(current.get("techniques") or [], previous.get("techniques") or []),
    }
    populated = [value for key, value in dimensions.items() if current.get(f"{key}s") or previous.get(f"{key}s")]
    semantic_score = sum(populated) / len(populated) if populated else 0.0
    near = bool(home_cook and (
        semantic_score >= 0.75
        or (
            dimensions["protein"] >= 1.0
            and dimensions["flavor"] >= 1.0
            and dimensions["technique"] >= 1.0
            and ingredient_similarity >= 0.5
        )
    ))
    reasons = []
    if exact:
        reasons.append("完整菜单语义相同")
    if near:
        reasons.extend(
            name for name, score in dimensions.items() if score >= 1.0
        )
    return {
        "duplicate": exact or near,
        "exact": exact,
        "near": near,
        "title_similarity": round(title_similarity, 3),
        "content_similarity": round(content_similarity, 3),
        "ingredient_similarity": round(ingredient_similarity, 3),
        "semantic_score": round(semantic_score, 3),
        "dimensions": dimensions,
        "reasons": list(dict.fromkeys(reasons)),
    }
