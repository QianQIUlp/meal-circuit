from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .contracts import validate_transition
from .validation import ValidationError


DOMAIN_SCHEMA_VERSION = 1
ENTITY_KINDS = {
    "task",
    "task_input",
    "analysis_result",
    "correction",
    "food_item",
    "daily_record",
    "checkin_day",
    "checkin_draft",
    "daily_review",
    "memory",
    "adjustment",
    "preferences",
    "asset",
}
# New IDs are full UUIDv4 values. The reader remains deliberately tolerant of
# shorter legacy IDs because migration must not rewrite historical identities.
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,95}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    clean = str(prefix).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", clean):
        raise ValueError("ID prefix must contain lowercase letters, numbers, or underscores")
    return f"{clean}_{uuid.uuid4()}"


def validate_id(value: object, name: str = "id") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValidationError(f"{name} 格式无效")
    return value


def validate_timestamp(value: object, name: str = "created_at") -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} 必须是 RFC 3339 时间")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError(f"{name} 必须是 RFC 3339 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} 必须包含时区")
    return value


def validate_date(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} 必须是 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} 必须是 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValidationError(f"{name} 必须是 YYYY-MM-DD")
    return value


@dataclass(frozen=True)
class DomainRevision:
    entity_id: str
    entity_kind: str
    revision_id: str
    parent_revision_ids: tuple[str, ...]
    created_at: str
    author_device_id: str
    deleted: bool
    payload: dict[str, Any]
    schema_version: int = DOMAIN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "revision_id": self.revision_id,
            "parent_revision_ids": list(self.parent_revision_ids),
            "created_at": self.created_at,
            "author_device_id": self.author_device_id,
            "deleted": self.deleted,
            "payload": copy.deepcopy(self.payload),
        }


def validate_revision(value: object) -> DomainRevision:
    if not isinstance(value, dict):
        raise ValidationError("领域 revision 必须是对象")
    version = value.get("schema_version")
    if version != DOMAIN_SCHEMA_VERSION:
        raise ValidationError(f"不支持的领域 schema_version：{version}")
    entity_kind = value.get("entity_kind")
    if entity_kind not in ENTITY_KINDS:
        raise ValidationError("entity_kind 无效")
    parents = value.get("parent_revision_ids")
    if not isinstance(parents, list) or len(set(parents)) != len(parents):
        raise ValidationError("parent_revision_ids 必须是无重复 ID 数组")
    parent_ids = tuple(validate_id(item, "parent_revision_id") for item in parents)
    entity_id = validate_id(value.get("entity_id"), "entity_id")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValidationError("payload 必须是对象")
    validate_payload(entity_kind, payload)
    validate_payload_identity(entity_kind, entity_id, payload)
    deleted = value.get("deleted")
    if not isinstance(deleted, bool):
        raise ValidationError("deleted 必须是布尔值")
    return DomainRevision(
        schema_version=version,
        entity_id=entity_id,
        entity_kind=entity_kind,
        revision_id=validate_id(value.get("revision_id"), "revision_id"),
        parent_revision_ids=parent_ids,
        created_at=validate_timestamp(value.get("created_at")),
        author_device_id=validate_id(value.get("author_device_id"), "author_device_id"),
        deleted=deleted,
        payload=copy.deepcopy(payload),
    )


def validate_payload(entity_kind: str, payload: dict[str, Any]) -> None:
    required: dict[str, tuple[str, ...]] = {
        "task": ("task",),
        "task_input": ("task_id", "task_type", "input_version", "original_input", "input_history"),
        "analysis_result": ("source_entity_id", "source_kind", "result_version", "result", "provenance"),
        "correction": ("id", "task_id", "correction_json", "created_at"),
        "food_item": ("food", "history"),
        "daily_record": ("id", "record_date", "raw_input", "created_at"),
        "checkin_day": ("checkin", "modules"),
        "checkin_draft": ("checkin", "modules"),
        "daily_review": ("review", "history"),
        "memory": ("id", "kind", "content", "active", "created_at", "updated_at"),
        "adjustment": ("id", "content", "active", "created_at", "updated_at"),
        "preferences": ("kind", "content"),
        "asset": ("sha256", "media_type", "extension", "byte_count"),
    }
    missing = [key for key in required[entity_kind] if key not in payload]
    if missing:
        raise ValidationError(f"{entity_kind} payload 缺少字段：{', '.join(missing)}")
    nested_required = {
        "task": ("id", "type", "status", "created_at"),
        "food_item": ("id", "name", "basis", "created_at", "updated_at"),
        "checkin_day": ("id", "checkin_date", "created_at", "updated_at"),
        "checkin_draft": ("id", "checkin_date", "created_at", "updated_at"),
        "daily_review": ("id", "review_date", "status", "source_record_ids_json", "result_version", "created_at", "updated_at"),
    }
    if entity_kind in nested_required:
        field = {"food_item": "food", "daily_review": "review"}.get(entity_kind, "checkin" if entity_kind.startswith("checkin") else "task")
        nested = payload.get(field)
        if not isinstance(nested, dict):
            raise ValidationError(f"{entity_kind}.{field} 必须是对象")
        missing = [key for key in nested_required[entity_kind] if key not in nested]
        if missing:
            raise ValidationError(f"{entity_kind}.{field} 缺少字段：{', '.join(missing)}")
    if entity_kind in {"food_item", "daily_review"} and not isinstance(payload["history"], list):
        raise ValidationError(f"{entity_kind}.history 必须是数组")
    if entity_kind.startswith("checkin") and not isinstance(payload["modules"], list):
        raise ValidationError(f"{entity_kind}.modules 必须是数组")
    if entity_kind == "daily_record":
        validate_date(payload["record_date"], "daily_record.record_date")
    elif entity_kind.startswith("checkin"):
        validate_date(payload["checkin"]["checkin_date"], f"{entity_kind}.checkin_date")
    elif entity_kind == "daily_review":
        validate_date(payload["review"]["review_date"], "daily_review.review_date")


