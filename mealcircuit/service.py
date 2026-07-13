from __future__ import annotations

import json
import re
import shutil
import hashlib
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import BinaryIO

from . import checkins
from .configuration import configured_today, load_doctrine, load_resolved_settings, load_settings
from .db import connect, init_db, row_dict
from .domain import new_id, utc_now
from .domain_store import (
    capture_correction,
    capture_derived_result,
    capture_entity,
    capture_task_input,
    preference_entity_id,
    task_input_entity_id,
)
from .meal_modes import (
    LEGACY_DEFAULT_MEAL_MODES,
    MEAL_KEYS,
    MEAL_KEYS_BY_NAME,
    MEAL_NAMES,
    home_cooked_meal_names,
    legacy_home_meal_modes,
    meal_environment_for_modes,
    meal_rotation,
)
from .storage import resolve_data_path, store_data_path, upload_root
from .validation import VALIDATOR_VERSION, ValidationError, validate_daily_review_result, validate_result

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
IMAGE_SIGNATURES = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",
}


def now() -> str:
    return utc_now()


def _revision_references(entity_ids: set[str]) -> list[dict]:
    if not entity_ids:
        return []
    init_db()
    placeholders = ",".join("?" for _ in entity_ids)
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                f"""SELECT entity_id,entity_kind,revision_id FROM entity_heads
                    WHERE entity_id IN ({placeholders}) ORDER BY entity_kind,entity_id""",
                tuple(sorted(entity_ids)),
            )
        ]


def _result_provenance(source_revisions: list[dict], generator: dict | None = None) -> dict:
    by_entity = {item["entity_id"]: item for item in source_revisions}
    settings_id = preference_entity_id("settings")
    doctrine_id = preference_entity_id("doctrine")
    with connect() as conn:
        hashes = {
            row["kind"]: row["content_sha256"]
            for row in conn.execute(
                "SELECT kind,content_sha256 FROM config_documents WHERE kind IN ('settings','doctrine')"
            )
        }
    metadata = dict(generator or {"provider": "external_agent", "model": "unspecified"})
    metadata.pop("api_key", None)
    metadata["generated_at"] = now()
    return {
        "schema_version": 1,
        "source_revisions": source_revisions,
        "settings_revision_id": (by_entity.get(settings_id) or {}).get("revision_id"),
        "settings_sha256": hashes.get("settings"),
        "doctrine_revision_id": (by_entity.get(doctrine_id) or {}).get("revision_id"),
        "doctrine_sha256": hashes.get("doctrine"),
        "result_schema_version": 1,
        "generator": metadata,
    }


def _generator_metadata(provider) -> dict:
    config = getattr(provider, "config", None)
    return {
        "provider": str(getattr(config, "provider", provider.__class__.__name__)),
        "model": str(getattr(config, "model", "unspecified")),
    }


def _decorate_staleness(provenance: dict | None) -> dict | None:
    if not isinstance(provenance, dict):
        return provenance
    references = provenance.get("source_revisions") or []
    entity_ids = {
        item.get("entity_id") for item in references if isinstance(item, dict) and item.get("entity_id")
    }
    current = {item["entity_id"]: item["revision_id"] for item in _revision_references(entity_ids)}
    stale = [
        item["entity_id"]
        for item in references
        if isinstance(item, dict) and current.get(item.get("entity_id")) != item.get("revision_id")
    ]
    value = json.loads(json.dumps(provenance, ensure_ascii=False))
    value["stale"] = bool(stale)
    value["stale_entity_ids"] = stale
    return value


def _client_identity(client=None) -> tuple[str | None, str | None]:
    if client is not None:
        config = getattr(client, "config", None)
        return (
            getattr(config, "provider", None) or client.__class__.__name__,
            getattr(config, "model", None) or "injected-client",
        )
    from .ai import ai_status

    status = ai_status()
    return status.get("provider"), status.get("model")


