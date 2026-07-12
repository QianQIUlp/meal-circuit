from __future__ import annotations

from typing import Any


MEAL_KEYS = ("breakfast", "lunch", "dinner")
MEAL_NAMES = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
MEAL_KEYS_BY_NAME = {value: key for key, value in MEAL_NAMES.items()}
PREPARATION_MODES = {"home_cook", "quick_assembly", "eat_out"}
LEGACY_DEFAULT_MEAL_MODES = {
    "breakfast": "quick_assembly",
    "lunch": "eat_out",
    "dinner": "home_cook",
}


def legacy_home_meal_modes(home: dict | None) -> dict[str, str]:
    home = home or {"enabled": False}
    if not home.get("enabled"):
        return dict(LEGACY_DEFAULT_MEAL_MODES)
    configured = home.get("meal_modes")
    if isinstance(configured, dict):
        return {key: str(configured.get(key) or LEGACY_DEFAULT_MEAL_MODES[key]) for key in MEAL_KEYS}
    scope = home.get("meal_scope")
    result = dict(LEGACY_DEFAULT_MEAL_MODES)
    if scope == "lunch_and_dinner":
        result["lunch"] = "home_cook"
    return result


def home_cooked_meal_names(settings: dict) -> list[str]:
    modes = settings.get("meal_modes") or legacy_home_meal_modes(settings.get("home_cooking"))
    return [MEAL_NAMES[key] for key in MEAL_KEYS if modes.get(key) == "home_cook"]


def meal_rotation(menu: dict, meal: dict) -> dict | None:
    rotation = meal.get("rotation")
    if isinstance(rotation, dict):
        return rotation
    if meal.get("name") == "晚餐":
        legacy = menu.get("rotation")
        return legacy if isinstance(legacy, dict) else None
    return None


def meal_modes_are_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(MEAL_KEYS)
        and all(value[key] in PREPARATION_MODES for key in MEAL_KEYS)
    )
