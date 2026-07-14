from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

from .configuration import load_settings
from .db import connect, init_db, row_dict
from .meal_modes import (
    LEGACY_DEFAULT_MEAL_MODES,
    MEAL_KEYS,
    PREPARATION_MODES,
    legacy_home_meal_modes,
    meal_modes_are_valid,
)
from .storage import profile_path
from .validation import ValidationError


TARGET_POLICY_VERSION = "target-policy-v2"
ONBOARDING_STEPS = (
    "welcome",
    "goals",
    "baseline",
    "safety",
    "training",
    "constraints",
    "review",
)
GOAL_TYPES = {
    "fat_loss",
    "muscle_gain",
    "body_recomposition",
    "performance",
    "maintenance",
    "eating_consistency",
    "general_wellbeing",
    "custom",
}
LIFE_STAGES = {"adult", "pregnant", "breastfeeding", "minor", "other"}
PHYSIOLOGICAL_INPUTS = {"male", "female", "unspecified"}
ACTIVITY_LEVELS = {"low", "moderate", "high", "very_high"}
ACTIVITY_BANDS = {
    "low": (1.2, 1.4),
    "moderate": (1.4, 1.6),
    "high": (1.6, 1.8),
}
OBSERVATION_FLAGS = (
    "therapeutic_diet",
    "medication_affects_nutrition",
    "eating_disorder_risk",
    "rapid_unexplained_change",
    "severe_persistent_symptoms",
    "severe_allergy_management",
)


def _meal_modes(value: object | None) -> dict[str, str]:
    if value is None:
        return dict(LEGACY_DEFAULT_MEAL_MODES)
    payload = _object(value, "逐餐准备方式")
    if not meal_modes_are_valid(payload):
        raise ValidationError("必须分别指定早餐、午餐和晚餐的准备方式")
    return {key: str(payload[key]) for key in MEAL_KEYS}


def _strategy_home_cooking(profile: dict, legacy_settings: dict | None) -> dict:
    constraints = profile["constraints"]
    modes = constraints["meal_modes"]
    home_meals = [key for key in MEAL_KEYS if modes[key] == "home_cook"]
    if not home_meals:
        return {"enabled": False, "meal_modes": modes}
    legacy = ((legacy_settings or {}).get("home_cooking") or {})
    equipment = constraints.get("equipment") or legacy.get("equipment") or []
    if not equipment:
        raise ValidationError("选择在家下厨时必须至少填写一种可用厨具")
    time_limit = constraints.get("cooking_time_minutes", 25)
    if not 10 <= time_limit <= 60:
        raise ValidationError("选择在家下厨时，做饭时间必须在 10–60 分钟之间")
    scope = (
        "dinner" if home_meals == ["dinner"] else
        "lunch_and_dinner" if home_meals == ["lunch", "dinner"] else
        "custom"
    )
    return {
        "enabled": True,
        "region": legacy.get("region", "china"),
        "meal_scope": scope,
        "meal_modes": modes,
        "servings": 1,
        "weekday_time_limit_minutes": time_limit,
        "equipment": equipment,
        "recipe_detail": "beginner_card",
        "rotation_window_days": legacy.get("rotation_window_days", 3),
        "reuse_policy": "reuse_ingredients_rotate_dishes",
        "flavor_preferences": constraints.get("preferences") or legacy.get("flavor_preferences") or [],
        "online_purchase_mode": "spec_and_search_keywords",
        "food_exclusions": constraints.get("food_exclusions") or legacy.get("food_exclusions") or [],
    }
CLINICIAN_GUIDED_FLAGS = {
    "therapeutic_diet",
    "medication_affects_nutrition",
    "severe_allergy_management",
}
HALT_FLAGS = {
    "eating_disorder_risk",
    "rapid_unexplained_change",
    "severe_persistent_symptoms",
}
SAFETY_MODES = {"setup_required", "observation", "standard", "clinician_guided", "halt_and_refer"}
TARGET_SOURCE_KINDS = {
    "policy_estimate",
    "user_confirmed_suggestion",
    "clinician_provided",
    "legacy_import",
    "observed_calibration",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} 必须是对象")
    return value


def _text(value: object, name: str, *, required: bool = True, maximum: int = 1000) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError(f"{name} 必须是非空文本" if required else f"{name} 必须是文本")
    clean = value.strip()
    if len(clean) > maximum:
        raise ValidationError(f"{name} 不能超过 {maximum} 字")
    return clean


def _number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
    required: bool = True,
) -> float | None:
    if value in (None, "") and not required:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} 必须是数字") from exc
    if isinstance(value, bool) or not minimum <= number <= maximum:
        raise ValidationError(f"{name} 必须在 {minimum:g}–{maximum:g} 之间")
    return round(number, 1)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} 必须是布尔值")
    return value


def _text_list(value: object, name: str, *, allowed: set[str] | None = None, maximum: int = 20) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError(f"{name} 必须是最多 {maximum} 项的数组")
    result = []
    for index, item in enumerate(value):
        clean = _text(item, f"{name}[{index}]", maximum=100)
        if allowed is not None and clean not in allowed:
            raise ValidationError(f"{name} 包含无效值：{clean}")
        if clean not in result:
            result.append(clean)
    return result


