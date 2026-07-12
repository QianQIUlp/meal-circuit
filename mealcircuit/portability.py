from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .db import CURRENT_SCHEMA_VERSION, init_db
from .storage import (
    app_home,
    backups_root,
    db_path,
    exports_root,
    food_label_root,
    private_doctrine_path,
    profile_path,
    settings_path,
    upload_root,
)
from .validation import ValidationError


BUNDLE_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _database_snapshot(target: Path) -> None:
    source = sqlite3.connect(db_path())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        row = destination.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise sqlite3.DatabaseError("数据库快照完整性检查失败")
    finally:
        destination.close()
        source.close()


def _bundle_sources(snapshot: Path) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = [("data/mealcircuit.db", snapshot)]
    for archive_name, path in (
        ("config/settings.json", settings_path()),
        ("config/profile.md", profile_path()),
        ("config/doctrine.private.md", private_doctrine_path()),
    ):
        if path.is_file():
            sources.append((archive_name, path))
    for prefix, root in (("media/uploads", upload_root()), ("media/food-labels", food_label_root())):
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            sources.append((f"{prefix}/{path.relative_to(root).as_posix()}", path))
    return sources


def export_bundle(destination: str | Path | None = None) -> dict:
    init_db()
    exports_root().mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = Path(destination).expanduser().resolve() if destination else exports_root() / f"mealcircuit-{timestamp}.zip"
    if target.suffix.lower() != ".zip":
        raise ValidationError("导出文件必须使用 .zip 扩展名")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValidationError(f"导出文件已存在：{target}")
    with tempfile.TemporaryDirectory(prefix="mealcircuit-export-") as temporary:
        snapshot = Path(temporary) / "mealcircuit.db"
        _database_snapshot(snapshot)
        sources = _bundle_sources(snapshot)
        entries = []
        for archive_name, source in sources:
            data = source.read_bytes()
            entries.append({"path": archive_name, "size": len(data), "sha256": _sha256_bytes(data)})
        manifest = {
            "format": "mealcircuit-portable-bundle",
            "format_version": BUNDLE_FORMAT_VERSION,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entries": entries,
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            for archive_name, source in sources:
                archive.write(source, archive_name)
    return {
        "path": str(target),
        "format_version": BUNDLE_FORMAT_VERSION,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "files": len(entries),
        "sha256": _sha256_bytes(target.read_bytes()),
    }


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValidationError(f"导入包包含不安全路径：{name}")
    return path


def _require_private_child(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(app_home().resolve())
    except ValueError as exc:
        raise ValidationError(f"恢复目标不在 MealCircuit 私人目录内：{resolved}") from exc
    return resolved


def _load_bundle(bundle: str | Path) -> tuple[Path, dict, dict[str, bytes]]:
    source = Path(bundle).expanduser().resolve()
    if not source.is_file():
        raise ValidationError(f"导入包不存在：{source}")
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            for name in names:
                _safe_member(name)
            if MANIFEST_NAME not in names:
                raise ValidationError("导入包缺少 manifest.json")
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            files = {name: archive.read(name) for name in names if name != MANIFEST_NAME and not name.endswith("/")}
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取导入包：{exc}") from exc
    if manifest.get("format") != "mealcircuit-portable-bundle" or manifest.get("format_version") != BUNDLE_FORMAT_VERSION:
        raise ValidationError("导入包格式或版本不受支持")
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, int) or schema_version > CURRENT_SCHEMA_VERSION:
        raise ValidationError("导入包数据库版本高于当前程序支持范围")
    expected = {item["path"]: item for item in manifest.get("entries") or [] if isinstance(item, dict)}
    if set(expected) != set(files):
        raise ValidationError("导入包文件清单与实际内容不一致")
    for name, data in files.items():
        item = expected[name]
        if item.get("size") != len(data) or item.get("sha256") != _sha256_bytes(data):
            raise ValidationError(f"导入包文件校验失败：{name}")
    if "data/mealcircuit.db" not in files:
        raise ValidationError("导入包缺少数据库快照")
    return source, manifest, files


def preview_import(bundle: str | Path) -> dict:
    source, manifest, files = _load_bundle(bundle)
    with tempfile.TemporaryDirectory(prefix="mealcircuit-import-preview-") as temporary:
        snapshot = Path(temporary) / "mealcircuit.db"
        snapshot.write_bytes(files["data/mealcircuit.db"])
        conn = sqlite3.connect(snapshot)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValidationError("导入数据库完整性检查失败")
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            counts = {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
                for table in tables
            }
        finally:
            conn.close()
    return {
        "path": str(source),
        "format_version": manifest["format_version"],
        "schema_version": manifest["schema_version"],
        "created_at": manifest["created_at"],
        "files": len(files),
        "database_integrity": "ok",
        "table_counts": counts,
        "will_replace_database": str(db_path()),
        "will_restore_config": sorted(name for name in files if name.startswith("config/")),
        "will_restore_media": sum(1 for name in files if name.startswith("media/")),
    }


def restore_bundle(bundle: str | Path, *, confirm: bool = False) -> dict:
    if not confirm:
        raise ValidationError("恢复会替换当前数据库；必须显式 confirm=True")
    preview = preview_import(bundle)
    _, _, files = _load_bundle(bundle)
    backups_root().mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = None
    if db_path().is_file():
        backup = backups_root() / f"pre-restore-{timestamp}.zip"
        export_bundle(backup)
    with tempfile.TemporaryDirectory(prefix="mealcircuit-restore-") as temporary:
        temporary_root = Path(temporary)
        restored_db = temporary_root / "mealcircuit.db"
        restored_db.write_bytes(files["data/mealcircuit.db"])
        conn = sqlite3.connect(restored_db)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValidationError("恢复数据库完整性检查失败")
        finally:
            conn.close()
        db_path().parent.mkdir(parents=True, exist_ok=True)
        staged_db = db_path().with_name(f"{db_path().name}.restore-tmp")
        shutil.copy2(restored_db, staged_db)
        staged_config = temporary_root / "config"
        staged_uploads = temporary_root / "uploads"
        staged_labels = temporary_root / "food-labels"
        staged_config.mkdir()
        staged_uploads.mkdir()
        staged_labels.mkdir()
        config_targets = {
            "config/settings.json": settings_path(),
            "config/profile.md": profile_path(),
            "config/doctrine.private.md": private_doctrine_path(),
        }
        for name, target in config_targets.items():
            if name in files:
                (staged_config / target.name).write_bytes(files[name])
        for name, data in files.items():
            if name.startswith("media/uploads/"):
                relative = PurePosixPath(name).relative_to("media/uploads")
                target = staged_uploads.joinpath(*relative.parts)
            elif name.startswith("media/food-labels/"):
                relative = PurePosixPath(name).relative_to("media/food-labels")
                target = staged_labels.joinpath(*relative.parts)
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        Path(f"{db_path()}-wal").unlink(missing_ok=True)
        Path(f"{db_path()}-shm").unlink(missing_ok=True)
        os.replace(staged_db, db_path())
        for name, target in config_targets.items():
            staged = staged_config / target.name
            if not staged.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            staged_target = target.with_name(f"{target.name}.restore-tmp")
            shutil.copy2(staged, staged_target)
            os.replace(staged_target, target)
        for staged_media, target_root in (
            (staged_uploads, upload_root()), (staged_labels, food_label_root())
        ):
            target_root = _require_private_child(target_root)
            replacement = target_root.with_name(f"{target_root.name}.restore-tmp")
            if replacement.exists():
                shutil.rmtree(replacement)
            shutil.copytree(staged_media, replacement)
            if target_root.exists():
                shutil.rmtree(target_root)
            os.replace(replacement, target_root)
    init_db()
    return {
        **preview,
        "restored": True,
        "pre_restore_backup": str(backup) if backup else None,
    }
