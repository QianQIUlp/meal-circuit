from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import sqlite3
import uuid
from typing import Any

from .domain import DomainRevision, make_revision, new_id, utc_now
from .contracts import validate_transition
from .storage import (
    app_home,
    managed_asset_root,
    private_doctrine_path,
    profile_path,
    resolve_data_path,
    settings_path,
)
from .validation import ValidationError


UUID_NAMESPACE = uuid.UUID("2a7c0c93-763f-4d3c-93f1-c8a5768da92a")


def preference_entity_id(kind: str) -> str:
    return f"preferences_{uuid.uuid5(UUID_NAMESPACE, kind)}"


def task_input_entity_id(task_id: str) -> str:
    return f"task_input_{uuid.uuid5(UUID_NAMESPACE, task_id)}"


def active_result_entity_id(source_entity_id: str) -> str:
    return f"result_{uuid.uuid5(UUID_NAMESPACE, f'active-result:{source_entity_id}')}"


def decode_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key, value in list(item.items()):
        if value is None or not isinstance(value, str):
            continue
        if key.endswith("_json") or key in {"before_json", "after_json"}:
            try:
                item[key] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"数据库字段 {key} 不是合法 JSON") from exc
    return item


def _one(connection: sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Row:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        raise KeyError(params[0])
    return row


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset_reference(connection: sqlite3.Connection, reference: str) -> dict:
    path = resolve_data_path(reference)
    if not path.is_file():
        return {"external_reference": reference, "unresolved": True}
    digest = _file_sha256(path)
    extension = path.suffix.lower() or ".bin"
    asset_id = f"asset_{digest}"
    try:
        relative = path.resolve().relative_to(app_home().resolve()).as_posix()
    except ValueError:
        destination = managed_asset_root() / f"{digest}{extension}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(path, temporary)
            if _file_sha256(temporary) != digest:
                temporary.unlink(missing_ok=True)
                raise ValidationError("受管资产复制校验失败")
            temporary.replace(destination)
        relative = destination.relative_to(app_home()).as_posix()
    connection.execute(
        """INSERT OR IGNORE INTO managed_assets(
               id,sha256,media_type,extension,byte_count,relative_path,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            asset_id,
            digest,
            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            extension,
            path.stat().st_size,
            relative,
            utc_now(),
        ),
    )
    return {"asset_id": asset_id}


def _asset_ids(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("asset_id") and isinstance(child, str):
                yield child
            yield from _asset_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _asset_ids(child)


def unresolved_asset_references(connection: sqlite3.Connection) -> list[str]:
    references = {
        str(row[0] or row[1])
        for row in connection.execute(
            """SELECT external_reference,id FROM managed_assets
               WHERE unresolved=1 OR relative_path IS NULL"""
        )
    }

    def walk(value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("external_reference")
            if value.get("unresolved") is True and isinstance(reference, str):
                references.add(reference)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for row in connection.execute(
        """SELECT r.payload_json FROM entity_heads h
           JOIN domain_revisions r ON r.revision_id=h.revision_id"""
    ):
        try:
            walk(json.loads(row[0]))
        except json.JSONDecodeError:
            continue
    return sorted(references)


def _validate_state_change(entity_kind: str, before: dict, after: dict) -> None:
    if entity_kind == "task":
        validate_transition("task", before["task"]["status"], after["task"]["status"])
    elif entity_kind == "daily_review":
        validate_transition(
            "daily_review", before["review"]["status"], after["review"]["status"]
        )
    elif entity_kind == "checkin_day":
        previous = {
            item["module"]["module_key"]: item["module"]["status"]
            for item in before.get("modules", [])
        }
        for item in after.get("modules", []):
            module = item["module"]
            validate_transition(
                "checkin_module",
                previous.get(module["module_key"], "not_started"),
                module["status"],
            )


def snapshot_payload(connection: sqlite3.Connection, entity_kind: str, entity_id: str) -> tuple[dict, bool]:
    if entity_kind == "task":
        row = _one(connection, "SELECT * FROM tasks WHERE id=?", (entity_id,))
        task = decode_row(row)
        for field in ("original_input", "input_version", "image_path"):
            task.pop(field, None)
        return {"task": task}, False
    if entity_kind == "food_item":
        row = _one(connection, "SELECT * FROM food_items WHERE id=?", (entity_id,))
        food = decode_row(row)
        reference = food.pop("package_photo_path", None)
        if reference:
            asset = _asset_reference(connection, reference)
            food.update({f"package_photo_{key}": value for key, value in asset.items()})
        return {
            "food": food,
            "history": [
                decode_row(item)
                for item in connection.execute(
                    "SELECT * FROM food_item_history WHERE food_id=? ORDER BY created_at,id", (entity_id,)
                )
            ],
        }, bool(row["deleted_at"])
    if entity_kind == "daily_record":
        row = _one(connection, "SELECT * FROM daily_records WHERE id=?", (entity_id,))
        return decode_row(row), False
    if entity_kind == "checkin_day":
        row = _one(connection, "SELECT * FROM daily_checkins WHERE id=?", (entity_id,))
        modules = []
        for module in connection.execute(
            "SELECT * FROM daily_checkin_modules WHERE checkin_id=? ORDER BY module_key", (entity_id,)
        ):
            modules.append(
                {
                    "module": decode_row(module),
                    "history": [
                        decode_row(item)
                        for item in connection.execute(
                            "SELECT * FROM daily_checkin_module_history WHERE module_id=? ORDER BY version,id",
                            (module["id"],),
                        )
                    ],
                }
            )
        return {"checkin": decode_row(row), "modules": modules}, False
    if entity_kind == "daily_review":
        row = _one(connection, "SELECT * FROM daily_reviews WHERE id=?", (entity_id,))
        return {
            "review": decode_row(row),
            "history": [
                decode_row(item)
                for item in connection.execute(
                    "SELECT * FROM daily_review_history WHERE review_id=? ORDER BY version,id", (entity_id,)
                )
            ],
        }, False
    if entity_kind == "memory":
        row = _one(connection, "SELECT * FROM memories WHERE id=?", (entity_id,))
        return decode_row(row), not bool(row["active"])
    if entity_kind == "adjustment":
        row = _one(connection, "SELECT * FROM adjustments WHERE id=?", (entity_id,))
        return decode_row(row), not bool(row["active"])
    if entity_kind == "asset":
        row = _one(connection, "SELECT * FROM managed_assets WHERE id=?", (entity_id,))
        item = decode_row(row)
        item.pop("relative_path", None)
        item.pop("external_reference", None)
        item.pop("unresolved", None)
        return item, False
    if entity_kind == "preferences":
        expected = {
            preference_entity_id("profile"): ("profile", profile_path()),
            preference_entity_id("settings"): ("settings", settings_path()),
            preference_entity_id("doctrine"): ("doctrine", private_doctrine_path()),
        }
        if entity_id == preference_entity_id("checkin_settings"):
            rows = [
                decode_row(row)
                for row in connection.execute(
                    "SELECT * FROM checkin_module_settings ORDER BY sort_order,module_key"
                )
            ]
            return {
                "kind": "checkin_settings",
                "content": json.dumps(rows, ensure_ascii=False, sort_keys=True),
            }, False
        if entity_id == preference_entity_id("agent_user_model"):
            row = connection.execute(
                "SELECT content FROM config_documents WHERE kind='agent_user_model'"
            ).fetchone()
            return {
                "kind": "agent_user_model",
                "content": str(row["content"] if row else '{"schema_version":1,"claims":[]}'),
            }, False
        if entity_id not in expected:
            raise KeyError(entity_id)
        kind, path = expected[entity_id]
        return {"kind": kind, "content": path.read_text(encoding="utf-8") if path.is_file() else ""}, False
    raise ValidationError(f"不支持捕获的领域实体：{entity_kind}")


def _metadata(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute("SELECT value FROM app_metadata WHERE key=?", (key,)).fetchone()
    if row is None:
        raise ValidationError(f"数据库缺少元数据：{key}")
    return str(row[0])


def capture_entity(
    connection: sqlite3.Connection,
    entity_kind: str,
    entity_id: str,
    *,
    created_at: str | None = None,
) -> DomainRevision | None:
    payload, deleted = snapshot_payload(connection, entity_kind, entity_id)
    if entity_kind != "asset":
        for asset_id in sorted(set(_asset_ids(payload))):
            capture_entity(connection, "asset", asset_id, created_at=created_at)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    head = connection.execute(
        """SELECT h.revision_id,r.payload_json,r.deleted FROM entity_heads h
           JOIN domain_revisions r ON r.revision_id=h.revision_id WHERE h.entity_id=?""",
        (entity_id,),
    ).fetchone()
    if head:
        _validate_state_change(entity_kind, json.loads(head["payload_json"]), payload)
    if head and head["payload_json"] == canonical and bool(head["deleted"]) == deleted:
        return None
    parents = [head["revision_id"]] if head else []
    revision = make_revision(
        entity_kind,
        payload,
        entity_id=entity_id,
        parent_revision_ids=parents,
        author_device_id=_metadata(connection, "device_id"),
        deleted=deleted,
        created_at=created_at or utc_now(),
    )
    connection.execute(
        """INSERT INTO domain_revisions(
               revision_id,entity_id,entity_kind,parent_revision_ids_json,payload_json,
               schema_version,author_device_id,deleted,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            revision.revision_id,
            revision.entity_id,
            revision.entity_kind,
            json.dumps(list(revision.parent_revision_ids), ensure_ascii=False),
            canonical,
            revision.schema_version,
            revision.author_device_id,
            int(revision.deleted),
            revision.created_at,
        ),
    )
    connection.execute(
        """INSERT INTO entity_heads(entity_id,entity_kind,revision_id,conflicted,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET
           entity_kind=excluded.entity_kind,revision_id=excluded.revision_id,
           conflicted=0,updated_at=excluded.updated_at""",
        (entity_id, entity_kind, revision.revision_id, 0, revision.created_at),
    )
    if entity_kind == "preferences":
        content = str(payload.get("content", ""))
        connection.execute(
            """INSERT INTO config_documents(kind,content,content_sha256,revision_id,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET
               content=excluded.content,content_sha256=excluded.content_sha256,
               revision_id=excluded.revision_id,updated_at=excluded.updated_at""",
            (
                payload["kind"],
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                revision.revision_id,
                revision.created_at,
            ),
        )
    enqueue_revision(connection, revision)
    return revision


def enqueue_revision(connection: sqlite3.Connection, revision: DomainRevision) -> bool:
    sync = connection.execute(
        "SELECT enabled,key_version FROM sync_configuration WHERE singleton=1"
    ).fetchone()
    if not sync or not sync["enabled"]:
        return False
    shadow = connection.execute(
        "SELECT server_version FROM sync_shadow WHERE entity_id=?", (revision.entity_id,)
    ).fetchone()
    base_server_version = int(shadow["server_version"]) if shadow else 0
    connection.execute(
        "DELETE FROM sync_outbox WHERE entity_id=? AND state='pending'", (revision.entity_id,)
    )
    connection.execute(
        """INSERT INTO sync_outbox(
               op_id,opaque_remote_id,entity_id,revision_id,base_server_version,
               encrypted_envelope,key_version,state,created_at,updated_at
           ) VALUES(?,?,?,?,?,NULL,?,'pending',?,?)""",
        (
            new_id("op"),
            f"pending:{revision.entity_id}",
            revision.entity_id,
            revision.revision_id,
            base_server_version,
            sync["key_version"],
            revision.created_at,
            revision.created_at,
        ),
    )
    return True


def _persist_projection(connection: sqlite3.Connection, revision: DomainRevision) -> None:
    canonical = json.dumps(revision.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT INTO domain_revisions(
               revision_id,entity_id,entity_kind,parent_revision_ids_json,payload_json,
               schema_version,author_device_id,deleted,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            revision.revision_id,
            revision.entity_id,
            revision.entity_kind,
            json.dumps(list(revision.parent_revision_ids), ensure_ascii=False),
            canonical,
            revision.schema_version,
            revision.author_device_id,
            int(revision.deleted),
            revision.created_at,
        ),
    )
    connection.execute(
        """INSERT INTO entity_heads(entity_id,entity_kind,revision_id,conflicted,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET
           entity_kind=excluded.entity_kind,revision_id=excluded.revision_id,
           conflicted=0,updated_at=excluded.updated_at""",
        (
            revision.entity_id,
            revision.entity_kind,
            revision.revision_id,
            0,
            revision.created_at,
        ),
    )
    enqueue_revision(connection, revision)


def capture_task_input(connection: sqlite3.Connection, task_id: str) -> DomainRevision | None:
    task = _one(connection, "SELECT * FROM tasks WHERE id=?", (task_id,))
    reference = task["image_path"]
    asset = _asset_reference(connection, reference) if reference else {}
    payload = {
        "task_id": task_id,
        "task_type": task["type"],
        "input_version": task["input_version"],
        "original_input": task["original_input"],
        "input_history": [
            decode_row(item)
            for item in connection.execute(
                "SELECT * FROM task_input_history WHERE task_id=? ORDER BY version", (task_id,)
            )
        ],
        **asset,
    }
    entity_id = task_input_entity_id(task_id)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    head = connection.execute(
        """SELECT h.revision_id,r.payload_json FROM entity_heads h
           JOIN domain_revisions r ON r.revision_id=h.revision_id WHERE h.entity_id=?""",
        (entity_id,),
    ).fetchone()
    if head and head["payload_json"] == canonical:
        return None
    revision = make_revision(
        "task_input",
        payload,
        entity_id=entity_id,
        parent_revision_ids=[head["revision_id"]] if head else [],
        author_device_id=_metadata(connection, "device_id"),
    )
    for asset_id in _asset_ids(payload):
        capture_entity(connection, "asset", asset_id)
    _persist_projection(connection, revision)
    return revision


def capture_derived_result(
    connection: sqlite3.Connection,
    *,
    source_entity_id: str,
    source_kind: str,
    result_version: int,
    result: dict,
    provenance: dict,
    entity_id: str | None = None,
) -> DomainRevision:
    parent_revision_ids: list[str] = []
    if entity_id:
        head = connection.execute(
            "SELECT revision_id FROM entity_heads WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if head:
            parent_revision_ids.append(head["revision_id"])
    revision = make_revision(
        "analysis_result",
        {
            "source_entity_id": source_entity_id,
            "source_kind": source_kind,
            "result_version": result_version,
            "result": result,
            "provenance": provenance,
        },
        entity_id=entity_id or new_id("result"),
        parent_revision_ids=parent_revision_ids,
        author_device_id=_metadata(connection, "device_id"),
    )
    _persist_projection(connection, revision)
    return revision


def tombstone_derived_results(
    connection: sqlite3.Connection,
    *,
    source_entity_id: str,
    keep_entity_id: str | None = None,
) -> list[str]:
    """Tombstone obsolete result heads so sync cannot resurrect generated mistakes."""
    tombstoned = []
    rows = connection.execute(
        """SELECT h.entity_id,h.revision_id,r.payload_json,r.deleted
           FROM entity_heads h JOIN domain_revisions r ON r.revision_id=h.revision_id
           WHERE h.entity_kind='analysis_result'"""
    ).fetchall()
    for row in rows:
        if row["entity_id"] == keep_entity_id or row["deleted"]:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        if payload.get("source_entity_id") != source_entity_id:
            continue
        revision = make_revision(
            "analysis_result",
            payload,
            entity_id=row["entity_id"],
            parent_revision_ids=[row["revision_id"]],
            author_device_id=_metadata(connection, "device_id"),
            deleted=True,
        )
        _persist_projection(connection, revision)
        tombstoned.append(row["entity_id"])
    return tombstoned


def capture_correction(connection: sqlite3.Connection, correction_id: str) -> DomainRevision:
    row = _one(
        connection,
        "SELECT * FROM task_corrections WHERE id=?",
        (correction_id,),
    )
    revision = make_revision(
        "correction",
        decode_row(row),
        entity_id=correction_id,
        author_device_id=_metadata(connection, "device_id"),
        created_at=row["created_at"],
    )
    _persist_projection(connection, revision)
    return revision


def seed_current_entities(connection: sqlite3.Connection) -> int:
    entities: list[tuple[str, str]] = []
    for table, kind in (
        ("tasks", "task"),
        ("food_items", "food_item"),
        ("daily_records", "daily_record"),
        ("daily_checkins", "checkin_day"),
        ("daily_reviews", "daily_review"),
        ("memories", "memory"),
        ("adjustments", "adjustment"),
    ):
        entities.extend((kind, row[0]) for row in connection.execute(f"SELECT id FROM {table}"))
    count = 0
    for kind, entity_id in entities:
        if capture_entity(connection, kind, entity_id) is not None:
            count += 1
    for row in connection.execute("SELECT id FROM tasks"):
        if capture_task_input(connection, row["id"]) is not None:
            count += 1
    for row in connection.execute("SELECT id FROM task_corrections"):
        entity_id = row["id"]
        if connection.execute(
            "SELECT 1 FROM entity_heads WHERE entity_id=?", (entity_id,)
        ).fetchone() is None:
            capture_correction(connection, entity_id)
            count += 1
    count += refresh_configuration_entities(connection)
    return count


def refresh_configuration_entities(connection: sqlite3.Connection) -> int:
    count = 0
    for kind in ("profile", "settings", "doctrine", "checkin_settings", "agent_user_model"):
        if capture_entity(connection, "preferences", preference_entity_id(kind)) is not None:
            count += 1
    return count


def enqueue_all_heads(connection: sqlite3.Connection) -> int:
    """Queue the current local state when synchronization is enabled for the first time."""
    config = connection.execute(
        "SELECT enabled,key_version FROM sync_configuration WHERE singleton=1"
    ).fetchone()
    if not config or not config["enabled"]:
        return 0
    count = 0
    for row in connection.execute(
        """SELECT h.entity_id,h.revision_id,COALESCE(s.server_version,0) AS base_server_version
           FROM entity_heads h LEFT JOIN sync_shadow s ON s.entity_id=h.entity_id
           WHERE NOT EXISTS (
               SELECT 1 FROM sync_outbox o
               WHERE o.entity_id=h.entity_id AND o.revision_id=h.revision_id
           ) ORDER BY h.entity_kind,h.entity_id"""
    ).fetchall():
        timestamp = utc_now()
        connection.execute(
            """INSERT INTO sync_outbox(
                   op_id,opaque_remote_id,entity_id,revision_id,base_server_version,
                   encrypted_envelope,key_version,state,created_at,updated_at
               ) VALUES(?,?,?,?,?,NULL,?,'pending',?,?)""",
            (
                new_id("op"),
                f"pending:{row['entity_id']}",
                row["entity_id"],
                row["revision_id"],
                row["base_server_version"],
                config["key_version"],
                timestamp,
                timestamp,
            ),
        )
        count += 1
    return count


def _delete_materialized(connection: sqlite3.Connection, revision: DomainRevision) -> None:
    entity_id = revision.entity_id
    if revision.entity_kind == "task":
        return
    elif revision.entity_kind == "food_item":
        connection.execute("DELETE FROM food_item_history WHERE food_id=?", (entity_id,))
        connection.execute("DELETE FROM food_items WHERE id=?", (entity_id,))
    elif revision.entity_kind == "daily_record":
        connection.execute("DELETE FROM daily_records WHERE id=?", (entity_id,))
    elif revision.entity_kind == "checkin_day":
        module_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM daily_checkin_modules WHERE checkin_id=?", (entity_id,)
            )
        ]
        for module_id in module_ids:
            connection.execute(
                "DELETE FROM daily_checkin_module_history WHERE module_id=?", (module_id,)
            )
        connection.execute("DELETE FROM daily_checkin_modules WHERE checkin_id=?", (entity_id,))
        connection.execute("DELETE FROM daily_checkins WHERE id=?", (entity_id,))
    elif revision.entity_kind == "daily_review":
        connection.execute("DELETE FROM daily_review_history WHERE review_id=?", (entity_id,))
        connection.execute("DELETE FROM daily_reviews WHERE id=?", (entity_id,))
    elif revision.entity_kind == "memory":
        connection.execute("DELETE FROM memories WHERE id=?", (entity_id,))
    elif revision.entity_kind == "adjustment":
        connection.execute("DELETE FROM adjustments WHERE id=?", (entity_id,))