def _start_agent_run(
    kind: str,
    context: dict,
    client=None,
    *,
    identity: tuple[str | None, str | None] | None = None,
) -> str:
    provider, model = identity or _client_identity(client)
    run_id = new_id("agent_run")
    policy = context.get("generation_policy") or {}
    source_manifest = dict(context.get("source_manifest") or {})
    source_manifest["agent_run_id"] = run_id
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_runs(
                   id,kind,provider,model,context_hash,context_schema_version,result_schema_version,
                   policy_version,validator_version,source_manifest_json,status,started_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'running',?)""",
            (
                run_id,
                kind,
                provider,
                model,
                context.get("context_hash") or "",
                int(context.get("context_schema_version") or 1),
                int(context.get("result_schema_version") or 1),
                policy.get("policy_version") or "",
                VALIDATOR_VERSION,
                json.dumps(source_manifest, ensure_ascii=False, sort_keys=True),
                now(),
            ),
        )
    return run_id


def _source_manifest_for_commit(context: dict, agent_run_id: str | None) -> dict:
    manifest = dict(context.get("source_manifest") or {})
    manifest["agent_run_id"] = agent_run_id
    return manifest


def _finish_agent_run(run_id: str, *, result: dict | None = None, error: Exception | None = None) -> None:
    status = "failed" if error is not None else "completed"
    error_summary = "" if error is None else f"{error.__class__.__name__}: {str(error)[:500]}"
    result_hash = ""
    attempts = []
    if result is not None:
        result_hash = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        attempts.append({"attempt": 1, "status": "valid"})
    elif error is not None:
        attempts.append({"attempt": 1, "status": "failed", "error": error_summary})
    with connect() as conn:
        updated = conn.execute(
            """UPDATE agent_runs SET status=?,error_summary=?,validation_attempts_json=?,result_hash=?,completed_at=?
               WHERE id=? AND status='running'""",
            (status, error_summary, json.dumps(attempts, ensure_ascii=False), result_hash, now(), run_id),
        )
        if updated.rowcount != 1:
            raise ValidationError("Agent 运行状态已变化")


def _detect_image(data: bytes) -> str:
    for signature, ext in IMAGE_SIGNATURES.items():
        if data.startswith(signature):
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return ext
    raise ValidationError("仅支持 JPEG、PNG、GIF 或 WebP 图片")


def _read_upload(stream: BinaryIO) -> tuple[bytes, str]:
    data = stream.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ValidationError("请选择食物照片")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError("图片不能超过 10MB")
    return data, _detect_image(data)


def create_photo_task(stream: BinaryIO, note: str = "") -> dict:
    init_db()
    data, ext = _read_upload(stream)
    task_id = new_id("task")
    absolute = upload_root() / f"{task_id}{ext}"
    stored_path = store_data_path(absolute)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(data)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id,type,status,original_input,image_path,created_at) VALUES(?,?,?,?,?,?)",
                (task_id, "photo", "pending", note.strip(), stored_path, now()),
            )
            capture_entity(conn, "task", task_id)
            capture_task_input(conn, task_id)
    except Exception:
        absolute.unlink(missing_ok=True)
        raise
    return get_task(task_id)


def create_material_task(materials: str) -> dict:
    materials = materials.strip()
    if not materials:
        raise ValidationError("请输入现有食材和粗略数量")
    if len(materials) > 10000:
        raise ValidationError("原材料输入不能超过 10000 字")
    init_db()
    task_id = new_id("task")
    with connect() as conn:
        conn.execute(
            "INSERT INTO tasks(id,type,status,original_input,created_at) VALUES(?,?,?,?,?)",
            (task_id, "material", "pending", materials, now()),
        )
        capture_entity(conn, "task", task_id)
        capture_task_input(conn, task_id)
    return get_task(task_id)


def list_tasks(status: str | None = None) -> list[dict]:
    init_db()
    sql = "SELECT * FROM tasks"
    params: tuple = ()
    if status:
        if status not in {"pending", "completed"}:
            raise ValidationError("status 只能是 pending 或 completed")
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY created_at DESC"
    with connect() as conn:
        return [row_dict(row) for row in conn.execute(sql, params).fetchall()]


def get_task(task_id: str) -> dict:
    init_db()
    with connect() as conn:
        task = row_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if not task:
            raise KeyError(task_id)
        task["input_history"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_input_history WHERE task_id=? ORDER BY version DESC", (task_id,)
            ).fetchall()
        ]
        task["corrections"] = [
            row_dict(row)
            for row in conn.execute(
                "SELECT * FROM task_corrections WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        ]
        task["result_provenance_json"] = _decorate_staleness(task.get("result_provenance_json"))
    return task


def update_task_input(task_id: str, text: str, expected_version: int) -> dict:
    if not isinstance(text, str):
        raise ValidationError("用户输入必须是文本")
    clean = text.strip()
    timestamp = now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise KeyError(task_id)
        if task["status"] != "pending":
            raise ValidationError("只能修改待处理任务；已完成任务请追加用户校正")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or task["input_version"] != expected_version:
            raise ValidationError("任务输入已变化，请刷新页面后重试")
        if task["type"] == "material":
            if not clean:
                raise ValidationError("请输入现有食材和粗略数量")
            if len(clean) > 10000:
                raise ValidationError("原材料输入不能超过 10000 字")
        if clean != task["original_input"]:
            conn.execute(
                "INSERT INTO task_input_history(id,task_id,version,input_text,archived_at) VALUES(?,?,?,?,?)",
                (new_id("task_input"), task_id, task["input_version"], task["original_input"], timestamp),
            )
            updated = conn.execute(
                """UPDATE tasks SET original_input=?,input_version=input_version+1
                   WHERE id=? AND status='pending' AND input_version=?""",
                (clean, task_id, expected_version),
            )
            if updated.rowcount != 1:
                raise ValidationError("任务状态或输入已变化，请刷新页面后重试")
        capture_task_input(conn, task_id)
    return get_task(task_id)


def complete_task(
    task_id: str,
    result: dict,
    *,
    provenance_context: dict | None = None,
    agent_run_id: str | None = None,
    source_revisions: list[dict] | None = None,
    generator: dict | None = None,
) -> dict:
    task = get_task(task_id)
    if task["status"] == "completed":
        raise ValidationError("任务已完成；不得覆盖原结果，请新增用户校正")
    from .personalization import require_generation

    policy = require_generation(task["type"])
    context = provenance_context or task_context(task_id)
    validate_result(task["type"], result, fact_only=policy["fact_only"])
    from .planning import enrich_task_result

    stored_result = enrich_task_result(result, context)
    if source_revisions is None:
        source_revisions = context["source_revisions"]
    portable_provenance = _result_provenance(source_revisions, generator)
    timestamp = now()
    with connect() as conn:
        updated = conn.execute(
            """UPDATE tasks SET status='completed', result_json=?, result_provenance_json=?, result_version=1,
               schema_version=2,policy_version=?,validator_version=?,source_manifest_json=?,
               context_hash=?,agent_run_id=?,completed_at=? WHERE id=? AND status='pending'""",
            (
                json.dumps(stored_result, ensure_ascii=False),
                json.dumps(portable_provenance, ensure_ascii=False),
                policy["policy_version"],
                VALIDATOR_VERSION,
                json.dumps(_source_manifest_for_commit(context, agent_run_id), ensure_ascii=False, sort_keys=True),
                context.get("context_hash") or "",
                agent_run_id,
                timestamp,
                task_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValidationError("任务状态已变化，请重新读取")
        capture_entity(conn, "task", task_id)
        capture_derived_result(
            conn,
            source_entity_id=task_id,
            source_kind="task",
            result_version=1,
            result=stored_result,
            provenance=portable_provenance,
        )
    from .adaptive import linked_dates

    for observed_date in linked_dates(task_id):
        queue_review_for_external_change(observed_date, "task_evidence_completed")
    return get_task(task_id)


def generate_task_result(task_id: str, client=None) -> dict:
    from .ai import generate_json, provider_from_environment
    from .personalization import require_generation

    task = get_task(task_id)
    if task["status"] == "completed":
        raise ValidationError("任务已完成；不得覆盖原结果，请新增用户校正")
    require_generation(task["type"])
    context = task_context(task_id)
    provider = client or provider_from_environment()
    run_id = _start_agent_run(task["type"], context, provider)
    try:
        result = generate_json(context, task["type"], provider)
        completed = complete_task(
            task_id,
            result,
            provenance_context=context,
            agent_run_id=run_id,
            source_revisions=context["source_revisions"],
            generator=_generator_metadata(provider),
        )
    except Exception as exc:
        _finish_agent_run(run_id, error=exc)
        raise
    _finish_agent_run(run_id, result=result)
    return completed


def submit_task_result(task_id: str, result: dict) -> dict:
    task = get_task(task_id)
    context = task_context(task_id)
    run_id = _start_agent_run(
        task["type"], context, identity=("external_agent", "structured_json_submission")
    )
    try:
        completed = complete_task(task_id, result, provenance_context=context, agent_run_id=run_id)
    except Exception as exc:
        _finish_agent_run(run_id, error=exc)
        raise
    _finish_agent_run(run_id, result=result)
    return completed


def add_correction(task_id: str, correction: dict) -> dict:
    task = get_task(task_id)
    if task["status"] != "completed":
        raise ValidationError("只能校正已完成任务")
    if not isinstance(correction, dict) or not isinstance(correction.get("text"), str) or not correction["text"].strip():
        raise ValidationError("校正必须包含非空 text")
    correction_id = new_id("correction")
    payload = {**correction, "text": correction["text"].strip()}
    with connect() as conn:
        conn.execute(
            "INSERT INTO task_corrections(id,task_id,correction_json,created_at) VALUES(?,?,?,?)",
            (correction_id, task_id, json.dumps(payload, ensure_ascii=False), now()),
        )
        capture_correction(conn, correction_id)
    from .adaptive import linked_dates

    for observed_date in linked_dates(task_id):
        queue_review_for_external_change(observed_date, "task_evidence_corrected")
    return get_task(task_id)


FOOD_FIELDS = (
    "name", "brand", "basis", "energy_kcal", "protein_g", "carbs_g", "fat_g",
    "fiber_g", "sodium_mg", "serving_unit", "category", "menu_priority",
    "default_portion", "usage_rule", "source_key", "source_url", "package_photo_path", "notes",
)

FOOD_CATEGORIES = {"protein", "staple", "vegetable", "fruit", "fat", "snack", "flavor", "other"}
MENU_PRIORITIES = {"high", "normal", "low", "excluded"}


def _validate_food(item: dict) -> dict:
    clean = {field: item.get(field) for field in FOOD_FIELDS}
    clean["name"] = str(clean.get("name") or "").strip()
    if not clean["name"]:
        raise ValidationError("食品名称不能为空")
    clean["basis"] = clean.get("basis") or "100g"
    if clean["basis"] not in {"100g", "serving"}:
        raise ValidationError("营养基准只能是 100g 或 serving")
    for field in ("brand", "serving_unit", "default_portion", "usage_rule", "source_url", "notes"):
        clean[field] = str(clean.get(field) or "").strip()
    clean["source_key"] = str(clean.get("source_key") or "").strip() or None
    clean["category"] = str(clean.get("category") or "other").strip()
    if clean["category"] not in FOOD_CATEGORIES:
        raise ValidationError("食品类别无效")
    clean["menu_priority"] = str(clean.get("menu_priority") or "normal").strip()
    if clean["menu_priority"] not in MENU_PRIORITIES:
        raise ValidationError("菜单优先级无效")
    for field in ("energy_kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sodium_mg"):
        value = clean.get(field)
        if value is not None:
            value = float(value)
            if value < 0:
                raise ValidationError(f"{field} 不能为负数")
        clean[field] = value
    if clean["basis"] == "serving" and not clean["serving_unit"]:
        raise ValidationError("按份记录时必须填写份量单位")
    return clean


def _food_history(conn, food_id: str, event: str, before: dict | None, after: dict | None) -> None:
    conn.execute(
        "INSERT INTO food_item_history(id,food_id,event,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
        (
            new_id("history"), food_id, event,
            json.dumps(before, ensure_ascii=False) if before else None,
            json.dumps(after, ensure_ascii=False) if after else None, now(),
        ),
    )


def create_food(item: dict) -> dict:
    init_db()
    clean = _validate_food(item)
    food_id, timestamp = new_id("food"), now()
    with connect() as conn:
        conn.execute(
            f"INSERT INTO food_items(id,{','.join(FOOD_FIELDS)},created_at,updated_at) VALUES({','.join('?' for _ in range(len(FOOD_FIELDS)+3))})",
            (food_id, *(clean[field] for field in FOOD_FIELDS), timestamp, timestamp),
        )
        after = dict(conn.execute("SELECT * FROM food_items WHERE id=?", (food_id,)).fetchone())
        _food_history(conn, food_id, "create", None, after)
        capture_entity(conn, "food_item", food_id)
    return get_food(food_id)


def list_foods(query: str = "") -> list[dict]:
    init_db()
    with connect() as conn:
        if query.strip():
            term = f"%{query.strip()}%"
            rows = conn.execute(
                "SELECT * FROM food_items WHERE deleted_at IS NULL AND (name LIKE ? OR brand LIKE ?) ORDER BY updated_at DESC",
                (term, term),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM food_items WHERE deleted_at IS NULL ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]


def list_priority_foods() -> list[dict]:
    init_db()
    with connect() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT * FROM food_items WHERE deleted_at IS NULL AND menu_priority='high'
               ORDER BY category,name,brand"""
        ).fetchall()]


def upsert_food_by_source(item: dict) -> dict:
    clean = _validate_food(item)
    source_key = clean.get("source_key")
    if not source_key:
        raise ValidationError("固定食品补录必须提供 source_key")
    init_db()
    with connect() as conn:
        existing = conn.execute("SELECT id FROM food_items WHERE source_key=?", (source_key,)).fetchone()
    if existing:
        return update_food(existing["id"], clean)
    return create_food(clean)


def get_food(food_id: str, include_deleted: bool = False) -> dict:
    init_db()
    sql = "SELECT * FROM food_items WHERE id=?" + ("" if include_deleted else " AND deleted_at IS NULL")
    with connect() as conn:
        row = conn.execute(sql, (food_id,)).fetchone()
        if not row:
            raise KeyError(food_id)
        return dict(row)


def update_food(food_id: str, item: dict) -> dict:
    before = get_food(food_id)
    clean = _validate_food(item)
    timestamp = now()
    with connect() as conn:
        conn.execute(
            f"UPDATE food_items SET {','.join(f'{field}=?' for field in FOOD_FIELDS)},updated_at=? WHERE id=? AND deleted_at IS NULL",
            (*(clean[field] for field in FOOD_FIELDS), timestamp, food_id),
        )
        after = dict(conn.execute("SELECT * FROM food_items WHERE id=?", (food_id,)).fetchone())
        _food_history(conn, food_id, "update", before, after)
        capture_entity(conn, "food_item", food_id)
    return get_food(food_id)


def delete_food(food_id: str) -> None:
    before = get_food(food_id)
    timestamp = now()
    with connect() as conn:
        conn.execute("UPDATE food_items SET deleted_at=?,updated_at=? WHERE id=?", (timestamp, timestamp, food_id))
        after = dict(conn.execute("SELECT * FROM food_items WHERE id=?", (food_id,)).fetchone())
        _food_history(conn, food_id, "delete", before, after)
        capture_entity(conn, "food_item", food_id)


def _record_ids_for_date(conn, record_date: str) -> list[str]:
    return [row["id"] for row in conn.execute(
        "SELECT id FROM daily_records WHERE record_date=? ORDER BY created_at,id", (record_date,)
    ).fetchall()]


def _checkin_versions_for_date(conn, record_date: str) -> dict[str, int]:
    rows = conn.execute(
        """SELECT m.module_key,m.version FROM daily_checkin_modules m
           JOIN daily_checkins c ON c.id=m.checkin_id
           WHERE c.checkin_date=? AND m.version>0 ORDER BY m.module_key""",
        (record_date,),
    ).fetchall()
    return {row["module_key"]: row["version"] for row in rows}


def _evidence_link_ids_for_date(conn, record_date: str) -> list[str]:
    return [row["id"] for row in conn.execute(
        "SELECT id FROM task_evidence_links WHERE observed_date=? ORDER BY created_at,id", (record_date,)
    ).fetchall()]


def _has_daily_source(conn, record_date: str) -> bool:
    return bool(
        _record_ids_for_date(conn, record_date)
        or _checkin_versions_for_date(conn, record_date)
        or _evidence_link_ids_for_date(conn, record_date)
    )


