from __future__ import annotations

import json
from typing import Any

from .meal_modes import home_cooked_meal_names
from .validation import VALIDATOR_VERSION, ValidationError


PLANNING_POLICY_VERSION = "constrained-planning-v1"
RESULT_SCHEMA_VERSION = 2
MEAL_SLOTS = {"早餐": "breakfast", "午餐": "lunch", "晚餐": "dinner", "加餐": "snack"}
HARD_RULE_ACTIONS = {"max_active_minutes", "max_total_minutes", "max_cookware", "max_steps", "exclude_food"}


def compile_constraints(context: dict) -> list[dict]:
    policy = context.get("generation_policy") or {}
    if policy.get("allowed") is not True:
        raise ValidationError(policy.get("reason") or "当前安全策略不允许规划")
    settings = context.get("settings") or {}
    profile_row = context.get("active_profile") or {}
    profile = profile_row.get("profile_json") or {}
    constraints = profile.get("constraints") or {}
    home = settings.get("home_cooking") or {"enabled": False}
    compiled = [{
        "id": "safety-permission",
        "kind": "permission",
        "hard": True,
        "value": policy.get("safety_mode"),
        "source": policy.get("policy_version"),
    }, {
        "id": "meal-environment",
        "kind": "environment",
        "hard": True,
        "value": settings.get("meal_environment"),
        "source": "active_strategy",
    }]
    target = settings.get("protein_target_g")
    if target is not None:
        compiled.append({
            "id": "protein-target",
            "kind": "nutrition_target",
            "hard": True,
            "value": target,
            "source": (settings.get("sources") or {}).get("targets") or [],
        })
    exclusions = list(dict.fromkeys(
        (constraints.get("food_exclusions") or []) + (home.get("food_exclusions") or [])
    ))
    if exclusions:
        compiled.append({
            "id": "food-exclusions",
            "kind": "food_exclusions",
            "hard": True,
            "value": exclusions,
            "source": "confirmed_profile",
        })
    if home.get("enabled"):
        for meal_name in home_cooked_meal_names(settings):
            compiled.extend([{
                "id": f"home-time-limit:{meal_name}",
                "kind": "max_total_minutes",
                "hard": True,
                "value": home["weekday_time_limit_minutes"],
                "meal_name": meal_name,
                "source": "versioned_meal_mode_strategy",
            }, {
                "id": f"home-equipment:{meal_name}",
                "kind": "allowed_cookware",
                "hard": True,
                "value": home["equipment"],
                "meal_name": meal_name,
                "source": "versioned_meal_mode_strategy",
            }])
    for rule in context.get("confirmed_rules") or []:
        effect = rule.get("effect_json") or {}
        action = effect.get("action")
        compiled.append({
            "id": f"rule:{rule['id']}",
            "kind": action or "declared_rule",
            "hard": action in HARD_RULE_ACTIONS or rule.get("kind") == "constraint",
            "value": effect.get("value"),
            "meal_name": effect.get("meal_name"),
            "statement": rule.get("statement"),
            "source": "confirmed_rule",
            "rule_id": rule["id"],
        })
    experiment = context.get("active_experiment")
    if experiment:
        compiled.append({
            "id": f"experiment:{experiment['id']}",
            "kind": "experiment",
            "hard": False,
            "value": experiment.get("plan_json") or {},
            "source": "active_experiment",
        })
    inventory = context.get("inventory") or []
    if inventory:
        compiled.append({
            "id": "available-inventory",
            "kind": "inventory",
            "hard": False,
            "value": [{
                "id": item["id"], "name": item["name"], "amount": item.get("amount_text") or "",
                "expires_on": item.get("expires_on"),
            } for item in inventory],
            "source": "inventory_events",
        })
    for event in context.get("planning_answers") or []:
        if event.get("status") != "answered":
            continue
        key = event.get("question_key")
        if key == "tomorrow_training":
            compiled.append({
                "id": f"question:{event['id']}",
                "kind": "tomorrow_training",
                "hard": False,
                "value": event.get("answer_json"),
                "source": "adaptive_question",
            })
        elif key == "tomorrow_environment":
            compiled.append({
                "id": f"question:{event['id']}",
                "kind": "tomorrow_environment",
                "hard": False,
                "value": event.get("answer_json"),
                "source": "adaptive_question",
            })
    return compiled


def _meal_text(meal: dict) -> str:
    recipe = meal.get("recipe_card") or {}
    ingredients = recipe.get("ingredients") or []
    values = list(meal.get("foods") or [])
    values.extend(item.get("name", "") for item in ingredients if isinstance(item, dict))
    return " ".join(str(value).strip().lower() for value in values)