def validate_payload_identity(entity_kind: str, entity_id: str, payload: dict[str, Any]) -> None:
    nested = {
        "task": ("task", "id"),
        "food_item": ("food", "id"),
        "checkin_day": ("checkin", "id"),
        "checkin_draft": ("checkin", "id"),
        "daily_review": ("review", "id"),
    }
    if entity_kind in nested:
        container, key = nested[entity_kind]
        payload_id = payload[container].get(key)
    elif entity_kind in {"daily_record", "correction", "memory", "adjustment"}:
        payload_id = payload.get("id")
    else:
        payload_id = None
    if payload_id is not None and payload_id != entity_id:
        raise ValidationError(f"{entity_kind} payload ID 与 entity_id 不一致")
    for key in ("task_id", "source_entity_id"):
        if key in payload:
            validate_id(payload[key], f"{entity_kind}.{key}")


def make_revision(
    entity_kind: str,
    payload: dict[str, Any],
    *,
    entity_id: str | None = None,
    parent_revision_ids: list[str] | tuple[str, ...] = (),
    author_device_id: str,
    deleted: bool = False,
    created_at: str | None = None,
) -> DomainRevision:
    prefix = {
        "food_item": "food",
        "daily_record": "record",
        "daily_review": "review",
        "analysis_result": "result",
        "preferences": "preferences",
    }.get(entity_kind, entity_kind)
    return validate_revision(
        {
            "schema_version": DOMAIN_SCHEMA_VERSION,
            "entity_id": entity_id or new_id(prefix),
            "entity_kind": entity_kind,
            "revision_id": new_id("rev"),
            "parent_revision_ids": list(parent_revision_ids),
            "created_at": created_at or utc_now(),
            "author_device_id": author_device_id,
            "deleted": deleted,
            "payload": payload,
        }
    )


def three_way_merge(
    base: dict[str, Any], local: dict[str, Any], remote: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Merge disjoint field edits and report overlapping JSON paths without using timestamps."""

    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    missing = object()
    keys = set(base) | set(local) | set(remote)
    for key in sorted(keys):
        before = base.get(key, missing)
        left = local.get(key, missing)
        right = remote.get(key, missing)
        if left == right:
            chosen = left
        elif left == before:
            chosen = right
        elif right == before:
            chosen = left
        elif all(isinstance(item, dict) for item in (before, left, right)):
            child, child_conflicts = three_way_merge(before, left, right)
            merged[key] = child
            conflicts.extend(f"{key}.{path}" for path in child_conflicts)
            continue
        elif all(isinstance(item, list) for item in (before, left, right)) and all(
            all(isinstance(child, dict) and isinstance(child.get("id"), str) for child in item)
            for item in (before, left, right)
        ):
            before_by_id = {item["id"]: item for item in before}
            left_by_id = {item["id"]: item for item in left}
            right_by_id = {item["id"]: item for item in right}
            if any(
                len(mapping) != len(items)
                for mapping, items in (
                    (before_by_id, before),
                    (left_by_id, left),
                    (right_by_id, right),
                )
            ):
                conflicts.append(key)
                chosen = left
            else:
                merged_items = []
                for item_id in sorted(set(before_by_id) | set(left_by_id) | set(right_by_id)):
                    item_before = before_by_id.get(item_id, missing)
                    item_left = left_by_id.get(item_id, missing)
                    item_right = right_by_id.get(item_id, missing)
                    if item_left == item_right:
                        item_chosen = item_left
                    elif item_left == item_before:
                        item_chosen = item_right
                    elif item_right == item_before:
                        item_chosen = item_left
                    elif all(isinstance(item, dict) for item in (item_before, item_left, item_right)):
                        child, child_conflicts = three_way_merge(item_before, item_left, item_right)
                        item_chosen = child
                        conflicts.extend(f"{key}[{item_id}].{path}" for path in child_conflicts)
                    else:
                        conflicts.append(f"{key}[{item_id}]")
                        item_chosen = item_left
                    if item_chosen is not missing:
                        merged_items.append(copy.deepcopy(item_chosen))
                merged[key] = merged_items
                continue
        elif key.endswith("_at") and isinstance(left, str) and isinstance(right, str):
            # Timestamps are metadata only: keep a deterministic value without using it
            # to decide which business-field edit wins.
            chosen = max(left, right)
        else:
            conflicts.append(key)
            chosen = left
        if chosen is not missing:
            merged[key] = copy.deepcopy(chosen)
    return merged, conflicts