def _queue_daily_review(conn, record_date: str, archive_reason: str = "new_daily_record") -> None:
    timestamp = now()
    source_ids = _record_ids_for_date(conn, record_date)
    source_json = json.dumps(source_ids, ensure_ascii=False)
    checkin_versions_json = json.dumps(_checkin_versions_for_date(conn, record_date), ensure_ascii=False, sort_keys=True)
    review = conn.execute("SELECT * FROM daily_reviews WHERE review_date=?", (record_date,)).fetchone()
    if review is None:
        conn.execute(
            """INSERT INTO daily_reviews(
                id,review_date,status,source_record_ids_json,source_checkin_versions_json,
                result_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (new_id("review"), record_date, "pending", source_json, checkin_versions_json, 0, timestamp, timestamp),
        )
        created = conn.execute("SELECT id FROM daily_reviews WHERE review_date=?", (record_date,)).fetchone()
        capture_entity(conn, "daily_review", created["id"], created_at=timestamp)
        return
    if review["status"] == "completed" and review["result_json"]:
        conn.execute(
            """INSERT INTO daily_review_history(
                id,review_id,version,source_record_ids_json,source_checkin_versions_json,
                result_json,result_provenance_json,completed_at,archived_at,archive_reason,schema_version,review_mode,
                source_manifest_json,context_hash,agent_run_id,policy_version,validator_version,plan_version_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("review_history"), review["id"], review["result_version"],
                review["source_record_ids_json"], review["source_checkin_versions_json"],
                review["result_json"], review["result_provenance_json"], review["completed_at"], timestamp, archive_reason,
                review["schema_version"], review["review_mode"], review["source_manifest_json"], review["context_hash"],
                review["agent_run_id"], review["policy_version"], review["validator_version"],
                review["plan_version_id"],
            ),
        )
    conn.execute(
        """UPDATE daily_reviews SET status='pending',source_record_ids_json=?,source_checkin_versions_json=?,
           result_json=NULL,result_provenance_json=NULL,source_manifest_json='{}',context_hash='',agent_run_id=NULL,
           policy_version='',validator_version='',plan_version_id=NULL,updated_at=?,completed_at=NULL
           WHERE review_date=?""",
        (source_json, checkin_versions_json, timestamp, record_date),
    )
    capture_entity(conn, "daily_review", review["id"], created_at=timestamp)


def queue_review_for_external_change(record_date: str, reason: str) -> dict:
    try:
        date.fromisoformat(record_date)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    reason = reason.strip()
    if not reason:
        raise ValidationError("重新排队必须说明原因")
    init_db()
    with connect() as conn:
        if not _has_daily_source(conn, record_date):
            raise ValidationError("该日期没有可用记录、状态或任务证据")
        _queue_daily_review(conn, record_date, reason)
    return get_daily_review(record_date)


def _validate_checkin_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    if parsed > configured_today():
        raise ValidationError("不能填写未来日期的每日状态")
    return value


def checkin_module_settings() -> list[dict]:
    init_db()
    with connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT module_key,enabled,sort_order,frequency,updated_at FROM checkin_module_settings ORDER BY sort_order,module_key"
        ).fetchall()]


def update_checkin_module_settings(items: list[dict]) -> list[dict]:
    if not isinstance(items, list) or {item.get("module_key") for item in items} != set(checkins.MODULE_BY_KEY):
        raise ValidationError("模块设置必须完整覆盖五个标准模块")
    cleaned = []
    for sort_order, item in enumerate(items):
        enabled = item.get("enabled")
        frequency = item.get("frequency")
        if not isinstance(enabled, bool) or frequency not in {"daily", "optional"}:
            raise ValidationError("模块设置值无效")
        cleaned.append((item["module_key"], int(enabled), sort_order, frequency))
    timestamp = now()
    init_db()
    with connect() as conn:
        for module_key, enabled, sort_order, frequency in cleaned:
            conn.execute(
                "UPDATE checkin_module_settings SET enabled=?,sort_order=?,frequency=?,updated_at=? WHERE module_key=?",
                (enabled, sort_order, frequency, timestamp, module_key),
            )
    return checkin_module_settings()


def _get_or_create_checkin(conn, checkin_date: str):
    row = conn.execute("SELECT * FROM daily_checkins WHERE checkin_date=?", (checkin_date,)).fetchone()
    if row is None:
        timestamp = now()
        checkin_id = new_id("checkin")
        conn.execute(
            "INSERT INTO daily_checkins(id,checkin_date,created_at,updated_at) VALUES(?,?,?,?)",
            (checkin_id, checkin_date, timestamp, timestamp),
        )
        row = conn.execute("SELECT * FROM daily_checkins WHERE id=?", (checkin_id,)).fetchone()
    return row


def _get_or_create_checkin_module(conn, checkin_date: str, module_key: str):
    checkins.module_definition(module_key)
    checkin = _get_or_create_checkin(conn, checkin_date)
    row = conn.execute(
        "SELECT * FROM daily_checkin_modules WHERE checkin_id=? AND module_key=?",
        (checkin["id"], module_key),
    ).fetchone()
    if row is None:
        timestamp = now()
        module_id = new_id("checkin_module")
        conn.execute(
            """INSERT INTO daily_checkin_modules(
                id,checkin_id,module_key,status,schema_version,version,created_at,updated_at
            ) VALUES(?,?,?,'not_started',?,0,?,?)""",
            (module_id, checkin["id"], module_key, checkins.SCHEMA_VERSION, timestamp, timestamp),
        )
        row = conn.execute("SELECT * FROM daily_checkin_modules WHERE id=?", (module_id,)).fetchone()
    return row


def _module_payload(row, include_draft: bool = True) -> dict:
    item = row_dict(row)
    answers = item.get("answers_json") or {}
    draft = item.get("draft_json") if include_draft else None
    item["summary"] = checkins.summarize(item["module_key"], answers, item["status"]) if item["version"] else ""
    item["has_draft"] = draft is not None
    item["active_answers"] = draft if draft is not None else answers
    item["next_question"] = checkins.next_question(item["module_key"], item["active_answers"])
    item["ready"] = item["next_question"] is None and bool(item["active_answers"])
    return item


def get_checkin_module(checkin_date: str, module_key: str) -> dict:
    _validate_checkin_date(checkin_date)
    checkins.module_definition(module_key)
    init_db()
    with connect() as conn:
        row = conn.execute(
            """SELECT m.* FROM daily_checkin_modules m JOIN daily_checkins c ON c.id=m.checkin_id
               WHERE c.checkin_date=? AND m.module_key=?""",
            (checkin_date, module_key),
        ).fetchone()
        if row is None:
            return {
                "module_key": module_key, "status": "not_started", "answers_json": {}, "draft_json": None,
                "schema_version": checkins.SCHEMA_VERSION, "version": 0, "summary": "", "has_draft": False,
                "active_answers": {}, "next_question": checkins.next_question(module_key, {}), "ready": False,
                "history": [],
            }
        payload = _module_payload(row)
        payload["history"] = [row_dict(item) for item in conn.execute(
            "SELECT * FROM daily_checkin_module_history WHERE module_id=? ORDER BY version", (row["id"],)
        ).fetchall()]
        return payload