def _meal_execution(meal: dict) -> dict:
    execution = dict(meal.get("execution") or {})
    recipe = meal.get("recipe_card") or {}
    if recipe:
        execution.setdefault("active_minutes", recipe.get("active_minutes"))
        execution.setdefault("total_minutes", recipe.get("total_minutes"))
        execution.setdefault("cookware", recipe.get("cookware") or [])
    execution.setdefault("active_minutes", None)
    execution.setdefault("total_minutes", None)
    execution.setdefault("cookware", [])
    meal["execution"] = execution
    return execution


def validate_and_enrich_daily_result(result: dict, context: dict) -> dict:
    compiled = compile_constraints(context)
    menu = result.get("tomorrow_menu") or {}
    meals = menu.get("meals") or []
    meals_by_name = {meal.get("name"): meal for meal in meals if isinstance(meal, dict)}
    for meal in meals:
        if isinstance(meal, dict):
            _meal_execution(meal)
    for constraint in compiled:
        kind = constraint["kind"]
        if kind == "food_exclusions":
            for exclusion in constraint["value"]:
                needle = str(exclusion).strip().lower()
                if not needle:
                    continue
                for meal in meals:
                    if needle in _meal_text(meal):
                        raise ValidationError(f"计划包含已确认排除食品：{exclusion}")
        if kind in {"max_active_minutes", "max_total_minutes", "max_cookware", "max_steps"}:
            meal_name = constraint.get("meal_name") or "晚餐"
            meal = meals_by_name.get(meal_name)
            if not meal:
                raise ValidationError(f"确认规则要求检查{meal_name}，但计划中缺少该餐次")
            execution = _meal_execution(meal)
            if kind == "max_cookware":
                actual = len(execution["cookware"])
            elif kind == "max_steps":
                actual = len((meal.get("recipe_card") or {}).get("steps") or [])
                if actual == 0:
                    raise ValidationError(f"计划缺少可验证的 {meal_name}.steps")
            else:
                key = "active_minutes" if kind == "max_active_minutes" else "total_minutes"
                actual = execution.get(key)
                if actual is None:
                    raise ValidationError(f"计划缺少可验证的 {meal_name}.{key}")
            limit = constraint.get("value")
            if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
                raise ValidationError(f"确认规则 {constraint['id']} 缺少合法限制值")
            if actual > limit:
                raise ValidationError(f"计划违反确认规则：{meal_name} {kind} 实际 {actual}，上限 {limit}")
        if kind == "exclude_food":
            exclusion = str(constraint.get("value") or "").strip()
            if exclusion and any(exclusion.lower() in _meal_text(meal) for meal in meals):
                raise ValidationError(f"计划违反确认规则，包含：{exclusion}")
    result["result_schema_version"] = RESULT_SCHEMA_VERSION
    result["decision_trace"] = {
        "planning_policy_version": PLANNING_POLICY_VERSION,
        "safety_mode": (context.get("generation_policy") or {}).get("safety_mode"),
        "hard_constraint_ids": [item["id"] for item in compiled if item["hard"]],
        "soft_constraint_ids": [item["id"] for item in compiled if not item["hard"]],
        "confirmed_rule_ids": [item["rule_id"] for item in compiled if item.get("rule_id")],
        "context_hash": context.get("context_hash") or "",
        "validator_version": VALIDATOR_VERSION,
    }
    return result


def enrich_task_result(result: dict, context: dict) -> dict:
    result["result_schema_version"] = RESULT_SCHEMA_VERSION
    result["analysis_mode"] = "fact_only" if (context.get("generation_policy") or {}).get("fact_only") else "advisory"
    result["provenance"] = {
        "context_hash": context.get("context_hash") or "",
        "policy_version": (context.get("generation_policy") or {}).get("policy_version") or "",
        "validator_version": VALIDATOR_VERSION,
    }
    return result


def validate_rescue_result(value: Any, plan_item: dict, constraints: list[dict]) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("救场结果必须是对象")
    reason = value.get("reason")
    steps = value.get("steps")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("救场结果必须包含非空 reason")
    if not isinstance(steps, list) or not steps or any(not isinstance(item, str) or not item.strip() for item in steps):
        raise ValidationError("救场结果 steps 必须是非空文本数组")
    replacements = value.get("replacement_foods") or []
    if not isinstance(replacements, list) or any(not isinstance(item, str) or not item.strip() for item in replacements):
        raise ValidationError("replacement_foods 必须是文本数组")
    combined = " ".join(replacements).lower()
    for constraint in constraints:
        if constraint["kind"] == "food_exclusions":
            for exclusion in constraint["value"]:
                if str(exclusion).lower() in combined:
                    raise ValidationError(f"救场方案包含已确认排除食品：{exclusion}")
        if constraint["kind"] == "exclude_food" and str(constraint.get("value") or "").lower() in combined:
            raise ValidationError(f"救场方案违反确认规则：{constraint.get('value')}")
    value["result_schema_version"] = RESULT_SCHEMA_VERSION
    value["plan_item_id"] = plan_item["plan_item_id"]
    value["constraint_ids"] = [item["id"] for item in constraints]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
