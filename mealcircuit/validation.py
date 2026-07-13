from __future__ import annotations

from typing import Any
from datetime import date, timedelta

from .meal_modes import MEAL_KEYS, MEAL_NAMES, legacy_home_meal_modes, meal_rotation


class ValidationError(ValueError):
    pass


VALIDATOR_VERSION = "validator-v2"


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


def validate_result(task_type: str, value: Any, *, fact_only: bool = False) -> dict:
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
        _text_list(obj.get("unknowns"), "unknowns")
        if fact_only:
            if "advice" in obj:
                raise ValidationError("事实型照片结果不得包含 advice 字段")
        else:
            _text_list(obj.get("advice"), "advice")
    elif task_type == "material":
        if fact_only:
            _text_list(obj.get("observed_items"), "observed_items")
            _text_list(obj.get("unknowns"), "unknowns")
            for forbidden in ("combinations", "minimal_adjustments"):
                if forbidden in obj:
                    raise ValidationError(f"事实型原材料结果不得包含 {forbidden} 字段")
        else:
            _text_list(obj.get("combinations"), "combinations")
        _validate_nutrition(obj.get("batch_nutrition"), "batch_nutrition")
        _validate_nutrition(obj.get("per_serving_nutrition"), "per_serving_nutrition")
        _text_list(obj.get("gaps"), "gaps")
        _text_list(obj.get("risks"), "risks")
        if not fact_only:
            _text_list(obj.get("minimal_adjustments"), "minimal_adjustments")
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
    if target is not None:
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

    home_cooking = (expected_settings or {}).get("home_cooking") or {"enabled": False}
    meal_modes = (expected_settings or {}).get("meal_modes") or legacy_home_meal_modes(home_cooking)
    if expected_settings and (expected_settings.get("meal_modes") or home_cooking.get("enabled")):
        meals_by_name = {meal["name"]: meal for meal in meals}
        for key in MEAL_KEYS:
            name, expected_mode = MEAL_NAMES[key], meal_modes[key]
            meal = meals_by_name[name]
            if meal.get("mode") != expected_mode:
                raise ValidationError(f"{name}.mode 必须是 {expected_mode}")
            if expected_mode != "home_cook" and meal.get("recipe_card") is not None:
                raise ValidationError(f"{name}不是在家下厨时不得包含 recipe_card")
            if expected_mode != "home_cook" and meal.get("rotation") is not None:
                raise ValidationError(f"{name}不是在家下厨时不得包含 rotation")
            if expected_mode == "eat_out":
                guidance = _required_object(meal.get("eat_out_guidance"), f"{name}.eat_out_guidance")
                for field in ("protein_anchor", "staple", "vegetables", "sauce_rule", "fallback"):
                    _required_text(guidance.get(field), f"{name}.eat_out_guidance.{field}")
            elif meal.get("eat_out_guidance") is not None:
                raise ValidationError(f"{name}不是外食时不得包含 eat_out_guidance")
    if home_cooking.get("enabled"):
        _validate_home_cooking_menu(menu, meals, home_cooking, menu_date)

    snack = _required_object(menu.get("conditional_snack"), "tomorrow_menu.conditional_snack")
    _required_text(snack.get("condition"), "tomorrow_menu.conditional_snack.condition")
    _text_list(snack.get("options"), "tomorrow_menu.conditional_snack.options", minimum=1)
    _required_text(menu.get("training_adjustment"), "tomorrow_menu.training_adjustment")
    _required_text(menu.get("gut_adjustment"), "tomorrow_menu.gut_adjustment")
    return obj


def _validate_home_cooking_menu(menu: dict, meals: list, settings: dict, menu_date: str) -> None:
    meals_by_name = {meal["name"]: meal for meal in meals}
    meal_modes = settings.get("meal_modes") or legacy_home_meal_modes(settings)
    home_meal_names = [MEAL_NAMES[key] for key in MEAL_KEYS if meal_modes[key] == "home_cook"]
    if not home_meal_names:
        raise ValidationError("home_cooking.enabled 时至少一个餐次必须为 home_cook")
    for meal_name in home_meal_names:
        _validate_home_recipe(meals_by_name[meal_name], meal_name, settings)
        _validate_meal_rotation(menu, meals_by_name[meal_name], meal_name)

    shopping = _required_list(menu.get("shopping_list"), "tomorrow_menu.shopping_list")
    if not shopping:
        raise ValidationError("tomorrow_menu.shopping_list 不能为空")
    for index, item in enumerate(shopping):
        item = _required_object(item, f"tomorrow_menu.shopping_list[{index}]")
        for field in ("name", "amount", "purpose", "selection_guide", "storage"):
            _required_text(item.get(field), f"tomorrow_menu.shopping_list[{index}].{field}")
        if not isinstance(item.get("required"), bool):
            raise ValidationError(f"tomorrow_menu.shopping_list[{index}].required 必须是布尔值")

    online = _required_list(menu.get("online_options"), "tomorrow_menu.online_options")
    if len(online) > 3:
        raise ValidationError("tomorrow_menu.online_options 最多允许 3 项")
    for index, item in enumerate(online):
        item = _required_object(item, f"tomorrow_menu.online_options[{index}]")
        for field in ("category", "package_size", "skip_if"):
            _required_text(item.get(field), f"tomorrow_menu.online_options[{index}].{field}")
        for field in ("selection_criteria", "search_keywords", "pairs_with"):
            _text_list(item.get(field), f"tomorrow_menu.online_options[{index}].{field}", minimum=1)

    reuse = _required_object(menu.get("reuse_plan"), "tomorrow_menu.reuse_plan")
    if reuse.get("horizon_days") != settings["rotation_window_days"]:
        raise ValidationError(f"tomorrow_menu.reuse_plan.horizon_days 必须是 {settings['rotation_window_days']}")
    reuse_items = _required_list(reuse.get("items"), "tomorrow_menu.reuse_plan.items")
    if not reuse_items:
        raise ValidationError("tomorrow_menu.reuse_plan.items 不能为空")
    start = date.fromisoformat(menu_date)
    end = start + timedelta(days=settings["rotation_window_days"] - 1)
    for index, item in enumerate(reuse_items):
        item = _required_object(item, f"tomorrow_menu.reuse_plan.items[{index}]")
        for field in ("ingredient", "tomorrow_use", "storage"):
            _required_text(item.get(field), f"tomorrow_menu.reuse_plan.items[{index}].{field}")
        later = _required_list(item.get("later_uses"), f"tomorrow_menu.reuse_plan.items[{index}].later_uses")
        if not later:
            raise ValidationError(f"tomorrow_menu.reuse_plan.items[{index}].later_uses 不能为空")
        for later_index, use in enumerate(later):
            use = _required_object(use, f"tomorrow_menu.reuse_plan.items[{index}].later_uses[{later_index}]")
            use_date_text = _required_text(use.get("date"), "reuse date")
            try:
                use_date = date.fromisoformat(use_date_text)
            except ValueError as exc:
                raise ValidationError("reuse date 必须是 YYYY-MM-DD") from exc
            if not start < use_date <= end:
                raise ValidationError("reuse date 必须位于明日之后的复用窗口内")
            _required_text(use.get("use"), "reuse use")


