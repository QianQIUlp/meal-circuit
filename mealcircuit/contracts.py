from __future__ import annotations

import json
import sys
import sysconfig
from functools import lru_cache
from pathlib import Path
from typing import Any

from .validation import ValidationError


def _contract_roots() -> tuple[Path, ...]:
    roots = [Path(__file__).resolve().parent.parent / "protocol"]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root) / "protocol")
    data_root = sysconfig.get_path("data")
    if data_root:
        roots.append(Path(data_root) / "share" / "mealcircuit" / "protocol")
    return tuple(dict.fromkeys(roots))


@lru_cache(maxsize=None)
def load_contract(name: str) -> dict[str, Any]:
    if not name.endswith(".json") or Path(name).name != name:
        raise ValidationError("协议文件名无效")
    for root in _contract_roots():
        candidate = root / name
        if candidate.is_file():
            value = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValidationError(f"不支持的协议版本：{name}")
            return value
    raise ValidationError(f"缺少协议文件：{name}")


def validate_transition(machine: str, before: str | None, after: str) -> None:
    machines = load_contract("state-machines-v1.json").get("machines", {})
    definition = machines.get(machine)
    if not isinstance(definition, dict):
        raise ValidationError(f"未知状态机：{machine}")
    if before is None:
        if after != definition.get("initial"):
            raise ValidationError(f"{machine} 初始状态必须是 {definition.get('initial')}")
        return
    allowed = definition.get("transitions", {}).get(before)
    if not isinstance(allowed, list) or after not in allowed:
        raise ValidationError(f"{machine} 不允许从 {before} 转到 {after}")
