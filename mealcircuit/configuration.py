from __future__ import annotations

import json
import shutil
from pathlib import Path

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


SETTING_FIELDS = (
    "meal_environment",
    "protein_target_g",
    "portion_method",
    "missing_training_default",
    "compensation_boundary",
)


def validate_settings(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("settings.json 顶层必须是对象")
    settings = {key: value.get(key) for key in SETTING_FIELDS}
    target = settings["protein_target_g"]
    if (
        not isinstance(target, list)
        or len(target) != 2
        or any(not isinstance(item, (int, float)) or isinstance(item, bool) or item <= 0 for item in target)
        or target[0] > target[1]
    ):
        raise ValidationError("protein_target_g 必须是两个递增正数")
    settings["protein_target_g"] = [float(item) if isinstance(item, float) else item for item in target]
    for key in SETTING_FIELDS:
        if key == "protein_target_g":
            continue
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise ValidationError(f"{key} 必须是非空文本")
        settings[key] = settings[key].strip()
    return settings


def load_settings() -> dict:
    path = settings_path()
    if not path.is_file():
        raise ValidationError(f"缺少私人设置：{path}；请先运行 python -m mealcircuit.agent_cli init 并填写配置")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取私人设置：{exc}") from exc
    return validate_settings(value)


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
    for directory in ("uploads", "food-labels", "exports", "backups", "archive/tmp-imports"):
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
    return {
        "home": str(app_home()),
        "database": str(db_path()),
        "profile_exists": profile_path().is_file(),
        "settings_valid": settings_ok,
        "settings_error": settings_error,
        "doctrine_mode": doctrine_mode,
    }