def materialize_revision(connection: sqlite3.Connection, revision: DomainRevision) -> None:
    """Replace one aggregate from a validated remote revision inside the caller transaction."""
    if revision.entity_kind == "asset":
        existing = connection.execute(
            "SELECT relative_path FROM managed_assets WHERE id=?", (revision.entity_id,)
        ).fetchone()
        relative_path = existing["relative_path"] if existing else None
        connection.execute(
            """INSERT INTO managed_assets(
                   id,sha256,media_type,extension,byte_count,relative_path,unresolved,created_at
               ) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               sha256=excluded.sha256,media_type=excluded.media_type,extension=excluded.extension,
               byte_count=excluded.byte_count,relative_path=COALESCE(managed_assets.relative_path,excluded.relative_path),
               unresolved=CASE WHEN managed_assets.relative_path IS NULL THEN 1 ELSE 0 END""",
            (
                revision.entity_id,
                revision.payload["sha256"],
                revision.payload["media_type"],
                revision.payload["extension"],
                revision.payload["byte_count"],
                relative_path,
                int(relative_path is None),
                revision.created_at,
            ),
        )
        from .portable import _apply_revision

        _apply_revision(connection, revision, {revision.entity_id: relative_path})
        if relative_path is None:
            connection.execute(
                "UPDATE managed_assets SET unresolved=1 WHERE id=?", (revision.entity_id,)
            )
        return
    _delete_materialized(connection, revision)
    asset_paths = {
        row["id"]: row["relative_path"]
        for row in connection.execute("SELECT id,relative_path FROM managed_assets")
    }
    from .portable import _apply_revision

    _apply_revision(connection, revision, asset_paths)