def _validate_home_recipe(meal: dict, meal_name: str, settings: dict) -> None:
    recipe = _required_object(meal.get("recipe_card"), f"{meal_name}.recipe_card")
    _required_text(recipe.get("title"), f"{meal_name}.recipe_card.title")
    if recipe.get("servings") != settings["servings"]:
        raise ValidationError(f"{meal_name}.recipe_card.servings 必须是 {settings['servings']}")
    active_minutes = recipe.get("active_minutes")
    total_minutes = recipe.get("total_minutes")
    for value, name in ((active_minutes, "active_minutes"), (total_minutes, "total_minutes")):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{meal_name}.recipe_card.{name} 必须是正整数")
    if active_minutes > total_minutes or total_minutes > settings["weekday_time_limit_minutes"]:
        raise ValidationError(f"{meal_name}总时间不得超过 {settings['weekday_time_limit_minutes']} 分钟")
    cookware = _text_list(recipe.get("cookware"), f"{meal_name}.recipe_card.cookware", minimum=1, maximum=2)
    if any(item not in settings["equipment"] for item in cookware):
        raise ValidationError(f"{meal_name}.recipe_card.cookware 包含配置外设备")
    ingredients = _required_list(recipe.get("ingredients"), f"{meal_name}.recipe_card.ingredients")
    if not ingredients:
        raise ValidationError(f"{meal_name}.recipe_card.ingredients 不能为空")
    for index, item in enumerate(ingredients):
        item = _required_object(item, f"{meal_name}.recipe_card.ingredients[{index}]")
        for field in ("name", "amount", "prep"):
            _required_text(item.get(field), f"{meal_name}.recipe_card.ingredients[{index}].{field}")
    seasonings = _required_list(recipe.get("seasonings"), f"{meal_name}.recipe_card.seasonings")
    if not seasonings:
        raise ValidationError(f"{meal_name}.recipe_card.seasonings 不能为空")
    for index, item in enumerate(seasonings):
        item = _required_object(item, f"{meal_name}.recipe_card.seasonings[{index}]")
        for field in ("name", "amount", "timing"):
            _required_text(item.get(field), f"{meal_name}.recipe_card.seasonings[{index}].{field}")
    steps = _required_list(recipe.get("steps"), f"{meal_name}.recipe_card.steps")
    if not steps:
        raise ValidationError(f"{meal_name}.recipe_card.steps 不能为空")
    for index, item in enumerate(steps):
        item = _required_object(item, f"{meal_name}.recipe_card.steps[{index}]")
        _required_text(item.get("instruction"), f"{meal_name}.recipe_card.steps[{index}].instruction")
        _required_text(item.get("heat"), f"{meal_name}.recipe_card.steps[{index}].heat")
        _required_text(item.get("done_signal"), f"{meal_name}.recipe_card.steps[{index}].done_signal")
        minutes = item.get("minutes")
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
            raise ValidationError(f"{meal_name}.recipe_card.steps[{index}].minutes 必须是正数")
    _text_list(recipe.get("failure_rescue"), f"{meal_name}.recipe_card.failure_rescue", minimum=1)
    _required_text(recipe.get("cleanup"), f"{meal_name}.recipe_card.cleanup")
    _required_text(recipe.get("gut_fallback"), f"{meal_name}.recipe_card.gut_fallback")


def _validate_meal_rotation(menu: dict, meal: dict, meal_name: str) -> None:
    rotation = _required_object(meal_rotation(menu, meal), f"{meal_name}.rotation")
    for field in ("dish_key", "primary_protein", "primary_vegetable", "flavor_profile", "technique"):
        _required_text(rotation.get(field), f"{meal_name}.rotation.{field}")
    repeat_reason = rotation.get("repeat_reason")
    if repeat_reason is not None and repeat_reason not in {
        "health_recovery", "ingredient_expiry", "shopping_constraint"
    }:
        raise ValidationError(f"{meal_name}.rotation.repeat_reason 无效")


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