def get_checkin_state(checkin_date: str) -> dict:
    _validate_checkin_date(checkin_date)
    init_db()
    settings = checkin_module_settings()
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.* FROM daily_checkin_modules m JOIN daily_checkins c ON c.id=m.checkin_id
               WHERE c.checkin_date=?""", (checkin_date,)
        ).fetchall()
    by_key = {row["module_key"]: _module_payload(row) for row in rows}
    modules = []
    for setting in settings:
        module_key = setting["module_key"]
        item = by_key.get(module_key) or {
            "module_key": module_key, "status": "not_started", "version": 0, "summary": "",
            "has_draft": False, "active_answers": {}, "answers_json": {}, "draft_json": None,
        }
        modules.append({**setting, **item, **checkins.module_definition(module_key)})
    due = [item for item in modules if item["enabled"] and item["frequency"] == "daily"]
    handled = [item for item in due if item["version"] > 0 and item["status"] in {"completed", "skipped"}]
    return {
        "date": checkin_date,
        "modules": modules,
        "coverage": {
            "due": len(due), "handled": len(handled),
            "completed": [item["module_key"] for item in due if item["status"] == "completed" and item["version"]],
            "skipped": [item["module_key"] for item in due if item["status"] == "skipped" and item["version"]],
            "missing": [item["module_key"] for item in due if not item["version"]],
        },
    }


def save_checkin_answer(checkin_date: str, module_key: str, question_id: str, value: object, expected_version: int) -> dict:
    _validate_checkin_date(checkin_date)
    init_db()
    with connect() as conn:
        row = _get_or_create_checkin_module(conn, checkin_date, module_key)
        if row["version"] != expected_version:
            raise ValidationError("答案版本已变化，请刷新页面后重试")
        current = row_dict(row)
        active = current.get("draft_json")
        if active is None:
            active = dict(current.get("answers_json") or {})
        question = checkins.question_definition(module_key, question_id, active)
        active[question_id] = checkins.validate_answer(question, value)
        active = checkins.prune_answers(module_key, active)
        status = current["status"] if current["version"] else "in_progress"
        timestamp = now()
        conn.execute(
            "UPDATE daily_checkin_modules SET status=?,draft_json=?,updated_at=? WHERE id=?",
            (status, json.dumps(active, ensure_ascii=False), timestamp, row["id"]),
        )
        conn.execute("UPDATE daily_checkins SET updated_at=? WHERE id=?", (timestamp, row["checkin_id"]))
        capture_entity(conn, "checkin_day", row["checkin_id"], created_at=timestamp)
    return get_checkin_module(checkin_date, module_key)


def complete_checkin_module(checkin_date: str, module_key: str, expected_version: int) -> dict:
    _validate_checkin_date(checkin_date)
    init_db()
    with connect() as conn:
        row = _get_or_create_checkin_module(conn, checkin_date, module_key)
        current = row_dict(row)
        if current["version"] != expected_version:
            raise ValidationError("答案版本已变化，请刷新页面后重试")
        if current.get("draft_json") is None:
            raise ValidationError("没有可提交的问答草稿")
        answers = checkins.validate_module_answers(module_key, current["draft_json"])
        timestamp = now()
        if current["version"] > 0:
            conn.execute(
                """INSERT INTO daily_checkin_module_history(
                    id,module_id,version,status,answers_json,archived_at,archive_reason
                ) VALUES(?,?,?,?,?,?,?)""",
                (new_id("checkin_history"), row["id"], current["version"], current["status"],
                 json.dumps(current.get("answers_json") or {}, ensure_ascii=False), timestamp, "module_updated"),
            )
        conn.execute(
            """UPDATE daily_checkin_modules SET status='completed',answers_json=?,draft_json=NULL,
               schema_version=?,version=version+1,updated_at=?,completed_at=? WHERE id=?""",
            (json.dumps(answers, ensure_ascii=False), checkins.SCHEMA_VERSION, timestamp, timestamp, row["id"]),
        )
        conn.execute("UPDATE daily_checkins SET updated_at=? WHERE id=?", (timestamp, row["checkin_id"]))
        capture_entity(conn, "checkin_day", row["checkin_id"], created_at=timestamp)
        _queue_daily_review(conn, checkin_date, "checkin_module_updated")
    return get_checkin_module(checkin_date, module_key)


def skip_checkin_module(checkin_date: str, module_key: str, expected_version: int) -> dict:
    _validate_checkin_date(checkin_date)
    init_db()
    with connect() as conn:
        row = _get_or_create_checkin_module(conn, checkin_date, module_key)
        current = row_dict(row)
        if current["version"] != expected_version:
            raise ValidationError("答案版本已变化，请刷新页面后重试")
        timestamp = now()
        if current["version"] > 0:
            conn.execute(
                """INSERT INTO daily_checkin_module_history(
                    id,module_id,version,status,answers_json,archived_at,archive_reason
                ) VALUES(?,?,?,?,?,?,?)""",
                (new_id("checkin_history"), row["id"], current["version"], current["status"],
                 json.dumps(current.get("answers_json") or {}, ensure_ascii=False), timestamp, "module_skipped"),
            )
        conn.execute(
            """UPDATE daily_checkin_modules SET status='skipped',answers_json=NULL,draft_json=NULL,
               schema_version=?,version=version+1,updated_at=?,completed_at=? WHERE id=?""",
            (checkins.SCHEMA_VERSION, timestamp, timestamp, row["id"]),
        )
        conn.execute("UPDATE daily_checkins SET updated_at=? WHERE id=?", (timestamp, row["checkin_id"]))
        capture_entity(conn, "checkin_day", row["checkin_id"], created_at=timestamp)
        _queue_daily_review(conn, checkin_date, "checkin_module_skipped")
    return get_checkin_module(checkin_date, module_key)


def discard_checkin_draft(checkin_date: str, module_key: str, expected_version: int) -> dict:
    _validate_checkin_date(checkin_date)
    init_db()
    with connect() as conn:
        row = _get_or_create_checkin_module(conn, checkin_date, module_key)
        if row["version"] != expected_version:
            raise ValidationError("答案版本已变化，请刷新页面后重试")
        status = row["status"] if row["version"] else "not_started"
        conn.execute(
            "UPDATE daily_checkin_modules SET status=?,draft_json=NULL,updated_at=? WHERE id=?",
            (status, now(), row["id"]),
        )
        capture_entity(conn, "checkin_day", row["checkin_id"])
    return get_checkin_module(checkin_date, module_key)


def add_daily_record(record_date: str, raw_input: str, structured: dict | None = None) -> dict:
    try:
        date.fromisoformat(record_date)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    if not raw_input.strip():
        raise ValidationError("每日记录不能为空")
    item = (new_id("record"), record_date, raw_input.strip(), json.dumps(structured, ensure_ascii=False) if structured else None, now())
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO daily_records VALUES(?,?,?,?,?)", item)
        capture_entity(conn, "daily_record", item[0], created_at=item[4])
        _queue_daily_review(conn, record_date)
    return {"id": item[0], "record_date": item[1], "raw_input": item[2], "structured_json": structured, "created_at": item[4]}


def ensure_daily_review(review_date: str) -> dict:
    try:
        date.fromisoformat(review_date)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    init_db()
    with connect() as conn:
        if not _has_daily_source(conn, review_date):
            raise ValidationError("该日期没有每日记录、已发布状态或任务证据")
        existing = conn.execute("SELECT id FROM daily_reviews WHERE review_date=?", (review_date,)).fetchone()
        if existing is None:
            _queue_daily_review(conn, review_date)
    return get_daily_review(review_date)


def requeue_daily_review(review_date: str, reason: str) -> dict:
    review = get_daily_review(review_date)
    if review["status"] != "completed" or not review["result_json"]:
        raise ValidationError("只有已完成复盘可以重新排队")
    reason = reason.strip()
    if not reason:
        raise ValidationError("重新排队必须说明原因")
    timestamp = now()
    with connect() as conn:
        current = conn.execute("SELECT * FROM daily_reviews WHERE review_date=?", (review_date,)).fetchone()
        conn.execute(
            """INSERT INTO daily_review_history(
                id,review_id,version,source_record_ids_json,source_checkin_versions_json,
                result_json,result_provenance_json,completed_at,archived_at,archive_reason,schema_version,review_mode,
                source_manifest_json,context_hash,agent_run_id,policy_version,validator_version,plan_version_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("review_history"), current["id"], current["result_version"],
                current["source_record_ids_json"], current["source_checkin_versions_json"],
                current["result_json"], current["result_provenance_json"], current["completed_at"], timestamp, reason,
                current["schema_version"], current["review_mode"], current["source_manifest_json"], current["context_hash"],
                current["agent_run_id"], current["policy_version"], current["validator_version"],
                current["plan_version_id"],
            ),
        )
        conn.execute(
            """UPDATE daily_reviews SET status='pending',result_json=NULL,result_provenance_json=NULL,source_manifest_json='{}',context_hash='',
                   agent_run_id=NULL,policy_version='',validator_version='',plan_version_id=NULL,
                   updated_at=?,completed_at=NULL
               WHERE review_date=?""", (timestamp, review_date)
        )
        capture_entity(conn, "daily_review", current["id"], created_at=timestamp)
    return get_daily_review(review_date)


def get_daily_review(review_date: str) -> dict:
    init_db()
    with connect() as conn:
        review = row_dict(conn.execute("SELECT * FROM daily_reviews WHERE review_date=?", (review_date,)).fetchone())
        if review is None:
            raise KeyError(review_date)
        review["history"] = [row_dict(row) for row in conn.execute(
            "SELECT * FROM daily_review_history WHERE review_id=? ORDER BY version", (review["id"],)
        ).fetchall()]
        review["result_provenance_json"] = _decorate_staleness(
            review.get("result_provenance_json")
        )
        return review


def list_daily_reviews(status: str | None = None) -> list[dict]:
    init_db()
    sql, params = "SELECT * FROM daily_reviews", ()
    if status:
        if status not in {"pending", "completed"}:
            raise ValidationError("status 只能是 pending 或 completed")
        sql, params = sql + " WHERE status=?", (status,)
    sql += " ORDER BY review_date DESC"
    with connect() as conn:
        return [row_dict(row) for row in conn.execute(sql, params).fetchall()]


def daily_review_schema(settings: dict | None = None) -> dict:
    settings = settings or load_resolved_settings()
    schema = {
        "system_status": "stable|observe|adjust|risk",
        "facts": ["string"],
        "inferences": ["string"],
        "core_advice": ["1–3条可执行建议"],
        "do_not_adjust": ["string"],
        "risk_signals": ["string"],
        "priority_food_decisions": [{"food_id": "string", "decision": "use|skip", "reason": "string"}],
        "tomorrow_menu": {
            "date": "YYYY-MM-DD",
            "environment": settings["meal_environment"],
            "protein_target_g": settings["protein_target_g"],
            "meals": [{
                "name": "早餐|午餐|晚餐", "foods": ["string"],
                "portion_guidance": "string", "protein_g": [0, 0], "substitutions": ["string"],
            }],
            "conditional_snack": {"condition": "string", "options": ["string"]},
            "training_adjustment": "string",
            "gut_adjustment": "string",
        },
        "one_line_review": "string",
    }
    home = settings.get("home_cooking") or {"enabled": False}
    meal_modes = settings.get("meal_modes") or legacy_home_meal_modes(home)
    if home.get("enabled"):
        meal_shape = schema["tomorrow_menu"]["meals"][0]
        recipe_card = {
            "title": "string", "servings": 1, "active_minutes": 1,
            "total_minutes": home["weekday_time_limit_minutes"],
            "cookware": home["equipment"][:2],
            "ingredients": [{"name": "string", "amount": "string", "prep": "string"}],
            "seasonings": [{"name": "string", "amount": "string", "timing": "string"}],
            "steps": [{"instruction": "string", "minutes": 1, "heat": "string", "done_signal": "string"}],
            "failure_rescue": ["string"], "cleanup": "string", "gut_fallback": "string",
        }
        rotation_card = {
            "dish_key": "stable_string", "primary_protein": "string",
            "primary_vegetable": "string", "flavor_profile": "stable_string",
            "technique": "stable_string",
            "repeat_reason": "omit unless health_recovery|ingredient_expiry|shopping_constraint",
        }
        eat_out_guidance = {
            "protein_anchor": "string", "staple": "string", "vegetables": "string",
            "sauce_rule": "string", "fallback": "string",
        }
        schema["tomorrow_menu"]["meals"] = []
        for key in MEAL_KEYS:
            name, mode = MEAL_NAMES[key], meal_modes[key]
            meal_spec = {**meal_shape, "name": name, "mode": mode}
            if mode == "home_cook":
                meal_spec.update({"recipe_card": recipe_card, "rotation": rotation_card})
            elif mode == "eat_out":
                meal_spec["eat_out_guidance"] = eat_out_guidance
            schema["tomorrow_menu"]["meals"].append(meal_spec)
        schema["tomorrow_menu"].update({
            "shopping_list": [{
                "name": "string", "amount": "string", "purpose": "string", "required": True,
                "selection_guide": "string", "storage": "string",
            }],
            "online_options": [{
                "category": "string", "selection_criteria": ["string"], "package_size": "string",
                "search_keywords": ["string"], "pairs_with": ["string"], "skip_if": "string",
            }],
            "reuse_plan": {"horizon_days": home["rotation_window_days"], "items": [{
                "ingredient": "string", "tomorrow_use": "string",
                "later_uses": [{"date": "YYYY-MM-DD", "use": "string"}], "storage": "string",
            }]},
        })
        schema["ingredient_carryover_decisions"] = [{
            "carryover_id": "string", "ingredient": "string", "decision": "use|skip|discard",
            "reason": "string", "planned_use": "string",
        }]
    elif settings.get("meal_modes"):
        meal_shape = schema["tomorrow_menu"]["meals"][0]
        schema["tomorrow_menu"]["meals"] = []
        for key in MEAL_KEYS:
            meal_spec = {**meal_shape, "name": MEAL_NAMES[key], "mode": meal_modes[key]}
            if meal_modes[key] == "eat_out":
                meal_spec["eat_out_guidance"] = {
                    "protein_anchor": "string", "staple": "string", "vegetables": "string",
                    "sauce_rule": "string", "fallback": "string",
                }
            schema["tomorrow_menu"]["meals"].append(meal_spec)
    return schema


