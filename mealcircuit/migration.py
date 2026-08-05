from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime
from pathlib import Path, PureWindowsPath

from .storage import (
    WINDOWS_RESERVED_NAMES,
    app_home,
    db_path,
    ensure_no_reparse_components,
    ensure_secure_app_home,
    ensure_secure_directory,
    iter_regular_tree_files,
    path_is_reparse_point,
    process_data_locked,
)
from .validation import ValidationError


LEGACY_DOCTRINE = "减脂增肌饮食系统总纲.md"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _lexical_absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _validated_source_directory(
    path: Path,
    root: Path,
    *,
    required: bool,
) -> Path | None:
    path = ensure_no_reparse_components(path, root)
    try:
        result = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValidationError(f"迁移源目录不存在：{path}")
        return None
    except OSError as exc:
        raise ValidationError(f"无法检查迁移源目录：{path}") from exc
    if path_is_reparse_point(path):
        raise ValidationError(f"迁移源包含符号链接或重解析点：{path}")
    if not stat.S_ISDIR(result.st_mode):
        raise ValidationError(f"迁移源不是目录：{path}")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"迁移源目录逃逸所选目录：{path}") from exc
    return path


def _validated_repo(path: str | Path) -> Path:
    repo = _lexical_absolute_path(Path(path).expanduser())
    validated = _validated_source_directory(repo, repo, required=True)
    assert validated is not None
    return validated


def _validated_source_file(path: Path, root: Path) -> Path:
    path = ensure_no_reparse_components(path, root)
    if path_is_reparse_point(path):
        raise ValidationError(f"迁移源包含符号链接或重解析点：{path}")
    try:
        result = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"迁移源文件逃逸所选目录：{path}") from exc
    if not stat.S_ISREG(result.st_mode):
        raise ValidationError(f"迁移源不是普通文件：{path}")
    return path


def _optional_source_file(path: Path, root: Path) -> Path | None:
    path = ensure_no_reparse_components(path, root)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValidationError(f"无法检查迁移源文件：{path}") from exc
    return _validated_source_file(path, root)


def _source_files(repo: Path) -> list[tuple[Path, Path]]:
    repo = _validated_repo(repo)
    home = app_home()
    pairs: list[tuple[Path, Path]] = []
    doctrine = _optional_source_file(repo / LEGACY_DOCTRINE, repo)
    if doctrine is not None:
        pairs.append((doctrine, home / "doctrine.private.md"))
    for source_root, target_root in (
        (repo / "data" / "uploads", home / "uploads"),
        (repo / "data" / "food-labels", home / "food-labels"),
        (repo / "tmp", home / "archive" / "tmp-imports"),
    ):
        validated_root = _validated_source_directory(
            source_root,
            repo,
            required=False,
        )
        if validated_root is None:
            continue
        for source in iter_regular_tree_files(validated_root):
            source = _validated_source_file(source, repo)
            pairs.append((source, target_root / source.relative_to(validated_root)))
    return pairs


def migration_preview(source_repo: str | Path) -> dict:
    repo = _validated_repo(source_repo)
    database = _optional_source_file(repo / "data" / "dietos.db", repo)
    database_path = database or (repo / "data" / "dietos.db")
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
            "source": str(database_path),
            "target": str(target_db),
            "exists": database is not None,
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
    normalized = value.replace("/", "\\")
    if (
        normalized.startswith("\\\\")
        or normalized.startswith("\\??\\")
        or normalized.casefold().startswith("\\globalroot\\")
    ):
        raise ValidationError(f"旧数据库包含不安全的媒体路径：{value}")
    path = PureWindowsPath(value)
    parts = list(path.parts)
    folded = [part.casefold() for part in parts]
    marker = category.casefold()
    if ".." in parts:
        raise ValidationError(f"旧数据库包含不安全的媒体路径：{value}")
    if marker in folded:
        index = folded.index(marker)
        tail = parts[index + 1 :]
    else:
        if path.drive or path.root or len(parts) != 1:
            raise ValidationError(f"旧数据库包含不安全的媒体路径：{value}")
        tail = [path.name]
    if not tail or any(
        part in {"", ".", ".."}
        or part.endswith((" ", "."))
        or any(character in part for character in '<>:"/\\|?*\0')
        or part.rstrip(" .").split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
        for part in tail
    ):
        raise ValidationError(f"旧数据库包含不安全的媒体路径：{value}")
    return Path(category, *tail).as_posix()


def _migrate_database(source: Path, target: Path, source_root: Path) -> dict:
    source = _validated_source_file(source, source_root)
    ensure_secure_directory(target.parent)
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


def _apply_migration_to_active_home(
    source_repo: Path,
    *,
    manifest_target_home: Path | None = None,
) -> dict:
    preview = migration_preview(source_repo)
    if preview["conflicts"]:
        raise ValidationError(f"目标存在内容冲突：{preview['conflicts']}")
    home = ensure_secure_app_home()
    manifest_home = manifest_target_home or home
    copied = []
    skipped = []
    manifest_files = []
    for source, target in _source_files(Path(preview["source_repo"])):
        ensure_secure_directory(target.parent)
        source_hash = sha256(source)
        if target.exists():
            skipped.append(str(target))
        else:
            shutil.copy2(source, target)
            if sha256(target) != source_hash:
                target.unlink(missing_ok=True)
                raise ValidationError(f"文件复制校验失败：{source}")
            copied.append(str(target))
        manifest_target = manifest_home / target.relative_to(home)
        manifest_files.append(
            {
                "source": str(source),
                "target": str(manifest_target),
                "bytes": source.stat().st_size,
                "sha256": source_hash,
            }
        )
    database_result = None
    source_database = Path(preview["database"]["source"])
    if preview["database"]["exists"]:
        database_result = _migrate_database(
            source_database,
            db_path(),
            Path(preview["source_repo"]),
        )
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_repo": preview["source_repo"],
        "target_home": str(manifest_home),
        "files": manifest_files,
        "database": database_result,
    }
    manifest_path = home / "backups" / f"migration-manifest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    ensure_secure_directory(manifest_path.parent)
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


def _remap_staging_path(value: str, staging_home: Path, final_home: Path) -> str:
    path = Path(value)
    try:
        relative = path.resolve().relative_to(staging_home.resolve())
    except ValueError:
        return value
    return str(final_home / relative)


@process_data_locked()
def apply_migration(source_repo: str | Path) -> dict:
    source = _validated_repo(source_repo)
    final_home = app_home().resolve()
    from .portable import atomic_home_update

    with atomic_home_update():
        result = _apply_migration_to_active_home(
            source,
            manifest_target_home=final_home,
        )
        staging_home = Path(result["target_home"]).resolve()
    result["target_home"] = str(final_home)
    result["copied"] = [
        _remap_staging_path(value, staging_home, final_home)
        for value in result["copied"]
    ]
    result["skipped_identical"] = [
        _remap_staging_path(value, staging_home, final_home)
        for value in result["skipped_identical"]
    ]
    result["manifest"] = _remap_staging_path(
        result["manifest"],
        staging_home,
        final_home,
    )
    return result
