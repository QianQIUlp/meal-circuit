from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


CURRENT_SCHEMA_VERSION = 7


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM app_metadata WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def detected_schema_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_metadata'"
    ).fetchone()
    if not exists:
        return 1
    raw = _metadata(connection, "database_schema_version")
    if raw is None:
        return 1
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError("database_schema_version is invalid") from exc


def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO app_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _backup(connection: sqlite3.Connection, database_path: Path, from_version: int) -> Path:
    backup_root = database_path.parent / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    target = backup_root / f"schema-v{from_version}-{_timestamp()}.db"
    suffix = 1
    while target.exists():
        target = backup_root / f"schema-v{from_version}-{_timestamp()}-{suffix}.db"
        suffix += 1
    destination = sqlite3.connect(target)
    try:
        connection.backup(destination)
    finally:
        destination.close()
    return target


def create_migration_backup(
    connection: sqlite3.Connection, database_path: Path, from_version: int
) -> Path:
    return _backup(connection, database_path, from_version)


def _restore(connection: sqlite3.Connection, backup_path: Path) -> None:
    connection.rollback()
    source = sqlite3.connect(backup_path)
    try:
        source.backup(connection)
        connection.commit()
    finally:
        source.close()


def restore_migration_backup(connection: sqlite3.Connection, backup_path: Path) -> None:
    _restore(connection, backup_path)


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute our migration DDL without sqlite3.executescript's implicit COMMIT."""
    for statement in script.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)


