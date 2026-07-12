from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .meal_modes import legacy_home_meal_modes, meal_modes_are_valid
from .storage import (
    ROOT,
    app_home,
    core_rules_path,
    db_path,
    private_doctrine_path,
    profile_path,
    settings_path,
)
from .validation import ValidationError


SETTINGS_SCHEMA_VERSION = 1
SETTING_FIELDS = (
    "meal_environment",
    "protein_target_g",
    "portion_method",
    "missing_training_default",
    "compensation_boundary",
    "home_cooking",
)

HOME_COOKING_DEFAULT = {"enabled": False}
HOME_COOKING_FIELDS = {
    "region",
    "meal_scope",
    "servings",
    "weekday_time_limit_minutes",
    "equipment",
    "recipe_detail",
    "rotation_window_days",
    "reuse_policy",
    "flavor_preferences",
    "online_purchase_mode",
    "food_exclusions",
}
HOME_COOKING_EQUIPMENT = {"rice_cooker", "stovetop_pan", "stovetop_pot", "refrigerator"}


def validate_settings(value: object, *, allow_missing_protein: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("settings.json 顶层必须是对象")
    raw_version = value.get("schema_version", 0)
    if not isinstance(raw_version, int) or isinstance(raw_version, bool) or raw_version not in {0, SETTINGS_SCHEMA_VERSION}:
        raise ValidationError(f"不支持的 settings schema_version：{raw_version}")
    timezone_name = value.get("timezone", "UTC")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValidationError("timezone 必须是 IANA 时区名称")
    timezone_name = timezone_name.strip()
    if timezone_name != "UTC":
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            # Windows Python does not ship the IANA database. Keep validation strict
            # when tzdata exists, and otherwise accept only a well-formed IANA key.
            try:
                ZoneInfo("Etc/UTC")
            except ZoneInfoNotFoundError:
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+.-]+)+", timezone_name):
                    raise ValidationError(f"timezone 不是有效的 IANA 时区名称：{timezone_name}") from exc
            else:
                raise ValidationError(f"timezone 不是可用的 IANA 时区：{timezone_name}") from exc
    settings = {key: value.get(key) for key in SETTING_FIELDS}
    settings["schema_version"] = SETTINGS_SCHEMA_VERSION
    settings["timezone"] = timezone_name
    settings["home_cooking"] = _validate_home_cooking(value.get("home_cooking"))
    target = settings["protein_target_g"]
    if target is None and allow_missing_protein:
        settings["protein_target_g"] = None
    elif (
        not isinstance(target, list)
        or len(target) != 2
        or any(not isinstance(item, (int, float)) or isinstance(item, bool) or item <= 0 for item in target)
        or target[0] > target[1]
    ):
        raise ValidationError("protein_target_g 必须是两个递增正数")
    else:
        settings["protein_target_g"] = [float(item) if isinstance(item, float) else item for item in target]
    for key in SETTING_FIELDS:
        if key in {"protein_target_g", "home_cooking"}:
            continue
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise ValidationError(f"{key} 必须是非空文本")
        settings[key] = settings[key].strip()
    return settings