def _date_text(value: object, name: str, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    clean = _text(value, name, required=required, maximum=10)
    try:
        date.fromisoformat(clean)
    except ValueError as exc:
        raise ValidationError(f"{name} 必须是 YYYY-MM-DD") from exc
    return clean


def _professional_guidance(value: object) -> dict:
    if value is None:
        return {
            "confirmed": False,
            "source": "",
            "summary": "",
            "confirmed_on": None,
            "valid_until": None,
        }
    payload = _object(value, "专业指导")
    confirmed = _bool(payload.get("confirmed", False), "professional_guidance.confirmed")
    source = _text(payload.get("source", ""), "专业指导来源", required=False, maximum=300)
    summary = _text(payload.get("summary", ""), "专业指导摘要", required=False, maximum=3000)
    confirmed_on = _date_text(payload.get("confirmed_on"), "专业指导确认日期")
    valid_until = _date_text(payload.get("valid_until"), "专业指导有效期")
    if confirmed and (not source or not summary or not confirmed_on):
        raise ValidationError("已确认的专业指导必须包含来源、摘要和确认日期")
    if valid_until and confirmed_on and valid_until < confirmed_on:
        raise ValidationError("专业指导有效期不能早于确认日期")
    return {
        "confirmed": confirmed,
        "source": source,
        "summary": summary,
        "confirmed_on": confirmed_on,
        "valid_until": valid_until,
    }


def _legacy_prefill() -> dict:
    result: dict[str, Any] = {}
    try:
        settings = load_settings()
    except (ValidationError, OSError):
        settings = None
    if settings:
        home = settings.get("home_cooking") or {"enabled": False}
        result["constraints"] = {
            "meal_environment": settings["meal_environment"],
            "portion_method": settings["portion_method"],
            "meal_modes": legacy_home_meal_modes(home),
            "cooking_time_minutes": home.get("weekday_time_limit_minutes", 25),
            "equipment": home.get("equipment") or [],
            "food_exclusions": home.get("food_exclusions") or [],
            "preferences": home.get("flavor_preferences") or [],
            "question_budget": 2,
        }
        result["legacy_settings"] = settings
    path = profile_path()
    if path.is_file():
        content = path.read_text(encoding="utf-8").strip()
        if content:
            result["legacy_profile_notes"] = content
    return result


def _active_prefill() -> dict | None:
    with connect() as conn:
        profile_row = row_dict(conn.execute(
            "SELECT * FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone())
        if not profile_row:
            return None
        goal_rows = [row_dict(row) for row in conn.execute(
            "SELECT * FROM goal_versions WHERE active=1 ORDER BY created_at,id"
        ).fetchall()]
    profile = profile_row["profile_json"]
    goal_rows.sort(key=lambda item: item["goal_json"].get("priority", 99))
    primary = goal_rows[0]["goal_json"] if goal_rows else {}
    secondary = [item["goal_json"]["type"] for item in goal_rows[1:]]
    safety = {
        "life_stage": profile.get("life_stage", "adult"),
        **{flag: bool(profile.get(flag)) for flag in OBSERVATION_FLAGS},
        "professional_guidance": profile.get("professional_guidance") or _professional_guidance(None),
    }
    legacy_prefill = _legacy_prefill()
    return {
        **legacy_prefill,
        "welcome": {"privacy_ack": True},
        "goals": {
            "primary_goal": primary.get("type", "general_wellbeing"),
            "secondary_goals": secondary,
            "motivation": primary.get("motivation", ""),
            "success_metrics": primary.get("success_metrics") or ["execution_rate"],
            "target_weight_kg": primary.get("target_weight_kg"),
            "horizon": primary.get("horizon", ""),
        },
        "baseline": {
            "age_years": profile.get("age_years"),
            "height_cm": profile.get("height_cm"),
            "weight_kg": profile.get("weight_kg"),
            "physiological_input": profile.get("physiological_input", "unspecified"),
            "activity_level": profile.get("activity_level", "moderate"),
        },
        "safety": safety,
        "training": profile.get("training") or {"types": [], "frequency_per_week": 0},
        "constraints": {
            **(legacy_prefill.get("constraints") or {}),
            **(profile.get("constraints") or {}),
        },
        "legacy_profile_notes": profile.get("legacy_profile_notes", ""),
    }


def onboarding_status() -> dict:
    init_db()
    with connect() as conn:
        profile = row_dict(conn.execute(
            "SELECT * FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone())
        session = row_dict(conn.execute(
            "SELECT * FROM onboarding_sessions WHERE status='in_progress' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone())
    if profile:
        return {
            "status": "completed",
            "safety_mode": profile.get("safety_policy_mode") or profile["safety_mode"],
            "profile_version": profile["version"],
            "session": session,
        }
    return {"status": "setup_required", "safety_mode": "setup_required", "profile_version": None, "session": session}


def start_onboarding() -> dict:
    init_db()
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM onboarding_sessions WHERE status='in_progress' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if existing:
            return row_dict(existing)
        timestamp = _now()
        session_id = _id("onboarding")
        prefill = _active_prefill() or _legacy_prefill()
        conn.execute(
            """INSERT INTO onboarding_sessions(
                   id,status,current_step,answers_json,version,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (session_id, "in_progress", "welcome", json.dumps(prefill, ensure_ascii=False), 0, timestamp, timestamp),
        )
    return get_onboarding(session_id)


def get_onboarding(session_id: str) -> dict:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM onboarding_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise KeyError(session_id)
    return row_dict(row)


def save_onboarding_step(session_id: str, step: str, payload: dict, expected_version: int) -> dict:
    if step not in ONBOARDING_STEPS:
        raise ValidationError("未知的初始化步骤")
    payload = _object(payload, "初始化答案")
    _validate_onboarding_step(step, payload)
    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM onboarding_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        if row["status"] != "in_progress":
            raise ValidationError("该初始化会话已结束")
        if row["version"] != expected_version:
            raise ValidationError("初始化答案已变化，请刷新后重试")
        answers = json.loads(row["answers_json"] or "{}")
        answers[step] = payload
        updated = conn.execute(
            """UPDATE onboarding_sessions SET current_step=?,answers_json=?,version=version+1,updated_at=?
               WHERE id=? AND status='in_progress' AND version=?""",
            (step, json.dumps(answers, ensure_ascii=False), timestamp, session_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ValidationError("初始化状态已变化，请刷新后重试")
    return get_onboarding(session_id)


def _validate_onboarding_step(step: str, payload: dict) -> None:
    if step == "welcome":
        if payload.get("privacy_ack") is not True:
            raise ValidationError("必须确认本地存储与模型发送边界")
    elif step == "goals":
        _validate_goals({"goals": payload})
    elif step == "baseline":
        _number(payload.get("age_years"), "年龄", minimum=13, maximum=120)
        _number(payload.get("height_cm"), "身高", minimum=100, maximum=250, required=False)
        _number(payload.get("weight_kg"), "体重", minimum=20, maximum=400, required=False)
        physiological = _text(payload.get("physiological_input", "unspecified"), "能量估算生理参数", maximum=20)
        activity = _text(payload.get("activity_level", "moderate"), "活动水平", maximum=20)
        if physiological not in PHYSIOLOGICAL_INPUTS or activity not in ACTIVITY_LEVELS:
            raise ValidationError("基线选项无效")
    elif step == "safety":
        life_stage = _text(payload.get("life_stage"), "生命阶段", maximum=30)
        if life_stage not in LIFE_STAGES:
            raise ValidationError("生命阶段无效")
        missing = [flag for flag in OBSERVATION_FLAGS if flag not in payload]
        if missing:
            raise ValidationError(f"安全步骤缺少明确回答：{missing}")
        for flag in OBSERVATION_FLAGS:
            _bool(payload[flag], flag)
        _professional_guidance(payload.get("professional_guidance"))
    elif step == "training":
        _text_list(payload.get("types"), "训练类型", allowed={"strength", "cardio", "sport", "mobility", "other"})
        _number(payload.get("frequency_per_week", 0), "每周训练次数", minimum=0, maximum=14)
    elif step == "constraints":
        _text(payload.get("meal_environment"), "用餐环境", maximum=300)
        _text(payload.get("portion_method"), "份量表达", maximum=200)
        modes = _meal_modes(payload.get("meal_modes")) if "meal_modes" in payload else None
        _number(payload.get("cooking_time_minutes", 25), "做饭时间", minimum=0, maximum=180)
        _number(payload.get("question_budget", 2), "每日问题预算", minimum=0, maximum=5)
        for name in ("equipment", "food_exclusions", "preferences"):
            _text_list(payload.get(name), name)
        if modes and "home_cook" in modes.values() and not payload.get("equipment"):
            raise ValidationError("选择在家下厨时必须至少填写一种可用厨具")


def _validate_profile(answers: dict) -> dict:
    welcome = _object(answers.get("welcome"), "欢迎步骤")
    if welcome.get("privacy_ack") is not True:
        raise ValidationError("必须确认本地存储与模型发送边界")
    baseline = _object(answers.get("baseline"), "基线步骤")
    safety = _object(answers.get("safety"), "安全步骤")
    training = _object(answers.get("training") or {}, "训练步骤")
    constraints = _object(answers.get("constraints") or {}, "现实约束步骤")

    age = int(_number(baseline.get("age_years"), "年龄", minimum=13, maximum=120))
    life_stage = _text(safety.get("life_stage"), "生命阶段", maximum=30)
    if life_stage not in LIFE_STAGES:
        raise ValidationError("生命阶段无效")
    physiological = _text(baseline.get("physiological_input", "unspecified"), "能量估算生理参数", maximum=20)
    if physiological not in PHYSIOLOGICAL_INPUTS:
        raise ValidationError("能量估算生理参数无效")
    activity_level = _text(baseline.get("activity_level", "moderate"), "活动水平", maximum=20)
    if activity_level not in ACTIVITY_LEVELS:
        raise ValidationError("活动水平无效")
    missing_safety = [flag for flag in OBSERVATION_FLAGS if flag not in safety]
    if missing_safety:
        raise ValidationError(f"安全步骤缺少明确回答：{missing_safety}")
    question_budget = int(_number(
        constraints.get("question_budget", 2), "每日问题预算", minimum=0, maximum=5
    ))
    meal_modes = _meal_modes(constraints.get("meal_modes")) if "meal_modes" in constraints else None
    equipment = _text_list(constraints.get("equipment"), "炊具")
    food_exclusions = _text_list(constraints.get("food_exclusions"), "排除食品")
    preferences = _text_list(constraints.get("preferences"), "饮食偏好")

    profile = {
        "age_years": age,
        "height_cm": _number(baseline.get("height_cm"), "身高", minimum=100, maximum=250, required=False),
        "weight_kg": _number(baseline.get("weight_kg"), "体重", minimum=20, maximum=400, required=False),
        "physiological_input": physiological,
        "activity_level": activity_level,
        "life_stage": life_stage,
        **{flag: _bool(safety[flag], flag) for flag in OBSERVATION_FLAGS},
        "professional_guidance": _professional_guidance(safety.get("professional_guidance")),
        "training": {
            "types": _text_list(
                training.get("types"), "训练类型", allowed={"strength", "cardio", "sport", "mobility", "other"}
            ),
            "frequency_per_week": int(_number(
                training.get("frequency_per_week", 0), "每周训练次数", minimum=0, maximum=14
            )),
        },
        "constraints": {
            "meal_environment": _text(
                constraints.get("meal_environment", "未指定；按可执行的普通环境处理"),
                "用餐环境",
                maximum=300,
            ),
            "portion_method": _text(
                constraints.get("portion_method", "手掌与拳头份量法"), "份量表达", maximum=200
            ),
            "cooking_time_minutes": int(_number(
                constraints.get("cooking_time_minutes", 25), "做饭时间", minimum=0, maximum=180
            )),
            "equipment": equipment,
            "food_exclusions": food_exclusions,
            "preferences": preferences,
            "question_budget": question_budget,
        },
        "legacy_profile_notes": _text(
            answers.get("legacy_profile_notes", ""), "旧档案备注", required=False, maximum=20000
        ),
    }
    if meal_modes is not None:
        profile["constraints"]["meal_modes"] = meal_modes
    return profile


def _validate_goals(answers: dict) -> list[dict]:
    payload = _object(answers.get("goals"), "目标步骤")
    primary = _text(payload.get("primary_goal"), "主目标", maximum=50)
    if primary not in GOAL_TYPES:
        raise ValidationError("主目标无效")
    secondary = _text_list(payload.get("secondary_goals"), "次目标", allowed=GOAL_TYPES)
    secondary = [item for item in secondary if item != primary]
    metrics = _text_list(payload.get("success_metrics"), "成功指标", maximum=8)
    if not metrics:
        raise ValidationError("请至少选择一项成功指标")
    target_weight = _number(payload.get("target_weight_kg"), "目标体重", minimum=20, maximum=400, required=False)
    custom_label = _text(
        payload.get("custom_goal_text", ""), "自定义目标",
        required=primary == "custom", maximum=300,
    )
    result = [{
        "key": "primary",
        "type": primary,
        "priority": 1,
        "motivation": _text(payload.get("motivation", ""), "目标动机", required=False, maximum=1000),
        "success_metrics": metrics,
        "target_weight_kg": target_weight,
        "horizon": _text(payload.get("horizon", ""), "目标周期", required=False, maximum=100),
        "custom_label": custom_label,
    }]
    for index, goal_type in enumerate(secondary, start=2):
        result.append({"key": f"secondary_{index - 1}", "type": goal_type, "priority": index, "success_metrics": metrics})
    return result


def safety_assessment(profile: dict) -> dict:
    flags = []
    if profile.get("age_years", 0) < 18 or profile.get("life_stage") == "minor":
        flags.append("minor")
    if profile.get("life_stage") in {"pregnant", "breastfeeding"}:
        flags.append(profile["life_stage"])
    flags.extend(flag for flag in OBSERVATION_FLAGS if profile.get(flag))
    if any(flag in HALT_FLAGS for flag in flags):
        mode = "halt_and_refer"
    elif (
        any(flag in CLINICIAN_GUIDED_FLAGS for flag in flags)
        or any(flag in {"minor", "pregnant", "breastfeeding"} for flag in flags)
    ):
        mode = "clinician_guided"
    else:
        mode = "standard"
    guidance = profile.get("professional_guidance") or {}
    guidance_current = bool(
        guidance.get("confirmed")
        and guidance.get("confirmed_on")
        and guidance["confirmed_on"] <= date.today().isoformat()
    )
    if guidance.get("valid_until") and guidance["valid_until"] < date.today().isoformat():
        guidance_current = False
    return {
        "mode": mode,
        "flags": flags,
        "policy_version": TARGET_POLICY_VERSION,
        "professional_guidance_current": guidance_current,
        "allowed_actions": (
            ["record", "fact_only_photo", "fact_only_material", "trend", "professional_questions"]
            if mode in {"observation", "halt_and_refer"}
            else (
                ["record", "fact_only_photo", "fact_only_material", "trend", "professional_questions", "daily_plan", "rescue"]
                if mode == "clinician_guided" and guidance_current
                else (
                    ["record", "fact_only_photo", "fact_only_material", "trend", "professional_questions"]
                    if mode == "clinician_guided"
                    else ["record", "photo", "material", "daily_plan", "adaptation", "rescue"]
                )
            )
        ),
    }


def target_assessment(profile: dict, goals: list[dict]) -> dict:
    safety = safety_assessment(profile)
    result: dict[str, Any] = {
        "policy_version": TARGET_POLICY_VERSION,
        "safety_mode": safety["mode"],
        "planning_default": (
            "portion_guided"
            if safety["mode"] == "standard"
            else ("clinician_guided" if safety["mode"] == "clinician_guided" and safety["professional_guidance_current"] else "observation")
        ),
        "resting_energy_estimate_kcal": None,
        "maintenance_energy_estimate_kcal": None,
        "energy_estimate_provenance": None,
        "protein_candidates": [],
        "notes": [],
    }
    if safety["mode"] != "standard":
        result["notes"].append("当前不是普通成人自主规划模式，不自动计算能量或蛋白目标。")
        return result

    age = profile.get("age_years")
    height = profile.get("height_cm")
    weight = profile.get("weight_kg")
    physiological = profile.get("physiological_input")
    activity = profile.get("activity_level")
    if 19 <= age <= 78 and height and weight and physiological in {"male", "female"}:
        offset = 5 if physiological == "male" else -161
        resting = round(10 * weight + 6.25 * height - 5 * age + offset)
        result["resting_energy_estimate_kcal"] = resting
        result["energy_estimate_provenance"] = {
            "source_kind": "policy_estimate",
            "method": "mifflin_st_jeor_1990",
            "applicability": "healthy_adults_age_19_78",
            "uncertainty_note": "公式只能估算群体范围，个体误差可能显著；不得直接视为摄入处方。",
            "policy_version": TARGET_POLICY_VERSION,
        }
        if activity in ACTIVITY_BANDS:
            low, high = ACTIVITY_BANDS[activity]
            result["maintenance_energy_estimate_kcal"] = [round(resting * low), round(resting * high)]
        else:
            result["notes"].append("极高活动量不使用通用活动系数，请录入专业或实测目标。")
    else:
        result["notes"].append("能量估算参数缺失或超出公式样本边界；继续使用份量法。")

    primary = goals[0]
    training_types = set((profile.get("training") or {}).get("types") or [])
    if primary["type"] in {"muscle_gain", "body_recomposition"} or "strength" in training_types:
        factor = [1.4, 2.0]
        basis = "力量、增肌或减脂增肌候选范围"
    elif training_types & {"cardio", "sport"}:
        factor = [1.2, 1.6]
        basis = "耐力或混合训练候选范围"
    else:
        factor = [0.8, 1.0]
        basis = "普通成人候选范围"
    if weight:
        current_candidate = {
            "candidate_id": "protein_current_weight",
            "reference": "current_weight",
            "reference_weight_kg": weight,
            "target_g": [round(weight * factor[0]), round(weight * factor[1])],
            "factor_g_per_kg": factor,
            "basis": basis,
            "source_kind": "policy_estimate",
            "method": "goal_and_training_factor",
            "applicability": "standard_mode_healthy_adult",
            "policy_version": TARGET_POLICY_VERSION,
        }
        result["protein_candidates"].append(current_candidate)
        target_weight = primary.get("target_weight_kg")
        if target_weight and abs(target_weight - weight) / weight > 0.15:
            result["protein_candidates"].append({
                "candidate_id": "protein_goal_weight",
                "reference": "goal_weight",
                "reference_weight_kg": target_weight,
                "target_g": [round(target_weight * factor[0]), round(target_weight * factor[1])],
                "factor_g_per_kg": factor,
                "basis": f"{basis}；当前与目标体重差异超过15%，需要用户选择参考体重",
                "source_kind": "policy_estimate",
                "method": "goal_and_training_factor",
                "applicability": "standard_mode_healthy_adult",
                "policy_version": TARGET_POLICY_VERSION,
            })
            result["notes"].append("当前体重与目标体重差异超过15%，不会自动选择蛋白参考体重。")
    else:
        result["notes"].append("缺少体重，蛋白目标保持未知。")
    return result


def onboarding_preview(session_id: str) -> dict:
    session = get_onboarding(session_id)
    profile = _validate_profile(session["answers_json"])
    goals = _validate_goals(session["answers_json"])
    safety = safety_assessment(profile)
    assessment = target_assessment(profile, goals)
    legacy_target = (session["answers_json"].get("legacy_settings") or {}).get("protein_target_g")
    if safety["mode"] == "standard" and not assessment["protein_candidates"] and legacy_target is not None:
        try:
            clean_legacy_target = _target_range(legacy_target, "旧蛋白目标")
        except ValidationError:
            clean_legacy_target = None
        if clean_legacy_target:
            assessment["protein_candidates"].append({
                "candidate_id": "protein_legacy_setting",
                "reference": "legacy_setting",
                "reference_weight_kg": None,
                "target_g": clean_legacy_target,
                "factor_g_per_kg": None,
                "basis": "迁移前私人设置中的蛋白目标；需在本次目标契约中重新确认",
                "source_kind": "user_confirmed_legacy_setting",
                "method": "legacy_private_setting",
                "applicability": "standard_mode_user_confirmed_legacy_setting",
                "policy_version": TARGET_POLICY_VERSION,
            })
    return {
        "session_id": session_id,
        "profile": profile,
        "goals": goals,
        "safety": safety,
        "target_assessment": assessment,
    }


def _target_range(value: object, name: str) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(item, (int, float)) or isinstance(item, bool) or item <= 0 for item in value)
        or value[0] > value[1]
    ):
        raise ValidationError(f"{name} 必须是两个递增正数")
    return [round(float(item), 1) for item in value]


def _protein_target_selection(
    confirmation: dict,
    assessment: dict,
    safety: dict,
    profile: dict,
) -> tuple[list[float] | None, dict | None]:
    if safety["mode"] == "standard":
        candidates = assessment["protein_candidates"]
        if not candidates:
            return None, None
        selected = None
        candidate_id = confirmation.get("protein_candidate_id")
        submitted = confirmation.get("protein_target_g")
        if candidate_id:
            selected = next((item for item in candidates if item["candidate_id"] == candidate_id), None)
            if not selected:
                raise ValidationError("蛋白目标候选不存在或已变化")
        elif submitted is not None:
            clean = _target_range(submitted, "protein_target_g")
            selected = next(
                (item for item in candidates if [float(value) for value in item["target_g"]] == clean),
                None,
            )
            if not selected:
                raise ValidationError("自定义蛋白目标必须有专业来源；普通模式请选择系统提供的候选范围")
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            raise ValidationError("请选择蛋白目标参考体重")
        valid_until = _date_text(confirmation.get("target_valid_until"), "目标有效期")
        if valid_until and valid_until < date.today().isoformat():
            raise ValidationError("目标有效期不能早于今天")
        return [float(value) for value in selected["target_g"]], {
            "source_kind": (
                "user_confirmed_legacy_setting"
                if selected.get("method") == "legacy_private_setting"
                else "user_confirmed_suggestion"
            ),
            "method": selected["method"],
            "source_detail": {
                "candidate_id": selected["candidate_id"],
                "reference": selected["reference"],
                "reference_weight_kg": selected["reference_weight_kg"],
                "factor_g_per_kg": selected["factor_g_per_kg"],
                "basis": selected["basis"],
            },
            "applicability": {
                "mode": "standard",
                "population": selected["applicability"],
            },
            "valid_from": date.today().isoformat(),
            "valid_until": valid_until,
        }
    if safety["mode"] == "clinician_guided" and safety["professional_guidance_current"]:
        professional_targets = confirmation.get("professional_targets") or {}
        if not isinstance(professional_targets, dict):
            raise ValidationError("professional_targets 必须是对象")
        if professional_targets.get("protein_g") is None:
            return None, None
        target = _target_range(professional_targets["protein_g"], "professional_targets.protein_g")
        guidance = profile["professional_guidance"]
        return target, {
            "source_kind": "clinician_provided",
            "method": "professional_constraint",
            "source_detail": {
                "source": guidance["source"],
                "summary": guidance["summary"],
                "confirmed_on": guidance["confirmed_on"],
            },
            "applicability": {"mode": "clinician_guided", "flags": safety["flags"]},
            "valid_from": guidance["confirmed_on"],
            "valid_until": guidance["valid_until"],
        }
    return None, None


def complete_onboarding(session_id: str, expected_version: int, confirmation: dict) -> dict:
    confirmation = _object(confirmation, "确认信息")
    if confirmation.get("accept_profile") is not True:
        raise ValidationError("必须确认目标契约和安全档案")
    session = get_onboarding(session_id)
    if session["status"] != "in_progress":
        raise ValidationError("该初始化会话已结束")
    if session["version"] != expected_version:
        raise ValidationError("初始化答案已变化，请刷新后重试")
    preview = onboarding_preview(session_id)
    profile = preview["profile"]
    goals = preview["goals"]
    safety = preview["safety"]
    assessment = preview["target_assessment"]
    if safety["mode"] == "standard":
        planning_mode = confirmation.get("planning_mode", "portion_guided")
    elif safety["mode"] == "clinician_guided" and safety["professional_guidance_current"]:
        planning_mode = "portion_guided"
    else:
        planning_mode = "observation"
    if planning_mode not in {"portion_guided", "numeric_assisted", "observation"}:
        raise ValidationError("规划模式无效")
    if safety["mode"] == "standard" and planning_mode == "observation":
        raise ValidationError("普通模式不能使用观察策略")
    if planning_mode != "observation" and confirmation.get("accept_strategy") is not True:
        raise ValidationError("必须确认初始策略")
    protein_target, target_provenance = _protein_target_selection(
        confirmation, assessment, safety, profile
    )

    timestamp = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM onboarding_sessions WHERE id=?", (session_id,)).fetchone()
        if not current or current["status"] != "in_progress" or current["version"] != expected_version:
            raise ValidationError("初始化状态已变化，请刷新后重试")
        profile_version = int(conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM profile_versions").fetchone()[0])
        strategy_version = int(conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM strategy_versions").fetchone()[0])
        conn.execute("UPDATE profile_versions SET active=0 WHERE active=1")
        conn.execute("UPDATE goal_versions SET active=0 WHERE active=1")
        conn.execute("UPDATE strategy_versions SET active=0 WHERE active=1")
        conn.execute("UPDATE nutrition_target_versions SET active=0 WHERE active=1")
        profile_id = _id("profile")
        conn.execute(
            """INSERT INTO profile_versions(
                   id,version,profile_json,safety_mode,safety_policy_mode,source,active,created_at
               ) VALUES(?,?,?,?,?,?,1,?)""",
            (
                profile_id,
                profile_version,
                json.dumps(profile, ensure_ascii=False),
                "standard" if safety["mode"] == "standard" else "observation",
                safety["mode"],
                "onboarding",
                timestamp,
            ),
        )
        goal_ids = []
        for goal in goals:
            goal_version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM goal_versions WHERE goal_key=?", (goal["key"],)
            ).fetchone()[0])
            goal_id = _id("goal")
            goal_ids.append(goal_id)
            conn.execute(
                """INSERT INTO goal_versions(
                       id,goal_key,version,profile_version_id,goal_json,active,created_at
                   ) VALUES(?,?,?,?,?,1,?)""",
                (goal_id, goal["key"], goal_version, profile_id, json.dumps(goal, ensure_ascii=False), timestamp),
            )
        target_ids = []
        if protein_target is not None and target_provenance is not None:
            target_id = _id("target")
            target_ids.append(target_id)
            target_version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM nutrition_target_versions WHERE target_key='protein_g'"
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO nutrition_target_versions(
                       id,target_key,version,profile_version_id,goal_version_ids_json,value_json,unit,
                       source_kind,source_detail_json,method,applicability_json,safety_mode,policy_version,
                       active,confirmed_at,valid_from,valid_until,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    target_id,
                    "protein_g",
                    target_version,
                    profile_id,
                    json.dumps(goal_ids, ensure_ascii=False),
                    json.dumps(protein_target, ensure_ascii=False),
                    "g/day",
                    target_provenance["source_kind"],
                    json.dumps(target_provenance["source_detail"], ensure_ascii=False),
                    target_provenance["method"],
                    json.dumps(target_provenance["applicability"], ensure_ascii=False),
                    safety["mode"],
                    TARGET_POLICY_VERSION,
                    timestamp,
                    target_provenance["valid_from"],
                    target_provenance["valid_until"],
                    timestamp,
                ),
            )
        strategy = {
            "planning_mode": planning_mode,
            "protein_target_g": protein_target,
            "meal_environment": profile["constraints"]["meal_environment"],
            "portion_method": profile["constraints"]["portion_method"],
            "target_assessment": assessment,
            "confirmed_source": confirmation.get("strategy_source", "user_confirmed_suggestion"),
            "nutrition_target_ids": target_ids,
            "safety_mode": safety["mode"],
            "legacy_settings_snapshot": session["answers_json"].get("legacy_settings"),
        }
        if "meal_modes" in profile["constraints"]:
            strategy["meal_modes"] = profile["constraints"]["meal_modes"]
            strategy["home_cooking"] = _strategy_home_cooking(
                profile, session["answers_json"].get("legacy_settings")
            )
        strategy_id = _id("strategy")
        conn.execute(
            """INSERT INTO strategy_versions(
                   id,version,profile_version_id,goal_version_ids_json,mode,strategy_json,
                   policy_version,active,created_at,confirmed_at
               ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
            (
                strategy_id,
                strategy_version,
                profile_id,
                json.dumps(goal_ids, ensure_ascii=False),
                planning_mode,
                json.dumps(strategy, ensure_ascii=False),
                TARGET_POLICY_VERSION,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """UPDATE onboarding_sessions SET status='completed',current_step='review',
                   completed_at=?,updated_at=? WHERE id=?""",
            (timestamp, timestamp, session_id),
        )
    result = active_personalization()
    from . import agent_workspace

    agent_workspace.mark_all_drafts_stale("目标、策略或安全档案已更新")
    return result


def active_personalization() -> dict:
    init_db()
    with connect() as conn:
        profile = row_dict(conn.execute(
            "SELECT * FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone())
        if not profile:
            return {
                "status": "setup_required",
                "safety": {"mode": "setup_required", "flags": [], "policy_version": TARGET_POLICY_VERSION},
                "profile": None,
                "goals": [],
                "strategy": None,
                "targets": [],
            }
        goals = [row_dict(row) for row in conn.execute(
            "SELECT * FROM goal_versions WHERE active=1 ORDER BY created_at,id"
        ).fetchall()]
        strategy = row_dict(conn.execute(
            "SELECT * FROM strategy_versions WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone())
        targets = [row_dict(row) for row in conn.execute(
            "SELECT * FROM nutrition_target_versions WHERE active=1 ORDER BY target_key,version DESC"
        ).fetchall()]
    today = date.today().isoformat()
    targets = [
        item for item in targets
        if (not item.get("valid_from") or item["valid_from"] <= today)
        and (not item.get("valid_until") or item["valid_until"] >= today)
    ]
    goals.sort(key=lambda item: item["goal_json"].get("priority", 99))
    return {
        "status": "completed",
        "safety": safety_assessment(profile["profile_json"]),
        "profile": profile,
        "goals": goals,
        "strategy": strategy,
        "targets": targets,
    }


def generation_policy(kind: str) -> dict:
    if kind not in {"photo", "material", "daily", "adaptation", "rescue"}:
        raise ValidationError("未知的生成类型")
    current = active_personalization()
    safety_mode = current["safety"]["mode"]
    if current["status"] == "setup_required":
        return {
            "allowed": False,
            "fact_only": True,
            "safety_mode": "setup_required",
            "reason": "请先完成目标与安全初始化，再生成分析或计划。",
            "policy_version": TARGET_POLICY_VERSION,
        }
    if safety_mode in {"observation", "halt_and_refer"}:
        allowed = kind in {"photo", "material"}
        return {
            "allowed": allowed,
            "fact_only": True,
            "safety_mode": safety_mode,
            "reason": (
                "受限安全模式只允许事实型照片和原材料分析。"
                if allowed else "受限安全模式不生成处方型菜单、适应分析或救场建议。"
            ),
            "policy_version": TARGET_POLICY_VERSION,
        }
    if safety_mode == "clinician_guided":
        guidance_current = current["safety"].get("professional_guidance_current", False)
        if kind in {"photo", "material"}:
            return {
                "allowed": True,
                "fact_only": True,
                "safety_mode": safety_mode,
                "reason": "专业指导模式下，照片和原材料仅做事实提取。",
                "policy_version": TARGET_POLICY_VERSION,
            }
        allowed = guidance_current and kind in {"daily", "rescue"}
        return {
            "allowed": allowed,
            "fact_only": False,
            "safety_mode": safety_mode,
            "reason": (
                "当前操作必须遵循已确认且仍有效的专业指导。"
                if allowed else "缺少仍有效的专业指导，不能生成菜单、适应分析或救场建议。"
            ),
            "policy_version": TARGET_POLICY_VERSION,
        }
    return {
        "allowed": True,
        "fact_only": False,
        "safety_mode": "standard",
        "reason": "",
        "policy_version": TARGET_POLICY_VERSION,
    }


def require_generation(kind: str) -> dict:
    policy = generation_policy(kind)
    if not policy["allowed"]:
        raise ValidationError(policy["reason"])
    return policy


def resolved_settings(legacy_settings: dict) -> dict:
    current = active_personalization()
    strategy_row = current.get("strategy")
    if not strategy_row:
        return {**legacy_settings, "sources": {"base": "settings.json", "strategy": None}}
    strategy = strategy_row["strategy_json"]
    resolved = dict(legacy_settings)
    snapshot = strategy.get("legacy_settings_snapshot") or {}
    conflicts = []
    target_rows = current.get("targets") or []
    protein_target = next(
        (row["value_json"] for row in target_rows if row["target_key"] == "protein_g"),
        None,
    )
    mappings = {
        "protein_target_g": protein_target,
        "meal_environment": strategy.get("meal_environment"),
        "portion_method": strategy.get("portion_method"),
    }
    for key, strategy_value in mappings.items():
        if strategy_value is None:
            if key == "protein_target_g":
                resolved[key] = None
            continue
        if key in snapshot and legacy_settings.get(key) != snapshot.get(key):
            conflicts.append({
                "field": key,
                "strategy_value": strategy_value,
                "manual_file_value": legacy_settings.get(key),
                "resolution": "manual_file_override",
            })
            continue
        resolved[key] = strategy_value
    meal_modes = strategy.get("meal_modes")
    if meal_modes_are_valid(meal_modes):
        resolved["meal_modes"] = dict(meal_modes)
        resolved["home_cooking"] = dict(strategy.get("home_cooking") or {
            "enabled": "home_cook" in meal_modes.values(),
            "meal_modes": meal_modes,
        })
    resolved["sources"] = {
        "base": "settings.json",
        "strategy": {"id": strategy_row["id"], "version": strategy_row["version"], "policy_version": strategy_row["policy_version"]},
        "targets": [
            {
                "id": row["id"],
                "key": row["target_key"],
                "version": row["version"],
                "source_kind": row["source_kind"],
                "policy_version": row["policy_version"],
                "valid_until": row["valid_until"],
            }
            for row in target_rows
        ],
        "safety_mode": current["safety"]["mode"],
        "conflicts": conflicts,
    }
    return resolved


def record_metric(metric_key: str, observed_date: str, value: object, source: str = "user") -> dict:
    metric_key = _text(metric_key, "指标键", maximum=100)
    try:
        date.fromisoformat(observed_date)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    if source not in {"user", "checkin", "imported"}:
        raise ValidationError("指标来源无效")
    item_id = _id("metric")
    timestamp = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO metric_observations(id,metric_key,observed_date,value_json,source,created_at) VALUES(?,?,?,?,?,?)",
            (item_id, metric_key, observed_date, json.dumps(value, ensure_ascii=False), source, timestamp),
        )
        row = conn.execute("SELECT * FROM metric_observations WHERE id=?", (item_id,)).fetchone()
    result = row_dict(row)
    from . import agent_workspace

    agent_workspace.mark_all_drafts_stale("新的长期指标可能影响计划")
    return result


def list_metrics(metric_key: str | None = None, limit: int = 100) -> list[dict]:
    if limit < 1 or limit > 500:
        raise ValidationError("指标数量必须在 1–500 之间")
    init_db()
    with connect() as conn:
        if metric_key:
            rows = conn.execute(
                "SELECT * FROM metric_observations WHERE metric_key=? ORDER BY observed_date DESC,created_at DESC LIMIT ?",
                (metric_key, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM metric_observations ORDER BY observed_date DESC,created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [row_dict(row) for row in rows]