def _home_menu_history(rows: list) -> tuple[list[dict], list[str]]:
    meals, online_categories = [], []
    for row in rows:
        item = row_dict(row)
        result = item.get("result_json") or {}
        menu = result.get("tomorrow_menu") or {}
        for meal in menu.get("meals", []):
            if meal.get("mode") != "home_cook" and not meal.get("recipe_card"):
                continue
            recipe = meal.get("recipe_card") or {}
            meals.append({
                "review_date": item["review_date"],
                "menu_date": menu.get("date"),
                "meal_name": meal.get("name"),
                "meal_slot": MEAL_KEYS_BY_NAME.get(meal.get("name"), "unknown"),
                "title": recipe.get("title") or " / ".join(meal.get("foods", [])),
                "rotation": meal_rotation(menu, meal),
            })
        for option in menu.get("online_options", []):
            category = option.get("category")
            if category and category not in online_categories:
                online_categories.append(category)
    return meals, online_categories


def _carryover_id(source_key: str, menu_date: str, index: int, ingredient: str) -> str:
    raw = "|".join((source_key, menu_date, str(index), ingredient))
    return f"carryover_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def _shopping_matches(ingredient: str, reuse_item: dict, shopping_list: list) -> list[dict]:
    text = " ".join(
        str(part or "")
        for part in (
            ingredient,
            reuse_item.get("tomorrow_use"),
            reuse_item.get("storage"),
            " ".join(str(use.get("use") or "") for use in reuse_item.get("later_uses", []) if isinstance(use, dict)),
        )
    ).lower()
    matches = []
    for item in shopping_list:
        if not isinstance(item, dict) or not item.get("required"):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        normalized = name.lower()
        if normalized in text or ingredient.lower() in normalized:
            matches.append({
                "name": name,
                "amount": item.get("amount") or "",
                "purpose": item.get("purpose") or "",
                "storage": item.get("storage") or "",
            })
    return matches


def _ingredient_carryover_obligations(review_date: str, rows: list) -> list[dict]:
    current_date = date.fromisoformat(review_date)
    target_menu_date = current_date + timedelta(days=1)
    obligations = []
    for row in rows:
        item = row_dict(row)
        result = item.get("result_json") or {}
        menu = result.get("tomorrow_menu") or {}
        reuse = menu.get("reuse_plan") or {}
        try:
            source_menu_date = date.fromisoformat(menu.get("date", ""))
        except (TypeError, ValueError):
            continue
        horizon_days = reuse.get("horizon_days")
        if not isinstance(horizon_days, int) or isinstance(horizon_days, bool) or horizon_days <= 0:
            continue
        window_end = source_menu_date + timedelta(days=horizon_days - 1)
        if current_date > window_end:
            continue
        shopping_list = menu.get("shopping_list") or []
        source_key = item.get("id") or item["review_date"]
        for index, reuse_item in enumerate(reuse.get("items") or []):
            if not isinstance(reuse_item, dict):
                continue
            ingredient = str(reuse_item.get("ingredient") or "").strip()
            if not ingredient:
                continue
            shopping_items = _shopping_matches(ingredient, reuse_item, shopping_list)
            if not shopping_items:
                continue
            planned_uses = []
            for use in reuse_item.get("later_uses") or []:
                if not isinstance(use, dict):
                    continue
                try:
                    use_date = date.fromisoformat(str(use.get("date") or ""))
                except ValueError:
                    continue
                if current_date <= use_date <= target_menu_date and use_date <= window_end:
                    planned_uses.append({"date": use_date.isoformat(), "use": str(use.get("use") or "")})
            if not planned_uses:
                continue
            planned_uses.sort(key=lambda value: value["date"])
            planned_date = date.fromisoformat(planned_uses[0]["date"])
            if planned_date <= current_date:
                urgency = "due_today_or_confirm_used"
            elif planned_date == target_menu_date:
                urgency = "use_tomorrow"
            else:
                urgency = "within_reuse_window"
            obligations.append({
                "id": _carryover_id(source_key, menu.get("date") or "", index, ingredient),
                "source_review_date": item["review_date"],
                "source_menu_date": menu.get("date"),
                "ingredient": ingredient,
                "purchase_assumption": "上一轮 required 采购项可能已买；若今日记录明确否定，可在裁决中跳过或丢弃。",
                "shopping_items": shopping_items,
                "original_tomorrow_use": reuse_item.get("tomorrow_use") or "",
                "planned_use_date": planned_uses[0]["date"],
                "planned_use": planned_uses[0]["use"],
                "remaining_later_uses": planned_uses,
                "storage": reuse_item.get("storage") or "",
                "urgency": urgency,
                "reuse_window_end": window_end.isoformat(),
            })
    return obligations


def _recent_review_rows_for_carryover(review_date: str, days: int = 14) -> list:
    end = date.fromisoformat(review_date)
    cutoff = (end - timedelta(days=days - 1)).isoformat()
    with connect() as conn:
        return conn.execute(
            """SELECT id,review_date,result_json FROM daily_reviews
               WHERE status='completed' AND review_date BETWEEN ? AND ? AND review_date<?
               ORDER BY review_date DESC""",
            (cutoff, review_date, review_date),
        ).fetchall()


def _effective_meal_settings(settings: dict, planning_answers: list[dict], review_date: str) -> tuple[dict, dict]:
    defaults = settings.get("meal_modes") or legacy_home_meal_modes(settings.get("home_cooking"))
    effective = dict(defaults)
    overrides: dict[str, str] = {}
    source = None
    for event in planning_answers:
        if event.get("status") != "answered" or event.get("question_key") != "tomorrow_meal_modes":
            continue
        answer = event.get("answer_json") or {}
        if not isinstance(answer, dict):
            continue
        candidate = {
            key: answer.get(key) for key in MEAL_KEYS
            if answer.get(key) in {"home_cook", "quick_assembly", "eat_out"}
        }
        overrides.update(candidate)
        source = {"question_id": event["id"], "question_version": event["version"]}
    effective.update(overrides)
    resolved = deepcopy(settings)
    if overrides:
        resolved["meal_modes"] = effective
        home = deepcopy(resolved.get("home_cooking") or {})
        home["enabled"] = "home_cook" in effective.values()
        home["meal_modes"] = effective
        home_meals = [key for key in MEAL_KEYS if effective[key] == "home_cook"]
        home["meal_scope"] = (
            "dinner" if home_meals == ["dinner"] else
            "lunch_and_dinner" if home_meals == ["lunch", "dinner"] else
            "custom"
        )
        resolved["home_cooking"] = home
        resolved["meal_environment"] = meal_environment_for_modes(effective)
    resolution = {
        "target_date": (date.fromisoformat(review_date) + timedelta(days=1)).isoformat(),
        "default_meal_modes": defaults,
        "overrides": overrides,
        "effective_meal_modes": effective,
        "source": source,
    }
    return resolved, resolution


