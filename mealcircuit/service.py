from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

from . import checkins
from .configuration import load_doctrine, load_settings
from .db import connect, init_db, row_dict
from .storage import resolve_data_path, store_data_path, upload_root
from .validation import ValidationError, validate_daily_review_result, validate_result

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
IMAGE_SIGNATURES = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


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
        task["corrections"] = [
            row_dict(row)
            for row in conn.execute(
                "SELECT * FROM task_corrections WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        ]
        return task


def complete_task(task_id: str, result: dict) -> dict:
    task = get_task(task_id)
    if task["status"] == "completed":
        raise ValidationError("任务已完成；不得覆盖原结果，请新增用户校正")
    validate_result(task["type"], result)
    with connect() as conn:
        updated = conn.execute(
            """UPDATE tasks SET status='completed', result_json=?, result_version=1,
               completed_at=? WHERE id=? AND status='pending'""",
            (json.dumps(result, ensure_ascii=False), now(), task_id),
        )
        if updated.rowcount != 1:
            raise ValidationError("任务状态已变化，请重新读取")
    return get_task(task_id)


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
    return get_food(food_id)


def delete_food(food_id: str) -> None:
    before = get_food(food_id)
    timestamp = now()
    with connect() as conn:
        conn.execute("UPDATE food_items SET deleted_at=?,updated_at=? WHERE id=?", (timestamp, timestamp, food_id))
        after = dict(conn.execute("SELECT * FROM food_items WHERE id=?", (food_id,)).fetchone())
        _food_history(conn, food_id, "delete", before, after)


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
        return
    if review["status"] == "completed" and review["result_json"]:
        conn.execute(
            """INSERT INTO daily_review_history(
                id,review_id,version,source_record_ids_json,source_checkin_versions_json,
                result_json,completed_at,archived_at,archive_reason
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                new_id("review_history"), review["id"], review["result_version"],
                review["source_record_ids_json"], review["source_checkin_versions_json"],
                review["result_json"], review["completed_at"], timestamp, archive_reason,
            ),
        )
    conn.execute(
        """UPDATE daily_reviews SET status='pending',source_record_ids_json=?,source_checkin_versions_json=?,result_json=NULL,
           updated_at=?,completed_at=NULL WHERE review_date=?""",
        (source_json, checkin_versions_json, timestamp, record_date),
    )


def _validate_checkin_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    if parsed > date.today():
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
        _queue_daily_review(conn, record_date)
    return {"id": item[0], "record_date": item[1], "raw_input": item[2], "structured_json": structured, "created_at": item[4]}


def ensure_daily_review(review_date: str) -> dict:
    try:
        date.fromisoformat(review_date)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD") from exc
    init_db()
    with connect() as conn:
        if not _record_ids_for_date(conn, review_date) and not _checkin_versions_for_date(conn, review_date):
            raise ValidationError("该日期没有每日记录或已发布状态")
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
                result_json,completed_at,archived_at,archive_reason
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                new_id("review_history"), current["id"], current["result_version"],
                current["source_record_ids_json"], current["source_checkin_versions_json"],
                current["result_json"], current["completed_at"], timestamp, reason,
            ),
        )
        conn.execute(
            """UPDATE daily_reviews SET status='pending',result_json=NULL,updated_at=?,completed_at=NULL
               WHERE review_date=?""", (timestamp, review_date)
        )
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
    settings = settings or load_settings()
    return {
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


def daily_review_context(review_date: str, days: int = 14) -> dict:
    review = ensure_daily_review(review_date)
    end = date.fromisoformat(review_date)
    cutoff = (end - timedelta(days=days - 1)).isoformat()
    doctrine = load_doctrine()
    settings = load_settings()
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
            """SELECT c.checkin_date,m.module_key,m.status,m.answers_json,m.version,m.completed_at
               FROM daily_checkins c JOIN daily_checkin_modules m ON m.checkin_id=c.id
               WHERE c.checkin_date BETWEEN ? AND ? AND m.version>0
               ORDER BY c.checkin_date,m.module_key""",
            (cutoff, review_date),
        ).fetchall()
    recent_checkins = []
    for row in checkin_rows:
        item = row_dict(row)
        answers = item.get("answers_json") or {}
        item["summary"] = checkins.summarize(item["module_key"], answers, item["status"])
        recent_checkins.append(item)
    target_state = get_checkin_state(review_date)
    target_modules = [
        {
            "module_key": item["module_key"], "label": item["label"], "status": item["status"],
            "version": item["version"], "answers": item.get("answers_json") or None,
            "summary": item["summary"] or None,
        }
        for item in target_state["modules"] if item["version"] > 0
    ]
    return {
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
        "priority_foods": list_priority_foods(),
        "settings": settings,
        "result_schema": daily_review_schema(settings),
    }


def complete_daily_review(review_date: str, result: dict) -> dict:
    review = ensure_daily_review(review_date)
    if review["status"] == "completed":
        raise ValidationError("该日期复盘已完成；新增每日记录后才可重新复盘")
    validate_daily_review_result(result, load_settings())
    expected_priority_ids = {food["id"] for food in list_priority_foods()}
    submitted_priority_ids = {item["food_id"] for item in result["priority_food_decisions"]}
    if submitted_priority_ids != expected_priority_ids:
        missing = sorted(expected_priority_ids - submitted_priority_ids)
        unknown = sorted(submitted_priority_ids - expected_priority_ids)
        raise ValidationError(f"优先食品裁决不完整；缺少={missing}，未知={unknown}")
    expected_menu_date = (date.fromisoformat(review_date) + timedelta(days=1)).isoformat()
    if result["tomorrow_menu"]["date"] != expected_menu_date:
        raise ValidationError(f"次日菜单日期必须是 {expected_menu_date}")
    timestamp = now()
    with connect() as conn:
        updated = conn.execute(
            """UPDATE daily_reviews SET status='completed',result_json=?,result_version=result_version+1,
               updated_at=?,completed_at=? WHERE review_date=? AND status='pending'""",
            (json.dumps(result, ensure_ascii=False), timestamp, timestamp, review_date),
        )
        if updated.rowcount != 1:
            raise ValidationError("复盘状态已变化，请重新读取")
    return get_daily_review(review_date)


def pending_work() -> dict:
    return {
        "tasks": list_tasks("pending"),
        "daily_reviews": list_daily_reviews("pending"),
    }


def daily_state(review_date: str | None = None) -> dict:
    target = review_date or date.today().isoformat()
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
    return {"date": target, "status": review["status"], "record_count": record_count, "review": review}


def add_memory(kind: str, content: str, evidence: str = "") -> dict:
    if kind not in {"preference", "gut_trigger", "constraint", "other"}:
        raise ValidationError("长期记忆类型无效")
    if not content.strip():
        raise ValidationError("长期记忆内容不能为空")
    item = (new_id("memory"), kind, content.strip(), evidence.strip(), 1, now(), now())
    with connect() as conn:
        conn.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?)", item)
    return {"id": item[0], "kind": kind, "content": item[2], "evidence": item[3]}


def add_adjustment(content: str, reason: str = "") -> dict:
    if not content.strip():
        raise ValidationError("当前调整不能为空")
    item = (new_id("adjustment"), content.strip(), reason.strip(), 1, now(), now())
    with connect() as conn:
        conn.execute("INSERT INTO adjustments VALUES(?,?,?,?,?,?)", item)
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
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    doctrine = load_doctrine()
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
    return {
        "task": task,
        "doctrine": doctrine,
        "recent_days": days,
        "recent_records": records,
        "food_library_matches": matches,
        "long_term_memories": memories,
        "current_adjustments": adjustments,
        "result_schema": result_schema(task["type"]),
        "analysis_boundary": "照片与数量只能区间估算；不可伪造不可见油、酱汁、重量或品牌。",
    }


def result_schema(task_type: str) -> dict:
    nutrition = {"energy_kcal": [0, 0], "protein_g": [0, 0], "carbs_g": [0, 0], "fat_g": [0, 0]}
    if task_type == "photo":
        return {
            "summary": "string",
            "candidates": [{"name": "string", "portion_range": "string", "nutrition": nutrition, "confidence": 0.0}],
            "unknowns": ["string"],
            "advice": ["string"],
        }
    return {
        "summary": "string", "combinations": ["string"], "batch_nutrition": nutrition,
        "per_serving_nutrition": nutrition, "gaps": ["string"], "risks": ["string"],
        "minimal_adjustments": ["string"],
    }
