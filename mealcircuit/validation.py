from __future__ import annotations

from typing import Any
from datetime import date


class ValidationError(ValueError):
    pass


def _required_object(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} 必须是对象")
    return value


def _required_list(value: Any, name: str) -> list:
    if not isinstance(value, list):
        raise ValidationError(f"{name} 必须是数组")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} 必须是非空文本")
    return value.strip()


def _validate_range(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) != 2:
        raise ValidationError(f"{name} 必须是 [最小值, 最大值] 或 null")
    low, high = value
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in value):
        raise ValidationError(f"{name} 区间必须是非负数字")
    if low > high:
        raise ValidationError(f"{name} 最小值不能大于最大值")


def _validate_nutrition(value: Any, name: str) -> None:
    obj = _required_object(value, name)
    for field in ("energy_kcal", "protein_g", "carbs_g", "fat_g"):
        if field not in obj:
            raise ValidationError(f"{name}.{field} 缺失")
        _validate_range(obj[field], f"{name}.{field}")


def validate_result(task_type: str, value: Any) -> dict:
    obj = _required_object(value, "结果")
    _required_text(obj.get("summary"), "summary")
    if task_type == "photo":
        candidates = _required_list(obj.get("candidates"), "candidates")
        if not candidates:
            raise ValidationError("candidates 不能为空；无法识别时也应提供 unknown 候选")
        for i, item in enumerate(candidates):
            item = _required_object(item, f"candidates[{i}]")
            _required_text(item.get("name"), f"candidates[{i}].name")
            _required_text(item.get("portion_range"), f"candidates[{i}].portion_range")
            confidence = item.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
                raise ValidationError(f"candidates[{i}].confidence 必须在 0 到 1 之间")
            _validate_nutrition(item.get("nutrition"), f"candidates[{i}].nutrition")
        _required_list(obj.get("unknowns"), "unknowns")
        _required_list(obj.get("advice"), "advice")
    elif task_type == "material":
        _required_list(obj.get("combinations"), "combinations")
        _validate_nutrition(obj.get("batch_nutrition"), "batch_nutrition")
        _validate_nutrition(obj.get("per_serving_nutrition"), "per_serving_nutrition")
        _required_list(obj.get("gaps"), "gaps")
        _required_list(obj.get("risks"), "risks")
        _required_list(obj.get("minimal_adjustments"), "minimal_adjustments")
    else:
        raise ValidationError(f"未知任务类型：{task_type}")
    return obj


def _text_list(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> list:
    items = _required_list(value, name)
    if len(items) < minimum:
        raise ValidationError(f"{name} 至少需要 {minimum} 项")
    if maximum is not None and len(items) > maximum:
        raise ValidationError(f"{name} 最多允许 {maximum} 项")
    for index, item in enumerate(items):
        _required_text(item, f"{name}[{index}]")
    return items


def validate_daily_review_result(value: Any, expected_settings: dict | None = None) -> dict:
    obj = _required_object(value, "每日复盘结果")
    status = _required_text(obj.get("system_status"), "system_status")
    if status not in {"stable", "observe", "adjust", "risk"}:
        raise ValidationError("system_status 必须是 stable、observe、adjust 或 risk")
    _text_list(obj.get("facts"), "facts", minimum=1)
    _text_list(obj.get("inferences"), "inferences")
    _text_list(obj.get("core_advice"), "core_advice", minimum=1, maximum=3)
    _text_list(obj.get("do_not_adjust"), "do_not_adjust")
    _text_list(obj.get("risk_signals"), "risk_signals")
    _required_text(obj.get("one_line_review"), "one_line_review")
    decisions = _required_list(obj.get("priority_food_decisions"), "priority_food_decisions")
    seen_food_ids = set()
    for index, decision in enumerate(decisions):
        decision = _required_object(decision, f"priority_food_decisions[{index}]")
        food_id = _required_text(decision.get("food_id"), f"priority_food_decisions[{index}].food_id")
        if food_id in seen_food_ids:
            raise ValidationError(f"priority_food_decisions 中重复食品ID：{food_id}")
        seen_food_ids.add(food_id)
        action = _required_text(decision.get("decision"), f"priority_food_decisions[{index}].decision")
        if action not in {"use", "skip"}:
            raise ValidationError(f"priority_food_decisions[{index}].decision 必须是 use 或 skip")
        _required_text(decision.get("reason"), f"priority_food_decisions[{index}].reason")

    menu = _required_object(obj.get("tomorrow_menu"), "tomorrow_menu")
    menu_date = _required_text(menu.get("date"), "tomorrow_menu.date")
    try:
        date.fromisoformat(menu_date)
    except ValueError as exc:
        raise ValidationError("tomorrow_menu.date 必须是 YYYY-MM-DD") from exc
    _required_text(menu.get("environment"), "tomorrow_menu.environment")
    target = menu.get("protein_target_g")
    _validate_range(target, "tomorrow_menu.protein_target_g")
    if expected_settings and target != expected_settings["protein_target_g"]:
        raise ValidationError(
            f"tomorrow_menu.protein_target_g 必须是 {expected_settings['protein_target_g']}"
        )
    if expected_settings and menu["environment"] != expected_settings["meal_environment"]:
        raise ValidationError(
            f"tomorrow_menu.environment 必须是 {expected_settings['meal_environment']}"
        )

    meals = _required_list(menu.get("meals"), "tomorrow_menu.meals")
    if len(meals) != 3:
        raise ValidationError("tomorrow_menu.meals 必须包含早餐、午餐、晚餐")
    names = []
    for index, meal in enumerate(meals):
        meal = _required_object(meal, f"tomorrow_menu.meals[{index}]")
        names.append(_required_text(meal.get("name"), f"tomorrow_menu.meals[{index}].name"))
        _text_list(meal.get("foods"), f"tomorrow_menu.meals[{index}].foods", minimum=1)
        _required_text(meal.get("portion_guidance"), f"tomorrow_menu.meals[{index}].portion_guidance")
        _validate_range(meal.get("protein_g"), f"tomorrow_menu.meals[{index}].protein_g")
        _text_list(meal.get("substitutions"), f"tomorrow_menu.meals[{index}].substitutions")
    if set(names) != {"早餐", "午餐", "晚餐"}:
        raise ValidationError("tomorrow_menu.meals 必须各包含一次早餐、午餐、晚餐")

    snack = _required_object(menu.get("conditional_snack"), "tomorrow_menu.conditional_snack")
    _required_text(snack.get("condition"), "tomorrow_menu.conditional_snack.condition")
    _text_list(snack.get("options"), "tomorrow_menu.conditional_snack.options", minimum=1)
    _required_text(menu.get("training_adjustment"), "tomorrow_menu.training_adjustment")
    _required_text(menu.get("gut_adjustment"), "tomorrow_menu.gut_adjustment")
    return obj


def nutrition_number(value: str | None, field: str) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是数字") from exc
    if number < 0:
        raise ValidationError(f"{field} 不能为负数")
    return number