def daily_review_context(review_date: str, days: int = 14) -> dict:
    from .personalization import require_generation
    from .planning import PLANNING_POLICY_VERSION

    generation_policy = require_generation("daily")
    review = ensure_daily_review(review_date)
    end = date.fromisoformat(review_date)
    cutoff = (end - timedelta(days=days - 1)).isoformat()
    doctrine = load_doctrine()
    settings = load_resolved_settings()
    from . import adaptive
    planning_answers = adaptive.question_events_for_date(review_date, include_pending=False)
    settings, meal_mode_resolution = _effective_meal_settings(settings, planning_answers, review_date)
    with connect() as conn:
        records = [row_dict(row) for row in conn.execute(
            """SELECT * FROM daily_records WHERE record_date BETWEEN ? AND ?
               ORDER BY record_date,created_at""", (cutoff, review_date)
        ).fetchall()]
        memories = [dict(row) for row in conn.execute(
            "SELECT * FROM memories WHERE active=1 ORDER BY updated_at DESC"
        ).fetchall()]
        adjustments = [dict(row) for row in conn.execute(
            "SELECT * FROM adjustments WHERE active=1 ORDER BY updated_at DESC"
        ).fetchall()]
        checkin_rows = conn.execute(
            """SELECT c.id AS checkin_id,c.checkin_date,m.module_key,m.status,m.answers_json,m.version,m.completed_at
               FROM daily_checkins c JOIN daily_checkin_modules m ON m.checkin_id=c.id
               WHERE c.checkin_date BETWEEN ? AND ? AND m.version>0
               ORDER BY c.checkin_date,m.module_key""",
            (cutoff, review_date),
        ).fetchall()
        recent_review_rows = conn.execute(
            """SELECT id,review_date,result_json FROM daily_reviews
               WHERE status='completed' AND review_date BETWEEN ? AND ? AND review_date<?
               ORDER BY review_date DESC""",
            (cutoff, review_date, review_date),
        ).fetchall()
    recent_checkins = []
    for row in checkin_rows:
        item = row_dict(row)
        answers = item.get("answers_json") or {}
        item["summary"] = checkins.summarize(item["module_key"], answers, item["status"])
        recent_checkins.append(item)
    target_state = get_checkin_state(review_date)
    recent_home_meals, recent_online_categories = _home_menu_history(recent_review_rows)
    recent_home_dinners = [item for item in recent_home_meals if item.get("meal_name") == "晚餐"]
    home_cooking = settings.get("home_cooking") or {"enabled": False}
    carryover_obligations = (
        _ingredient_carryover_obligations(review_date, recent_review_rows)
        if home_cooking.get("enabled") else []
    )
    priority_foods = list_priority_foods()
    source_ids = {
        *(item["id"] for item in records),
        *(item["id"] for item in memories),
        *(item["id"] for item in adjustments),
        *(row["checkin_id"] for row in checkin_rows),
        *(row["id"] for row in recent_review_rows),
        *(item["id"] for item in priority_foods),
        *(preference_entity_id(kind) for kind in ("profile", "settings", "doctrine", "checkin_settings")),
    }
    target_modules = [
        {
            "module_key": item["module_key"], "label": item["label"], "status": item["status"],
            "version": item["version"], "answers": item.get("answers_json") or None,
            "summary": item["summary"] or None,
        }
        for item in target_state["modules"] if item["version"] > 0
    ]
    from .personalization import active_personalization

    personalization = active_personalization()
    evidence = [item for item in adaptive.meal_evidence(cutoff, review_date) if item["status"] == "completed"]
    execution_feedback = adaptive.list_plan_feedback(cutoff, review_date)
    adaptations = adaptive.active_adaptations(review_date)
    inventory = adaptive.list_inventory()
    experiment = adaptive.active_experiment()
    source_manifest = {
        "records": [item["id"] for item in records],
        "memories": [{"id": item["id"], "updated_at": item["updated_at"]} for item in memories],
        "adjustments": [{"id": item["id"], "updated_at": item["updated_at"]} for item in adjustments],
        "priority_foods": [{"id": item["id"], "updated_at": item["updated_at"]} for item in list_priority_foods()],
        "checkins": [{"date": item["checkin_date"], "module": item["module_key"], "version": item["version"]} for item in recent_checkins],
        "meal_evidence": [{
            "link_id": item["id"], "task_id": item["task_id"], "result_version": item["result_version"],
            "correction_ids": [correction["id"] for correction in item["corrections"]],
        } for item in evidence],
        "profile": ({
            "id": personalization["profile"]["id"],
            "version": personalization["profile"]["version"],
            "safety_mode": personalization["safety"]["mode"],
        } if personalization.get("profile") else None),
        "goals": [{"id": item["id"], "version": item["version"]} for item in personalization.get("goals") or []],
        "strategy": ({"id": personalization["strategy"]["id"], "version": personalization["strategy"]["version"]} if personalization.get("strategy") else None),
        "targets": [{
            "id": item["id"], "key": item["target_key"], "version": item["version"],
            "policy_version": item["policy_version"], "source_kind": item["source_kind"],
        } for item in personalization.get("targets") or []],
        "feedback": [{"id": item["id"], "version": item["version"]} for item in execution_feedback],
        "questions": [{
            "id": item["id"], "key": item["question_key"], "version": item["version"], "status": item["status"],
        } for item in planning_answers],
        "rules": [{
            "id": item["id"], "version": item["version"], "updated_at": item["updated_at"],
            "scope_policy_version": item.get("scope_policy_version") or "",
            "strategy_version_id": item.get("strategy_version_id"),
        } for item in adaptations["confirmed_rules"]],
        "inventory": [{"id": item["id"], "version": item["version"]} for item in inventory],
        "experiment": ({
            "id": experiment["id"], "version": experiment["version"], "updated_at": experiment["updated_at"],
            "scope_policy_version": experiment.get("scope_policy_version") or "",
        } if experiment else None),
        "doctrine": {
            "mode": doctrine["mode"],
            "sha256": hashlib.sha256(doctrine["content"].encode("utf-8")).hexdigest(),
        },
        "policy_version": generation_policy["policy_version"],
        "context_schema_version": 2,
        "result_schema_version": 2,
        "validator_version": VALIDATOR_VERSION,
        "planning_policy_version": PLANNING_POLICY_VERSION,
        "agent_run_id": None,
    }
    context = {
        "context_schema_version": 2,
        "result_schema_version": 2,
        "generation_policy": generation_policy,
        "daily_review": review,
        "doctrine": doctrine,
        "recent_days": days,
        "recent_records": records,
        "target_checkin": {"date": review_date, "modules": target_modules},
        "checkin_coverage": target_state["coverage"],
        "recent_checkins": recent_checkins,
        "checkin_resolution_note": (
            "同一日期、同一模块以最新已发布问答为准；它可以补充早期记录中的‘未提供’，"
            "但跳过和缺失仍表示未知。若实际值互相冲突，必须同时说明来源与时间，不得静默覆盖。"
        ),
        "long_term_memories": memories,
        "current_adjustments": adjustments,
        "priority_foods": priority_foods,
        "source_revisions": _revision_references(source_ids),
        "settings": settings,
        "meal_mode_resolution": meal_mode_resolution,
        "home_cooking_preferences": home_cooking,
        "meal_modes": meal_mode_resolution["effective_meal_modes"],
        "recent_home_meals": recent_home_meals,
        "recent_home_dinners": recent_home_dinners,
        "recent_online_categories": recent_online_categories,
        "ingredient_carryover_obligations": carryover_obligations,
        "safety": personalization["safety"],
        "active_profile": personalization.get("profile"),
        "current_goals": personalization.get("goals") or [],
        "active_strategy": personalization.get("strategy"),
        "meal_evidence": evidence,
        "recent_execution_feedback": execution_feedback,
        "confirmed_rules": adaptations["confirmed_rules"],
        "transient_adaptations": adaptations["transient"],
        "inventory": inventory,
        "active_experiment": experiment,
        "planning_answers": planning_answers,
        "source_manifest": source_manifest,
        "home_cooking_generation_protocol": [
            "逐餐严格读取 effective_meal_modes；单日明确安排优先于个人默认。每个 home_cook 餐次分别生成一人份新手执行卡，quick_assembly 使用低摩擦组装，eat_out 给外食选择规则且不得生成 recipe_card。",
            "优先复用食材但按餐次轮换菜式；同一餐次连续两天不重复 dish_key 或 flavor_profile。",
            "生成明日菜单前必须逐项评估 ingredient_carryover_obligations；未过期且适合当前状态的食材优先组装进明日午餐或晚餐。",
            "临近过期的食材优先明日用完；已超过保存窗口、今日记录否定库存或与肠胃状态冲突时可跳过或丢弃，但必须说明原因。",
            "新采购清单不得在可用剩余食材能完成同等功能时重复购买。",
            "每个自炊餐次总时间不得超过配置上限，最多两件主要炊具，每餐最多引入一个新技巧。",
            "正常肠胃可用番茄、小米椒、醋和蒜香建立酸辣风味；异常时保留香鲜并降低辣、酸、油。",
            "网购仅给规格、筛选标准和搜索关键词，不提供商品链接、价格或库存断言；近14天已推荐品类默认不重复。",
        ] if home_cooking.get("enabled") else [],
        "result_schema": daily_review_schema(settings),
    }
    context["context_hash"] = hashlib.sha256(
        json.dumps(context, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return context


def complete_daily_review(
    review_date: str,
    result: dict,
    *,
    provenance_context: dict | None = None,
    agent_run_id: str | None = None,
    source_revisions: list[dict] | None = None,
    generator: dict | None = None,
) -> dict:
    from .personalization import require_generation

    policy = require_generation("daily")
    review = ensure_daily_review(review_date)
    if review["status"] == "completed":
        raise ValidationError("该日期复盘已完成；新增每日记录后才可重新复盘")
    context = provenance_context or daily_review_context(review_date)
    settings = context["settings"]
    validate_daily_review_result(result, settings)
    _validate_home_meal_rotation(review_date, result, settings)
    expected_priority_ids = {food["id"] for food in list_priority_foods()}
    submitted_priority_ids = {item["food_id"] for item in result["priority_food_decisions"]}
    if submitted_priority_ids != expected_priority_ids:
        missing = sorted(expected_priority_ids - submitted_priority_ids)
        unknown = sorted(submitted_priority_ids - expected_priority_ids)
        raise ValidationError(f"优先食品裁决不完整；缺少={missing}，未知={unknown}")
    expected_menu_date = (date.fromisoformat(review_date) + timedelta(days=1)).isoformat()
    if result["tomorrow_menu"]["date"] != expected_menu_date:
        raise ValidationError(f"次日菜单日期必须是 {expected_menu_date}")
    home_cooking = settings.get("home_cooking") or {"enabled": False}
    if home_cooking.get("enabled"):
        obligations = _ingredient_carryover_obligations(
            review_date, _recent_review_rows_for_carryover(review_date)
        )
        _validate_ingredient_carryover_decisions(result, obligations)
    from .planning import validate_and_enrich_daily_result

    result = validate_and_enrich_daily_result(result, context)
    if source_revisions is None:
        source_revisions = context["source_revisions"]
    portable_provenance = _result_provenance(source_revisions, generator)
    stored_result = json.loads(json.dumps(result, ensure_ascii=False))
    timestamp = now()
    next_version = review["result_version"] + 1
    menu = result.get("tomorrow_menu") or {}
    for meal in menu.get("meals") or []:
        if not meal.get("plan_item_id"):
            meal["plan_item_id"] = "plan_" + hashlib.sha256(
                f"{review['id']}|{next_version}|{meal.get('name','meal')}".encode("utf-8")
            ).hexdigest()[:12]
        if not meal.get("strategy_key"):
            rotation = meal_rotation(menu, meal) or {}
            if rotation.get("dish_key"):
                meal["strategy_key"] = rotation["dish_key"]
            else:
                normalized = "|".join(sorted(str(item).strip().lower() for item in meal.get("foods") or []))
                meal["strategy_key"] = "meal_" + hashlib.sha256(
                    f"{meal.get('name','meal')}|{normalized}".encode("utf-8")
                ).hexdigest()[:12]
    safety_mode = (context.get("safety") or {}).get("mode") or "setup_required"
    from .planning import MEAL_SLOTS

    plan_version_id = new_id("plan_version")
    plan_date = menu["date"]
    committed_manifest = _source_manifest_for_commit(context, agent_run_id)
    with connect() as conn:
        updated = conn.execute(
            """UPDATE daily_reviews SET status='completed',result_json=?,result_provenance_json=?,result_version=result_version+1,
               schema_version=2,review_mode=?,source_manifest_json=?,context_hash=?,agent_run_id=?,
               policy_version=?,validator_version=?,plan_version_id=?,updated_at=?,completed_at=?
               WHERE review_date=? AND status='pending'""",
            (
                json.dumps(stored_result, ensure_ascii=False),
                json.dumps(portable_provenance, ensure_ascii=False),
                safety_mode,
                json.dumps(_source_manifest_for_commit(context, agent_run_id), ensure_ascii=False, sort_keys=True), context["context_hash"],
                agent_run_id, policy["policy_version"], VALIDATOR_VERSION,
                plan_version_id, timestamp, timestamp, review_date,
            ),
        )
        if updated.rowcount != 1:
            raise ValidationError("复盘状态已变化，请重新读取")
        conn.execute(
            "UPDATE plan_versions SET status='superseded' WHERE plan_date=? AND status='published'",
            (plan_date,),
        )
        conn.execute(
            """INSERT INTO plan_versions(
                   id,review_id,result_version,plan_date,status,schema_version,menu_json,
                   source_manifest_json,context_hash,policy_version,validator_version,agent_run_id,created_at
               ) VALUES(?,?,?,?,'published',2,?,?,?,?,?,?,?)""",
            (
                plan_version_id,
                review["id"],
                next_version,
                plan_date,
                json.dumps(menu, ensure_ascii=False),
                json.dumps(committed_manifest, ensure_ascii=False, sort_keys=True),
                context["context_hash"],
                policy["policy_version"],
                VALIDATOR_VERSION,
                agent_run_id,
                timestamp,
            ),
        )
        for sort_order, meal in enumerate(menu.get("meals") or []):
            conn.execute(
                """INSERT INTO plan_items(
                       id,plan_version_id,meal_slot,name,strategy_key,item_json,sort_order,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    meal["plan_item_id"],
                    plan_version_id,
                    MEAL_SLOTS.get(meal.get("name"), "other"),
                    meal.get("name") or "未命名餐次",
                    meal.get("strategy_key") or "",
                    json.dumps(meal, ensure_ascii=False),
                    sort_order,
                    timestamp,
                ),
            )
        current = conn.execute("SELECT id FROM daily_reviews WHERE review_date=?", (review_date,)).fetchone()
        capture_entity(conn, "daily_review", current["id"], created_at=timestamp)
        capture_derived_result(
            conn,
            source_entity_id=current["id"],
            source_kind="daily_review",
            result_version=review["result_version"] + 1,
            result=stored_result,
            provenance=portable_provenance,
        )
    return get_daily_review(review_date)


def generate_daily_review(review_date: str, client=None) -> dict:
    from .ai import generate_json, provider_from_environment
    from .personalization import require_generation

    review = ensure_daily_review(review_date)
    if review["status"] == "completed":
        raise ValidationError("该日期复盘已完成；新增每日记录后才可重新复盘")
    require_generation("daily")
    context = daily_review_context(review_date)
    provider = client or provider_from_environment()
    run_id = _start_agent_run("daily", context, provider)
    try:
        result = generate_json(context, "daily", provider)
        completed = complete_daily_review(
            review_date,
            result,
            provenance_context=context,
            agent_run_id=run_id,
            source_revisions=context["source_revisions"],
            generator=_generator_metadata(provider),
        )
    except Exception as exc:
        _finish_agent_run(run_id, error=exc)
        raise
    _finish_agent_run(run_id, result=result)
    return completed


def submit_daily_review(review_date: str, result: dict) -> dict:
    context = daily_review_context(review_date)
    run_id = _start_agent_run(
        "daily", context, identity=("external_agent", "structured_json_submission")
    )
    try:
        completed = complete_daily_review(
            review_date, result, provenance_context=context, agent_run_id=run_id
        )
    except Exception as exc:
        _finish_agent_run(run_id, error=exc)
        raise
    _finish_agent_run(run_id, result=result)
    return completed


def rescue_context(rescue_id: str) -> dict:
    from . import adaptive
    from .personalization import active_personalization, require_generation
    from .planning import PLANNING_POLICY_VERSION, compile_constraints

    policy = require_generation("rescue")
    session = adaptive.get_rescue_session(rescue_id)
    plan = adaptive.get_plan_for_date(session["plan_date"])
    if not plan:
        raise ValidationError("救场对应的正式计划不存在")
    plan_item = next(
        (item for item in plan["menu"]["meals"] if item["plan_item_id"] == session["plan_item_id"]),
        None,
    )
    if not plan_item:
        raise ValidationError("救场对应的计划项目不存在")
    personalization = active_personalization()
    adaptations = adaptive.active_adaptations(session["plan_date"])
    inventory = adaptive.list_inventory()
    doctrine = load_doctrine()
    context = {
        "context_schema_version": 2,
        "result_schema_version": 2,
        "generation_policy": policy,
        "doctrine": doctrine,
        "rescue_session": session,
        "plan": plan,
        "plan_item": plan_item,
        "settings": load_resolved_settings(),
        "safety": personalization["safety"],
        "active_profile": personalization.get("profile"),
        "current_goals": personalization.get("goals") or [],
        "active_strategy": personalization.get("strategy"),
        "active_targets": personalization.get("targets") or [],
        "confirmed_rules": adaptations["confirmed_rules"],
        "inventory": inventory,
        "result_schema": {
            "reason": "string",
            "steps": ["string"],
            "replacement_foods": ["string"],
            "portion_change": "string",
            "safety_notes": ["string"],
        },
    }
    context["compiled_constraints"] = compile_constraints(context)
    context["source_manifest"] = {
        "rescue_session": {"id": session["id"], "plan_item_id": session["plan_item_id"]},
        "plan": {
            "plan_version_id": plan.get("plan_version_id"),
            "review_id": plan["review_id"],
            "result_version": plan["result_version"],
            "context_hash": plan.get("context_hash") or "",
        },
        "profile": ({
            "id": personalization["profile"]["id"],
            "version": personalization["profile"]["version"],
            "safety_mode": personalization["safety"]["mode"],
        } if personalization.get("profile") else None),
        "goals": [{"id": item["id"], "version": item["version"]} for item in personalization.get("goals") or []],
        "strategy": ({"id": personalization["strategy"]["id"], "version": personalization["strategy"]["version"]} if personalization.get("strategy") else None),
        "targets": [{"id": item["id"], "key": item["target_key"], "version": item["version"]} for item in personalization.get("targets") or []],
        "rules": [{"id": item["id"], "version": item["version"], "updated_at": item["updated_at"]} for item in adaptations["confirmed_rules"]],
        "inventory": [{"id": item["id"], "version": item["version"]} for item in inventory],
        "doctrine": {
            "mode": doctrine["mode"],
            "sha256": hashlib.sha256(doctrine["content"].encode("utf-8")).hexdigest(),
        },
        "policy_version": policy["policy_version"],
        "planning_policy_version": PLANNING_POLICY_VERSION,
        "context_schema_version": 2,
        "result_schema_version": 2,
        "validator_version": VALIDATOR_VERSION,
        "agent_run_id": None,
    }
    context["context_hash"] = hashlib.sha256(
        json.dumps(context, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return context


def generate_rescue(rescue_id: str, client=None) -> dict:
    from . import adaptive
    from .ai import generate_json

    context = rescue_context(rescue_id)
    run_id = _start_agent_run("rescue", context, client)
    try:
        result = generate_json(context, "rescue", client)
        completed = adaptive.complete_rescue_session(
            rescue_id,
            result,
            provenance_context=context,
            agent_run_id=run_id,
        )
    except Exception as exc:
        _finish_agent_run(run_id, error=exc)
        raise
    _finish_agent_run(run_id, result=result)
    return completed


def submit_rescue_result(rescue_id: str, result: dict) -> dict:
    from . import adaptive

    context = rescue_context(rescue_id)
    run_id = _start_agent_run(
        "rescue", context, identity=("external_agent", "structured_json_submission")
    )
    try:
        completed = adaptive.complete_rescue_session(
            rescue_id, result, provenance_context=context, agent_run_id=run_id
        )
    except Exception as exc:
        _finish_agent_run(run_id, error=exc)
        raise
    _finish_agent_run(run_id, result=result)
    return completed


def _non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} 必须是非空文本")
    return value.strip()


def _validate_ingredient_carryover_decisions(result: dict, obligations: list[dict]) -> None:
    decisions = result.get("ingredient_carryover_decisions")
    if not obligations and decisions is None:
        return
    if obligations and decisions is None:
        decisions = []
    if not isinstance(decisions, list):
        raise ValidationError("ingredient_carryover_decisions 必须是数组")
    expected = {item["id"]: item for item in obligations}
    seen = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise ValidationError(f"ingredient_carryover_decisions[{index}] 必须是对象")
        carryover_id = _non_empty_text(
            decision.get("carryover_id"), f"ingredient_carryover_decisions[{index}].carryover_id"
        )
        if carryover_id in seen:
            raise ValidationError(f"ingredient_carryover_decisions 中重复承接ID：{carryover_id}")
        seen.add(carryover_id)
        ingredient = _non_empty_text(
            decision.get("ingredient"), f"ingredient_carryover_decisions[{index}].ingredient"
        )
        action = _non_empty_text(
            decision.get("decision"), f"ingredient_carryover_decisions[{index}].decision"
        )
        if action not in {"use", "skip", "discard"}:
            raise ValidationError("ingredient_carryover_decisions.decision 必须是 use、skip 或 discard")
        _non_empty_text(decision.get("reason"), f"ingredient_carryover_decisions[{index}].reason")
        _non_empty_text(decision.get("planned_use"), f"ingredient_carryover_decisions[{index}].planned_use")
        if carryover_id in expected and ingredient != expected[carryover_id]["ingredient"]:
            raise ValidationError(f"ingredient_carryover_decisions[{index}].ingredient 与承接食材不一致")
    if seen != set(expected):
        missing = sorted(set(expected) - seen)
        unknown = sorted(seen - set(expected))
        raise ValidationError(f"食材承接裁决不完整；缺少={missing}，未知={unknown}")


def _validate_home_meal_rotation(review_date: str, result: dict, settings: dict) -> None:
    home = settings.get("home_cooking") or {"enabled": False}
    if not home.get("enabled"):
        return
    current_menu = result["tomorrow_menu"]
    with connect() as conn:
        previous_row = conn.execute(
            """SELECT review_date,result_json FROM daily_reviews
               WHERE status='completed' AND review_date<? ORDER BY review_date DESC LIMIT 1""",
            (review_date,),
        ).fetchone()
    if previous_row is None:
        return
    previous_result = row_dict(previous_row).get("result_json") or {}
    previous_menu = previous_result.get("tomorrow_menu") or {}
    current_date = date.fromisoformat(current_menu["date"])
    try:
        previous_date = date.fromisoformat(previous_menu["date"])
    except (KeyError, TypeError, ValueError):
        return
    if previous_date != current_date - timedelta(days=1):
        return
    current_meals = {meal.get("name"): meal for meal in current_menu.get("meals", [])}
    previous_meals = {meal.get("name"): meal for meal in previous_menu.get("meals", [])}
    for meal_name in home_cooked_meal_names(settings):
        current_meal = current_meals.get(meal_name) or {}
        previous_meal = previous_meals.get(meal_name) or {}
        current = meal_rotation(current_menu, current_meal)
        previous = meal_rotation(previous_menu, previous_meal)
        if not current or not previous:
            continue
        repeated = (
            previous.get("dish_key") == current.get("dish_key")
            or previous.get("flavor_profile") == current.get("flavor_profile")
        )
        if repeated and not current.get("repeat_reason"):
            raise ValidationError(f"连续{meal_name}不得重复菜品或主风味；确需重复时必须提供 repeat_reason")


def pending_work() -> dict:
    return {
        "tasks": list_tasks("pending"),
        "daily_reviews": list_daily_reviews("pending"),
    }


def daily_state(review_date: str | None = None) -> dict:
    target = review_date or configured_today().isoformat()
    init_db()
    with connect() as conn:
        review = row_dict(conn.execute(
            "SELECT * FROM daily_reviews WHERE review_date=?", (target,)
        ).fetchone())
        record_count = conn.execute(
            "SELECT COUNT(*) FROM daily_records WHERE record_date=?", (target,)
        ).fetchone()[0]
    if review is None:
        return {"date": target, "status": "unrecorded", "record_count": record_count, "review": None}
    review["result_provenance_json"] = _decorate_staleness(
        review.get("result_provenance_json")
    )
    return {"date": target, "status": review["status"], "record_count": record_count, "review": review}


def dashboard_snapshot(review_date: str | None = None, days: int = 14) -> dict:
    """Build a read-only dashboard projection without queueing or reopening work."""
    target = review_date or configured_today().isoformat()
    try:
        end = date.fromisoformat(target)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    if days < 1 or days > 31:
        raise ValidationError("趋势天数必须在 1–31 之间")

    start = end - timedelta(days=days - 1)
    timeline = {
        (start + timedelta(days=offset)).isoformat(): {
            "date": (start + timedelta(days=offset)).isoformat(),
            "modules": {},
        }
        for offset in range(days)
    }
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.checkin_date,m.module_key,m.status,m.answers_json,m.version
               FROM daily_checkins c JOIN daily_checkin_modules m ON m.checkin_id=c.id
               WHERE c.checkin_date BETWEEN ? AND ? AND m.version>0
               ORDER BY c.checkin_date,m.module_key""",
            (start.isoformat(), target),
        ).fetchall()

    for row in rows:
        item = row_dict(row)
        answers = item.get("answers_json") or {}
        module_key = item["module_key"]
        module = {
            "status": item["status"],
            "summary": checkins.summarize(module_key, answers, item["status"]),
        }
        if item["status"] == "completed":
            if module_key == "weight":
                module["measured"] = answers.get("measured")
                module["weight_kg"] = answers.get("weight_kg") if answers.get("measured") == "yes" else None
            elif module_key == "training":
                module["trained"] = answers.get("trained")
                module["effort"] = answers.get("effort")
            elif module_key == "hunger":
                level = answers.get("hunger_level")
                module["hunger_level"] = int(level) if isinstance(level, str) and level.isdigit() else None
                module["hunger_time"] = answers.get("hunger_time")
            elif module_key == "sleep":
                module["sleep_duration"] = answers.get("sleep_duration")
            elif module_key == "gut":
                module["gut_state"] = answers.get("gut_state")
        timeline[item["checkin_date"]]["modules"][module_key] = module

    work = pending_work()
    queue = [
        {
            "kind": "photo" if item["type"] == "photo" else "material",
            "label": "照片任务" if item["type"] == "photo" else "原材料分析",
            "evidence": item.get("original_input") or (Path(item["image_path"]).name if item.get("image_path") else "待补充"),
            "status": item["status"],
            "href": f'/tasks/{item["id"]}',
            "created_at": item["created_at"],
        }
        for item in work["tasks"]
    ]
    queue.extend(
        {
            "kind": "review",
            "label": "每日复盘",
            "evidence": item["review_date"],
            "status": item["status"],
            "href": f'/reviews/{item["review_date"]}',
            "created_at": item["updated_at"],
        }
        for item in work["daily_reviews"]
    )
    queue.sort(key=lambda item: item["created_at"], reverse=True)

    daily = daily_state(target)
    result = daily["review"].get("result_json") if daily["review"] else None
    return {
        "date": target,
        "daily": daily,
        "conclusion": (result or {}).get("one_line_review"),
        "core_advice": (result or {}).get("core_advice") or [],
        "tomorrow_menu": (result or {}).get("tomorrow_menu"),
        "checkin": get_checkin_state(target),
        "trend": list(timeline.values()),
        "queue": queue,
    }


def add_memory(kind: str, content: str, evidence: str = "") -> dict:
    if kind not in {"preference", "gut_trigger", "constraint", "other"}:
        raise ValidationError("长期记忆类型无效")
    if not content.strip():
        raise ValidationError("长期记忆内容不能为空")
    item = (new_id("memory"), kind, content.strip(), evidence.strip(), 1, now(), now())
    with connect() as conn:
        conn.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?)", item)
        capture_entity(conn, "memory", item[0], created_at=item[5])
    return {"id": item[0], "kind": kind, "content": item[2], "evidence": item[3]}


def add_adjustment(content: str, reason: str = "") -> dict:
    if not content.strip():
        raise ValidationError("当前调整不能为空")
    item = (new_id("adjustment"), content.strip(), reason.strip(), 1, now(), now())
    with connect() as conn:
        conn.execute("INSERT INTO adjustments VALUES(?,?,?,?,?,?)", item)
        capture_entity(conn, "adjustment", item[0], created_at=item[4])
    return {"id": item[0], "content": item[1], "reason": item[2]}


def overview() -> dict:
    init_db()
    with connect() as conn:
        return {
            "records": [row_dict(r) for r in conn.execute("SELECT * FROM daily_records ORDER BY record_date DESC,created_at DESC LIMIT 30")],
            "daily_reviews": [row_dict(r) for r in conn.execute("SELECT * FROM daily_reviews ORDER BY review_date DESC LIMIT 30")],
            "memories": [dict(r) for r in conn.execute("SELECT * FROM memories WHERE active=1 ORDER BY updated_at DESC")],
            "adjustments": [dict(r) for r in conn.execute("SELECT * FROM adjustments WHERE active=1 ORDER BY updated_at DESC")],
        }


def task_context(task_id: str, days: int = 14) -> dict:
    task = get_task(task_id)
    from .adaptive import active_adaptations, list_inventory, task_evidence_links
    from .personalization import active_personalization, require_generation

    generation_policy = require_generation(task["type"])
    evidence_links = task_evidence_links(task_id)
    anchor = max(
        (date.fromisoformat(item["observed_date"]) for item in evidence_links),
        default=configured_today(),
    )
    cutoff = (anchor - timedelta(days=days - 1)).isoformat()
    doctrine = load_doctrine()
    personalization = active_personalization()
    with connect() as conn:
        records = [row_dict(r) for r in conn.execute(
            "SELECT * FROM daily_records WHERE record_date>=? ORDER BY record_date,created_at", (cutoff,)
        )]
        memories = [dict(r) for r in conn.execute("SELECT * FROM memories WHERE active=1 ORDER BY updated_at DESC")]
        adjustments = [dict(r) for r in conn.execute("SELECT * FROM adjustments WHERE active=1 ORDER BY updated_at DESC")]
    all_foods = list_foods()
    if task["type"] == "material":
        source = task["original_input"].lower()
        matches = [f for f in all_foods if f["name"].lower() in source or (f["brand"] and f["brand"].lower() in source)]
    else:
        matches = all_foods
    if task.get("image_path"):
        task["image_path"] = str(resolve_data_path(task["image_path"]))
    adaptations = active_adaptations(anchor.isoformat())
    inventory = list_inventory()
    source_manifest = {
        "task": {"id": task["id"], "input_version": task.get("input_version", 1)},
        "records": [item["id"] for item in records],
        "evidence_links": [item["id"] for item in evidence_links],
        "foods": [{"id": item["id"], "updated_at": item["updated_at"]} for item in matches],
        "memories": [{"id": item["id"], "updated_at": item["updated_at"]} for item in memories],
        "adjustments": [{"id": item["id"], "updated_at": item["updated_at"]} for item in adjustments],
        "profile": ({"id": personalization["profile"]["id"], "version": personalization["profile"]["version"]} if personalization.get("profile") else None),
        "goals": [{"id": item["id"], "version": item["version"]} for item in personalization.get("goals") or []],
        "strategy": ({"id": personalization["strategy"]["id"], "version": personalization["strategy"]["version"]} if personalization.get("strategy") else None),
        "targets": [{"id": item["id"], "key": item["target_key"], "version": item["version"]} for item in personalization.get("targets") or []],
        "rules": [{"id": item["id"], "version": item["version"], "updated_at": item["updated_at"]} for item in adaptations["confirmed_rules"]],
        "inventory": [{"id": item["id"], "version": item["version"]} for item in inventory],
        "doctrine": {
            "mode": doctrine["mode"],
            "sha256": hashlib.sha256(doctrine["content"].encode("utf-8")).hexdigest(),
        },
        "policy_version": generation_policy["policy_version"],
        "context_schema_version": 2,
        "result_schema_version": 2,
        "validator_version": VALIDATOR_VERSION,
        "agent_run_id": None,
    }
    source_ids = {
        task_input_entity_id(task_id),
        *(item["id"] for item in records),
        *(item["id"] for item in matches),
        *(item["id"] for item in memories),
        *(item["id"] for item in adjustments),
        *(preference_entity_id(kind) for kind in ("profile", "settings", "doctrine", "checkin_settings")),
    }
    context = {
        "task": task,
        "context_schema_version": 2,
        "result_schema_version": 2,
        "generation_policy": generation_policy,
        "doctrine": doctrine,
        "recent_days": days,
        "recent_records": records,
        "food_library_matches": matches,
        "long_term_memories": memories,
        "current_adjustments": adjustments,
        "evidence_links": evidence_links,
        "safety": personalization["safety"],
        "active_profile": personalization.get("profile"),
        "current_goals": personalization.get("goals") or [],
        "active_strategy": personalization.get("strategy"),
        "active_targets": personalization.get("targets") or [],
        "confirmed_rules": adaptations["confirmed_rules"],
        "inventory": inventory,
        "source_manifest": source_manifest,
        "source_revisions": _revision_references(source_ids),
        "result_schema": result_schema(task["type"], fact_only=generation_policy["fact_only"]),
        "analysis_boundary": "照片与数量只能区间估算；不可伪造不可见油、酱汁、重量或品牌。",
    }
    context["context_hash"] = hashlib.sha256(
        json.dumps(context, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return context


def result_schema(task_type: str, *, fact_only: bool = False) -> dict:
    nutrition = {"energy_kcal": [0, 0], "protein_g": [0, 0], "carbs_g": [0, 0], "fat_g": [0, 0]}
    if task_type == "photo":
        schema = {
            "summary": "string",
            "candidates": [{"name": "string", "portion_range": "string", "nutrition": nutrition, "confidence": 0.0}],
            "unknowns": ["string"],
        }
        if not fact_only:
            schema["advice"] = ["string"]
        return schema
    if fact_only:
        return {
            "summary": "string", "observed_items": ["string"], "batch_nutrition": nutrition,
            "per_serving_nutrition": nutrition, "gaps": ["string"], "risks": ["string"],
            "unknowns": ["string"],
        }
    return {
        "summary": "string", "combinations": ["string"], "batch_nutrition": nutrition,
        "per_serving_nutrition": nutrition, "gaps": ["string"], "risks": ["string"],
        "minimal_adjustments": ["string"],
    }
