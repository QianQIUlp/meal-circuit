from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
_WARNED_LEGACY: set[str] = set()


def _environment(name: str, legacy: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    if legacy and os.environ.get(legacy):
        if legacy not in _WARNED_LEGACY:
            print(f"警告：{legacy} 已弃用，请改用 {name}", file=sys.stderr)
            _WARNED_LEGACY.add(legacy)
        return os.environ[legacy]
    return None


def app_home() -> Path:
    configured = _environment("MEALCIRCUIT_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return (Path(base) / "MealCircuit").resolve()
        return (Path.home() / "AppData" / "Local" / "MealCircuit").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "MealCircuit").resolve()
    base = os.environ.get("XDG_DATA_HOME")
    return ((Path(base).expanduser() if base else Path.home() / ".local" / "share") / "mealcircuit").resolve()


def db_path() -> Path:
    configured = _environment("MEALCIRCUIT_DB", "DIETOS_DB")
    return Path(configured).expanduser().resolve() if configured else app_home() / "mealcircuit.db"


def port_value() -> int:
    configured = _environment("MEALCIRCUIT_PORT", "DIETOS_PORT")
    return int(configured or "8765")


def upload_root() -> Path:
    return app_home() / "uploads"


def food_label_root() -> Path:
    return app_home() / "food-labels"


def profile_path() -> Path:
    return app_home() / "profile.md"


def settings_path() -> Path:
    return app_home() / "settings.json"


def private_doctrine_path() -> Path:
    configured = _environment("MEALCIRCUIT_DOCTRINE")
    return Path(configured).expanduser().resolve() if configured else app_home() / "doctrine.private.md"


def core_rules_path() -> Path:
    return ROOT / "rules" / "core.md"


def exports_root() -> Path:
    return app_home() / "exports"


def backups_root() -> Path:
    return app_home() / "backups"


def resolve_data_path(value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (app_home() / path).resolve()


def store_data_path(path: str | Path) -> str:
    absolute = Path(path).resolve()
    try:
        return absolute.relative_to(app_home().resolve()).as_posix()
    except ValueError:
        return str(absolute)