def _validate_home_cooking(value: object) -> dict:
    if value is None:
        return dict(HOME_COOKING_DEFAULT)
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        raise ValidationError("home_cooking.enabled 必须是布尔值")
    if not value["enabled"]:
        return dict(HOME_COOKING_DEFAULT)
    missing = sorted((HOME_COOKING_FIELDS - {"meal_scope"}) - value.keys())
    if missing:
        raise ValidationError(f"home_cooking 缺少字段：{missing}")
    if value["region"] != "china":
        raise ValidationError("home_cooking.region 首版仅支持 china")
    if value.get("meal_scope") not in {None, "dinner", "lunch_and_dinner", "custom"}:
        raise ValidationError("home_cooking.meal_scope 无效")
    meal_modes = value.get("meal_modes") or legacy_home_meal_modes(value)
    if not meal_modes_are_valid(meal_modes):
        raise ValidationError("home_cooking.meal_modes 必须逐项指定早餐、午餐和晚餐的准备方式")
    if value["servings"] != 1:
        raise ValidationError("home_cooking 目前仅支持一人份")
    if value["recipe_detail"] != "beginner_card":
        raise ValidationError("home_cooking.recipe_detail 必须是 beginner_card")
    if value["reuse_policy"] != "reuse_ingredients_rotate_dishes":
        raise ValidationError("home_cooking.reuse_policy 无效")
    if value["online_purchase_mode"] != "spec_and_search_keywords":
        raise ValidationError("home_cooking.online_purchase_mode 无效")
    time_limit = value["weekday_time_limit_minutes"]
    if not isinstance(time_limit, int) or isinstance(time_limit, bool) or not 10 <= time_limit <= 60:
        raise ValidationError("home_cooking.weekday_time_limit_minutes 必须是 10–60 的整数")
    window = value["rotation_window_days"]
    if not isinstance(window, int) or isinstance(window, bool) or not 2 <= window <= 14:
        raise ValidationError("home_cooking.rotation_window_days 必须是 2–14 的整数")
    equipment = value["equipment"]
    if not isinstance(equipment, list) or not equipment or any(item not in HOME_COOKING_EQUIPMENT for item in equipment):
        raise ValidationError("home_cooking.equipment 包含无效设备")
    for field in ("flavor_preferences", "food_exclusions"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValidationError(f"home_cooking.{field} 必须是文本数组")
    return {
        "enabled": True,
        **{key: value[key] for key in HOME_COOKING_FIELDS if key in value},
        "meal_modes": meal_modes,
        "equipment": list(dict.fromkeys(value["equipment"])),
        "flavor_preferences": list(dict.fromkeys(item.strip() for item in value["flavor_preferences"])),
        "food_exclusions": list(dict.fromkeys(item.strip() for item in value["food_exclusions"])),
    }


def load_settings(*, allow_missing_protein: bool = False) -> dict:
    path = settings_path()
    if not path.is_file():
        raise ValidationError(f"缺少私人设置：{path}；请先运行 python -m mealcircuit.agent_cli init 并填写配置")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取私人设置：{exc}") from exc
    return validate_settings(value, allow_missing_protein=allow_missing_protein)


def load_resolved_settings() -> dict:
    """Overlay an explicitly confirmed strategy on legacy private settings."""
    settings = load_settings(allow_missing_protein=True)
    from .personalization import resolved_settings

    return resolved_settings(settings)


def configured_today(settings: dict | None = None) -> date:
    value = settings or load_settings(allow_missing_protein=True)
    timezone_name = str(value.get("timezone") or "UTC")
    # UTC is part of the standard library and must keep source checkouts usable on
    # Windows even before the package-managed ``tzdata`` dependency is installed.
    if timezone_name == "UTC":
        return datetime.now(timezone.utc).date()
    try:
        timezone_value = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        if timezone_name in {"Asia/Shanghai", "Asia/Singapore"}:
            return datetime.now(timezone(timedelta(hours=8))).date()
        raise ValidationError(
            f"缺少 IANA 时区数据，无法按 {timezone_name} 判断今天；请安装 tzdata 或改用 UTC"
        ) from exc
    return datetime.now(timezone_value).date()


def load_doctrine() -> dict:
    private_path = private_doctrine_path()
    if private_path.is_file():
        return {
            "path": str(private_path),
            "mode": "private_override",
            "sources": [{"kind": "private_doctrine", "path": str(private_path)}],
            "content": private_path.read_text(encoding="utf-8"),
        }
    core_path = core_rules_path()
    user_profile = profile_path()
    if not core_path.is_file():
        raise ValidationError(f"缺少公开核心规则：{core_path}")
    if not user_profile.is_file():
        raise ValidationError(f"缺少私人档案：{user_profile}；请先运行 python -m mealcircuit.agent_cli init")
    sources = [
        {"kind": "core_rules", "path": str(core_path)},
        {"kind": "personal_profile", "path": str(user_profile)},
    ]
    content = core_path.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n" + user_profile.read_text(encoding="utf-8").lstrip()
    return {"path": None, "mode": "composed", "sources": sources, "content": content}


def initialize_private_home() -> dict:
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    skipped: list[str] = []
    templates = {
        ROOT / "templates" / "profile.md": profile_path(),
        ROOT / "templates" / "settings.json": settings_path(),
    }
    for source, target in templates.items():
        if target.exists():
            skipped.append(str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        created.append(str(target))
    for directory in ("uploads", "food-labels", "assets", "exports", "backups", "archive/tmp-imports"):
        target = home / directory
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        (skipped if existed else created).append(str(target))
    return {"home": str(home), "created": created, "skipped": skipped}


def configuration_status() -> dict:
    settings_ok = False
    settings_error = None
    try:
        load_settings()
        settings_ok = True
    except ValidationError as exc:
        settings_error = str(exc)
    doctrine_mode = "missing"
    try:
        doctrine_mode = load_doctrine()["mode"]
    except (ValidationError, OSError):
        pass
    try:
        from .ai import ai_status

        ai = ai_status()
        ai_error = None
    except ValidationError as exc:
        ai = None
        ai_error = str(exc)
    try:
        from .personalization import onboarding_status

        onboarding = onboarding_status()
    except (ValidationError, OSError):
        onboarding = {"status": "unavailable", "safety_mode": "setup_required"}
    unresolved_assets: list[str] = []
    try:
        from .db import connect, init_db
        from .domain_store import unresolved_asset_references

        init_db()
        with connect() as connection:
            unresolved_assets = unresolved_asset_references(connection)
    except (OSError, RuntimeError, ValidationError):
        pass
    return {
        "home": str(app_home()),
        "database": str(db_path()),
        "profile_exists": profile_path().is_file(),
        "settings_valid": settings_ok,
        "settings_error": settings_error,
        "doctrine_mode": doctrine_mode,
        "ai": ai,
        "ai_error": ai_error,
        "onboarding": onboarding,
        "unresolved_assets": unresolved_assets,
    }
