from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from .storage import app_home, db_path
from .validation import ValidationError


LEGACY_DOCTRINE = "减脂增肌饮食系统总纲.md"
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_files(repo: Path) -> list[tuple[Path, Path]]:
    home = app_home()
    pairs: list[tuple[Path, Path]] = []
    doctrine = repo / LEGACY_DOCTRINE
    if doctrine.is_file():
        pairs.append((doctrine, home / "doctrine.private.md"))
    for source_root, target_root in (
        (repo / "data" / "uploads", home / "uploads"),
        (repo / "data" / "food-labels", home / "food-labels"),
        (repo / "tmp", home / "archive" / "tmp-imports"),
    ):
        if source_root.is_dir():
            for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
                pairs.append((source, target_root / source.relative_to(source_root)))
    return pairs


def migration_preview(source_repo: str | Path) -> dict:
    repo = Path(source_repo).expanduser().resolve()
    if not repo.is_dir():
        raise ValidationError(f"迁移源目录不存在：{repo}")
    database = repo / "data" / "dietos.db"
    files = _source_files(repo)
    conflicts = []
    for source, target in files:
        if target.exists() and (not target.is_file() or sha256(source) != sha256(target)):
            conflicts.append(str(target))
    target_db = db_path()
    return {
        "mode": "preview",
        "source_repo": str(repo),
        "target_home": str(app_home()),
        "database": {
            "source": str(database),
            "target": str(target_db),
            "exists": database.is_file(),
            "target_exists": target_db.is_file(),
        },
        "files": [{"source": str(source), "target": str(target), "bytes": source.stat().st_size} for source, target in files],
        "settings_target": str(app_home() / "settings.json"),
        "conflicts": sorted(set(conflicts)),
    }


def _integrity(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}


def _logical_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in sorted(_table_counts(connection)):
        digest.update(table.encode())
        cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        for row in cursor:
            digest.update(json.dumps(list(row), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def _normal_path(value: str | None, category: str) -> str | None:
    if not value:
        return value
    path = Path(value)
    parts = [part.lower() for part in path.parts]
    marker = category.lower()
    if marker in parts:
        index = parts.index(marker)
        return Path(category, *path.parts[index + 1 :]).as_posix()
    return Path(category, path.name).as_posix()


def _migrate_database(source: Path, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="mealcircuit-migrate-", suffix=".db", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary)
        try:
            source_integrity = _integrity(source_connection)
            if source_integrity != "ok":
                raise ValidationError(f"源数据库完整性检查失败：{source_integrity}")
            source_counts = _table_counts(source_connection)
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA foreign_keys = ON")
            destination_connection.execute("BEGIN")
            for row in destination_connection.execute("SELECT id,image_path FROM tasks WHERE image_path IS NOT NULL").fetchall():
                destination_connection.execute("UPDATE tasks SET image_path=? WHERE id=?", (_normal_path(row[1], "uploads"), row[0]))
            for row in destination_connection.execute("SELECT id,package_photo_path FROM food_items WHERE package_photo_path IS NOT NULL").fetchall():
                destination_connection.execute(
                    "UPDATE food_items SET package_photo_path=? WHERE id=?",
                    (_normal_path(row[1], "food-labels"), row[0]),
                )
            destination_connection.commit()
            target_integrity = _integrity(destination_connection)
            target_counts = _table_counts(destination_connection)
            target_digest = _logical_digest(destination_connection)
        finally:
            source_connection.close()
            destination_connection.close()
        if target_integrity != "ok" or source_counts != target_counts:
            raise ValidationError("迁移后数据库验证失败")
        if target.exists():
            existing = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
            try:
                if _integrity(existing) == "ok" and _logical_digest(existing) == target_digest:
                    temporary.unlink(missing_ok=True)
                    return {"status": "identical", "integrity": "ok", "table_counts": target_counts, "logical_sha256": target_digest}
            finally:
                existing.close()
            raise ValidationError(f"目标数据库已存在且内容不同：{target}")
        os.replace(temporary, target)
        return {"status": "copied", "integrity": "ok", "table_counts": target_counts, "logical_sha256": target_digest}
    finally:
        temporary.unlink(missing_ok=True)


def apply_migration(source_repo: str | Path) -> dict:
    preview = migration_preview(source_repo)
    if preview["conflicts"]:
        raise ValidationError(f"目标存在内容冲突：{preview['conflicts']}")
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    copied = []
    skipped = []
    manifest_files = []
    for source, target in _source_files(Path(preview["source_repo"])):
        target.parent.mkdir(parents=True, exist_ok=True)
        source_hash = sha256(source)
        if target.exists():
            skipped.append(str(target))
        else:
            shutil.copy2(source, target)
            if sha256(target) != source_hash:
                target.unlink(missing_ok=True)
                raise ValidationError(f"文件复制校验失败：{source}")
            copied.append(str(target))
        manifest_files.append({"source": str(source), "target": str(target), "bytes": source.stat().st_size, "sha256": source_hash})
    database_result = None
    source_database = Path(preview["database"]["source"])
    if source_database.is_file():
        database_result = _migrate_database(source_database, db_path())
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_repo": preview["source_repo"],
        "target_home": str(home),
        "files": manifest_files,
        "database": database_result,
    }
    manifest_path = home / "backups" / f"migration-manifest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "mode": "applied",
        "source_repo": preview["source_repo"],
        "target_home": str(home),
        "copied": copied,
        "skipped_identical": skipped,
        "database": database_result,
        "manifest": str(manifest_path),
        "source_preserved": True,
    }