def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    if task_columns and "input_version" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN input_version INTEGER NOT NULL DEFAULT 1")
    food_columns = {row[1] for row in connection.execute("PRAGMA table_info(food_items)")}
    for name, definition in {
        "fiber_g": "REAL",
        "sodium_mg": "REAL",
        "category": "TEXT NOT NULL DEFAULT 'other'",
        "menu_priority": "TEXT NOT NULL DEFAULT 'normal'",
        "default_portion": "TEXT NOT NULL DEFAULT ''",
        "usage_rule": "TEXT NOT NULL DEFAULT ''",
        "source_key": "TEXT",
    }.items():
        if food_columns and name not in food_columns:
            connection.execute(f"ALTER TABLE food_items ADD COLUMN {name} {definition}")
    for table in ("daily_reviews", "daily_review_history"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns and "source_checkin_versions_json" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN source_checkin_versions_json TEXT NOT NULL DEFAULT '{{}}'"
            )
    history_columns = {row[1] for row in connection.execute("PRAGMA table_info(daily_review_history)")}
    if history_columns and "archive_reason" not in history_columns:
        connection.execute(
            "ALTER TABLE daily_review_history ADD COLUMN archive_reason TEXT NOT NULL DEFAULT ''"
        )
    if food_columns:
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_food_source_key ON food_items(source_key) WHERE source_key IS NOT NULL"
        )
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS domain_revisions (
            revision_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            entity_kind TEXT NOT NULL,
            parent_revision_ids_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            author_device_id TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0,1)),
            created_at TEXT NOT NULL,
            UNIQUE(entity_id,revision_id)
        );
        CREATE INDEX IF NOT EXISTS idx_domain_entity ON domain_revisions(entity_kind,entity_id,created_at);

        CREATE TABLE IF NOT EXISTS entity_heads (
            entity_id TEXT PRIMARY KEY,
            entity_kind TEXT NOT NULL,
            revision_id TEXT NOT NULL REFERENCES domain_revisions(revision_id),
            conflicted INTEGER NOT NULL DEFAULT 0 CHECK (conflicted IN (0,1)),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS managed_assets (
            id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            media_type TEXT NOT NULL,
            extension TEXT NOT NULL,
            byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
            relative_path TEXT,
            external_reference TEXT,
            unresolved INTEGER NOT NULL DEFAULT 0 CHECK (unresolved IN (0,1)),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS config_documents (
            kind TEXT PRIMARY KEY CHECK (kind IN ('profile','settings','doctrine','checkin_settings','agent_user_model')),
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            revision_id TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_outbox (
            local_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            op_id TEXT NOT NULL UNIQUE,
            opaque_remote_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            base_server_version INTEGER NOT NULL DEFAULT 0,
            encrypted_envelope TEXT,
            key_version INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','sending','conflict')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sync_outbox_state ON sync_outbox(state,local_sequence);

        CREATE TABLE IF NOT EXISTS sync_shadow (
            opaque_remote_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL UNIQUE,
            server_version INTEGER NOT NULL,
            revision_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_cursor (
            scope TEXT PRIMARY KEY,
            cursor_value INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_conflicts (
            id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            entity_kind TEXT NOT NULL,
            base_revision_json TEXT,
            local_revision_json TEXT NOT NULL,
            remote_revision_json TEXT NOT NULL,
            conflicting_paths_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unresolved' CHECK (status IN ('unresolved','resolved')),
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sync_conflict_status ON sync_conflicts(status,created_at);
        """
    )


def _migrate_2_to_3(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS sync_configuration (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
            server_url TEXT,
            account_id TEXT,
            device_name TEXT NOT NULL DEFAULT '',
            key_version INTEGER NOT NULL DEFAULT 1,
            media_policy TEXT NOT NULL DEFAULT 'all_wifi' CHECK (media_policy IN ('all','all_wifi','on_demand')),
            updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO sync_configuration(singleton,enabled,device_name,key_version,media_policy,updated_at)
        VALUES(1,0,'',1,'all_wifi','1970-01-01T00:00:00Z');
        """
    )


def _migrate_3_to_4(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(sync_configuration)")}
    if "remote_device_id" not in columns:
        connection.execute("ALTER TABLE sync_configuration ADD COLUMN remote_device_id TEXT")
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS sync_unknown_entities (
            opaque_remote_id TEXT PRIMARY KEY,
            server_version INTEGER NOT NULL,
            key_version INTEGER NOT NULL,
            encrypted_envelope TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def _migrate_4_to_5(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS sync_asset_state (
            asset_id TEXT PRIMARY KEY,
            blob_id TEXT NOT NULL UNIQUE,
            uploaded INTEGER NOT NULL DEFAULT 0 CHECK (uploaded IN (0,1)),
            downloaded INTEGER NOT NULL DEFAULT 0 CHECK (downloaded IN (0,1)),
            updated_at TEXT NOT NULL
        );
        """
    )


def _migrate_5_to_6(connection: sqlite3.Connection) -> None:
    for table in ("tasks", "daily_reviews", "daily_review_history"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "result_provenance_json" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN result_provenance_json TEXT")


def _migrate_6_to_7(connection: sqlite3.Connection) -> None:
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='config_documents'"
    ).fetchone()
    if table_sql and "agent_user_model" in str(table_sql[0]):
        return
    connection.execute("ALTER TABLE config_documents RENAME TO config_documents_v6")
    connection.execute(
        """CREATE TABLE config_documents (
               kind TEXT PRIMARY KEY CHECK (kind IN (
                   'profile','settings','doctrine','checkin_settings','agent_user_model'
               )),
               content TEXT NOT NULL,
               content_sha256 TEXT NOT NULL,
               revision_id TEXT,
               updated_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """INSERT INTO config_documents(kind,content,content_sha256,revision_id,updated_at)
           SELECT kind,content,content_sha256,revision_id,updated_at FROM config_documents_v6"""
    )
    connection.execute("DROP TABLE config_documents_v6")


MIGRATIONS = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
    6: _migrate_6_to_7,
}


def migrate(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    existed_before: bool,
    initial_backup: tuple[int, Path] | None = None,
) -> dict:
    connection.execute("CREATE TABLE IF NOT EXISTS app_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    raw_version = _metadata(connection, "database_schema_version")
    if raw_version is None:
        _set_metadata(connection, "database_schema_version", "1")
        current = 1
    else:
        try:
            current = int(raw_version)
        except ValueError as exc:
            raise RuntimeError("database_schema_version is invalid") from exc
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {current} is newer than supported {CURRENT_SCHEMA_VERSION}"
        )
    connection.commit()
    backup_paths: list[Path] = []
    applied: list[str] = []
    initial_backup_used = False
    try:
        while current < CURRENT_SCHEMA_VERSION:
            migration = MIGRATIONS.get(current)
            if migration is None:
                raise RuntimeError(f"missing database migration from version {current}")
            if existed_before:
                connection.commit()
                if initial_backup and initial_backup[0] == current and not initial_backup_used:
                    backup_paths.append(initial_backup[1])
                    initial_backup_used = True
                else:
                    backup_paths.append(_backup(connection, database_path, current))
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration(connection)
                current += 1
                _set_metadata(connection, "database_schema_version", str(current))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            applied.append(f"{current - 1}->{current}")
    except Exception:
        if backup_paths:
            _restore(connection, backup_paths[0])
        raise
    if _metadata(connection, "instance_id") is None:
        _set_metadata(connection, "instance_id", f"instance_{uuid.uuid4()}")
    if _metadata(connection, "device_id") is None:
        _set_metadata(connection, "device_id", f"device_{uuid.uuid4()}")
    if _metadata(connection, "created_at") is None:
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        _set_metadata(connection, "created_at", created_at)
    connection.commit()
    return {
        "schema_version": current,
        "applied": applied,
        "backup_path": str(backup_paths[0]) if backup_paths else None,
        "migration_backups": [str(path) for path in backup_paths],
        "instance_id": _metadata(connection, "instance_id"),
    }
