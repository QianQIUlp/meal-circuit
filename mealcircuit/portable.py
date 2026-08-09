from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import stat
import struct
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from . import __version__
from .configuration import load_settings
from .crypto import decrypt, derive_key, encrypt, format_recovery_key, parse_recovery_key, random_key
from .db import connect, init_db
from .domain import (
    DOMAIN_SCHEMA_VERSION,
    DomainRevision,
    make_revision,
    new_id,
    three_way_merge,
    utc_now,
    validate_payload,
    validate_revision,
)
from .storage import (
    DATA_DIRECTORY_LOCK,
    DataDirectoryBusyError,
    app_home,
    background_data_operations_active,
    copy_tree_without_reparse_points,
    create_secure_directory,
    data_home_identity,
    db_path,
    ensure_no_reparse_components,
    ensure_secure_directory,
    managed_asset_root,
    private_doctrine_path,
    profile_path,
    process_data_lock,
    process_data_locked,
    resolve_managed_media_path,
    settings_path,
    validate_private_directory_security,
)
from .validation import ValidationError


PORTABLE_FORMAT = "mealcircuit.portable"
PORTABLE_VERSION = 1
ENCRYPTED_MAGIC = b"MCX1\n"
CHUNK_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_FILES = 100_000
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024 * 1024
MAX_IMPORT_SOURCE_BYTES = MAX_ARCHIVE_BYTES + 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_METADATA_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_MCX_HEADER_BYTES = 64 * 1024
UUID_NAMESPACE = uuid.UUID("2a7c0c93-763f-4d3c-93f1-c8a5768da92a")
_IMPORT_LOCK = DATA_DIRECTORY_LOCK
_IMPORT_ACTIVE = False
_JOURNAL_OWNER_MARKER = ".mealcircuit-import-owner"
_HOME_OWNER_MARKER = ".mealcircuit-home-owner"
ASSET_DESCRIPTOR_FIELDS = frozenset({"id", "sha256", "path", "bytes", "media_type"})
LEGACY_ASSET_DESCRIPTOR_FIELDS = frozenset({"sha256", "path", "bytes"})
ASSET_MEDIA_EXTENSIONS = {
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/png": frozenset({".png"}),
    "image/gif": frozenset({".gif"}),
    "image/webp": frozenset({".webp"}),
}
PORTABLE_TEMP_STALE_SECONDS = 24 * 60 * 60


class ImportInProgressError(ValidationError):
    """Raised when another process owns the recoverable home-import transaction."""


class ImportRollbackError(RuntimeError):
    """An import failed and the automatic rollback must be retried on startup."""


def _lexical_absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _same_lexical_path(left: Path, right: Path) -> bool:
    left_value = str(_lexical_absolute_path(left))
    right_value = str(_lexical_absolute_path(right))
    if os.name == "nt":
        left_value = left_value.casefold()
        right_value = right_value.casefold()
    return left_value == right_value


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(f"无法检查导入事务路径：{path}") from exc
    return True


def _write_owner_marker(
    directory: Path,
    marker_name: str,
    transaction_id: str,
    *,
    require_new: bool,
) -> None:
    ensure_secure_directory(directory)
    marker = directory / marker_name
    mode = "x" if require_new else "w"
    try:
        with marker.open(mode, encoding="ascii", newline="\n") as stream:
            stream.write(transaction_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise RuntimeError(f"无法写入导入事务归属标记：{marker}") from exc


def _require_owned_directory(
    directory: Path,
    marker_name: str,
    transaction_id: str,
) -> None:
    validate_private_directory_security(directory)
    marker = directory / marker_name
    try:
        marker_stat = marker.lstat()
    except OSError as exc:
        raise RuntimeError(f"导入事务目录缺少归属标记：{directory}") from exc
    attributes = int(getattr(marker_stat, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(marker_stat.st_mode)
        or attributes & 0x400
        or not stat.S_ISREG(marker_stat.st_mode)
    ):
        raise RuntimeError(f"导入事务归属标记不是普通文件：{marker}")
    try:
        marker_value = marker.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取导入事务归属标记：{marker}") from exc
    if marker_value != transaction_id:
        raise RuntimeError(f"导入事务目录归属无法证明：{directory}")


def _remove_owned_tree(
    directory: Path,
    marker_name: str,
    transaction_id: str,
) -> None:
    if not _entry_exists(directory):
        return
    _require_owned_directory(directory, marker_name, transaction_id)
    shutil.rmtree(directory)


@contextlib.contextmanager
def _cross_process_import_lock(home: Path) -> Iterator[None]:
    """Hold a non-blocking, per-home OS file lock for recovery and promotion."""
    try:
        with process_data_lock(home, wait=False):
            yield
    except DataDirectoryBusyError as exc:
        raise ImportInProgressError(
            "另一 MealCircuit 实例正在读取、导入或恢复数据；请等待它完成后重试"
        ) from exc


class _ImportTransaction:
    """Build an import in a sibling directory, then atomically promote it."""

    ENVIRONMENT_KEYS = ("MEALCIRCUIT_HOME", "MEALCIRCUIT_DB", "DIETOS_DB", "MEALCIRCUIT_DOCTRINE")

    def __init__(self) -> None:
        self.home = _lexical_absolute_path(app_home())
        self.home_existed = _entry_exists(self.home)
        self.transaction_id = uuid.uuid4().hex
        self.promotion_recorded = False
        database = _lexical_absolute_path(db_path())
        doctrine = _lexical_absolute_path(private_doctrine_path())
        try:
            self.database_relative = database.relative_to(self.home)
            self.doctrine_relative = doctrine.relative_to(self.home)
        except ValueError as exc:
            raise ValidationError(
                "原子导入要求数据库与私人 doctrine 位于 MEALCIRCUIT_HOME 内；请先迁回统一数据目录"
            ) from exc
        identity = data_home_identity(self.home)
        self.journal = self.home.parent / f".mealcircuit-import-rollback-{identity}"
        if _entry_exists(self.journal):
            raise RuntimeError(f"存在未恢复的导入事务：{self.journal}")
        create_secure_directory(self.journal)
        try:
            _write_owner_marker(
                self.journal,
                _JOURNAL_OWNER_MARKER,
                self.transaction_id,
                require_new=True,
            )
        except BaseException:
            shutil.rmtree(self.journal, ignore_errors=True)
            raise
        self.staging = self.home.parent / (
            f".mealcircuit-import-staging-{identity}-{self.transaction_id}"
        )
        self.backup = self.journal / "previous-home"
        self.state = "preparing"
        staging_created = False
        try:
            self._write_manifest()
            if self.home_existed:
                copy_tree_without_reparse_points(self.home, self.staging)
            else:
                create_secure_directory(self.staging)
            staging_created = True
            _write_owner_marker(
                self.staging,
                _HOME_OWNER_MARKER,
                self.transaction_id,
                require_new=not self.home_existed,
            )
            self.state = "prepared"
            self._write_manifest()
        except BaseException:
            if staging_created:
                try:
                    _remove_owned_tree(
                        self.staging,
                        _HOME_OWNER_MARKER,
                        self.transaction_id,
                    )
                except RuntimeError:
                    # require-new plus this in-memory flag proves constructor ownership.
                    shutil.rmtree(self.staging, ignore_errors=True)
            self.close()
            raise

    def _write_manifest(self) -> None:
        value = {
            "version": 3,
            "transaction_id": self.transaction_id,
            "state": self.state,
            "home": str(self.home),
            "home_existed": self.home_existed,
            "staging": str(self.staging),
            "backup": str(self.backup),
            "database_relative": self.database_relative.as_posix(),
            "doctrine_relative": self.doctrine_relative.as_posix(),
        }
        temporary = self.journal / "manifest.tmp"
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.journal / "manifest.json")

    @classmethod
    def from_manifest(
        cls,
        journal: Path,
        value: dict,
        *,
        expected_home: Path,
    ) -> "_ImportTransaction":
        if value.get("version") != 3:
            raise RuntimeError("导入回滚日志版本不受支持")
        transaction_id = value.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or len(transaction_id) != 32
            or any(character not in "0123456789abcdef" for character in transaction_id)
        ):
            raise RuntimeError("导入事务标识无效")
        item = cls.__new__(cls)
        item.journal = _lexical_absolute_path(journal)
        item.transaction_id = transaction_id
        item.state = str(value["state"])
        if item.state not in {"preparing", "prepared", "original_moved", "staging_promoted"}:
            raise RuntimeError("导入事务状态无效")
        item.promotion_recorded = item.state == "staging_promoted"
        item.home = _lexical_absolute_path(value["home"])
        if not _same_lexical_path(item.home, expected_home):
            raise RuntimeError("导入事务 home 与当前私人目录不匹配")
        item.home_existed = bool(value["home_existed"])
        item.staging = _lexical_absolute_path(value["staging"])
        item.backup = _lexical_absolute_path(value["backup"])
        item.database_relative = Path(value["database_relative"])
        item.doctrine_relative = Path(value["doctrine_relative"])
        identity = data_home_identity(item.home)
        expected_journal = item.home.parent / f".mealcircuit-import-rollback-{identity}"
        expected_staging = item.home.parent / (
            f".mealcircuit-import-staging-{identity}-{item.transaction_id}"
        )
        expected_backup = item.journal / "previous-home"
        if not _same_lexical_path(item.journal, expected_journal):
            raise RuntimeError("导入事务日志身份不匹配")
        if not _same_lexical_path(item.backup, expected_backup):
            raise RuntimeError("导入事务备份路径逃逸")
        if not _same_lexical_path(item.staging, expected_staging):
            raise RuntimeError("导入事务 staging 路径逃逸")
        for relative in (item.database_relative, item.doctrine_relative):
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("导入事务相对路径逃逸")
        _require_owned_directory(
            item.journal,
            _JOURNAL_OWNER_MARKER,
            item.transaction_id,
        )
        return item

    @contextlib.contextmanager
    def activated(self) -> Iterator[None]:
        previous = {key: os.environ.get(key) for key in self.ENVIRONMENT_KEYS}
        os.environ["MEALCIRCUIT_HOME"] = str(self.staging)
        os.environ["MEALCIRCUIT_DB"] = str(self.staging / self.database_relative)
        os.environ["MEALCIRCUIT_DOCTRINE"] = str(self.staging / self.doctrine_relative)
        os.environ.pop("DIETOS_DB", None)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def commit(self) -> None:
        if self.state != "prepared":
            raise RuntimeError("导入 staging 未准备完成")
        _require_owned_directory(
            self.staging,
            _HOME_OWNER_MARKER,
            self.transaction_id,
        )
        if self.home_existed:
            if _entry_exists(self.backup):
                raise RuntimeError("导入事务 backup 已被预置")
            os.replace(self.home, self.backup)
            ensure_secure_directory(self.backup)
        self.state = "original_moved"
        self._write_manifest()
        if not self.home_existed and _entry_exists(self.home):
            raise RuntimeError("新的 MealCircuit 私人目录在提交前被其他进程创建")
        os.replace(self.staging, self.home)
        # Set the in-memory state immediately after the rename. Recovery can now
        # distinguish the promoted staging tree from an unrelated competing home.
        self.state = "staging_promoted"
        self._write_manifest()
        self.promotion_recorded = True
        shutil.rmtree(self.backup, ignore_errors=True)
        self.close()

    def restore(self) -> None:
        if _entry_exists(self.backup):
            ensure_secure_directory(self.backup)
            if _entry_exists(self.home):
                _remove_owned_tree(
                    self.home,
                    _HOME_OWNER_MARKER,
                    self.transaction_id,
                )
            os.replace(self.backup, self.home)
        elif not self.home_existed:
            if _entry_exists(self.home):
                _remove_owned_tree(
                    self.home,
                    _HOME_OWNER_MARKER,
                    self.transaction_id,
                )
        if _entry_exists(self.staging):
            _remove_owned_tree(
                self.staging,
                _HOME_OWNER_MARKER,
                self.transaction_id,
            )

    def close(self) -> None:
        _remove_owned_tree(
            self.journal,
            _JOURNAL_OWNER_MARKER,
            self.transaction_id,
        )


def _journal_root(home: Path | None = None) -> Path:
    home = _lexical_absolute_path(home or app_home())
    identity = data_home_identity(home)
    return home.parent / f".mealcircuit-import-rollback-{identity}"


def _recover_interrupted_import_locked(home: Path) -> bool:
    journal = _journal_root(home)
    if not _entry_exists(journal):
        return False
    try:
        validate_private_directory_security(journal)
    except ValidationError as exc:
        raise RuntimeError(
            f"导入事务日志未通过当前用户与 DACL 验证；已保留现场：{journal}"
        ) from exc
    manifest_path = journal / "manifest.json"
    try:
        manifest_stat = manifest_path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"导入事务日志缺少有效 manifest；已保留现场：{journal}"
        ) from exc
    manifest_attributes = int(getattr(manifest_stat, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(manifest_stat.st_mode)
        or manifest_attributes & 0x400
        or not stat.S_ISREG(manifest_stat.st_mode)
    ):
        raise RuntimeError(
            f"导入事务 manifest 不是普通文件；已保留现场：{journal}"
        )
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        transaction = _ImportTransaction.from_manifest(
            journal,
            value,
            expected_home=_lexical_absolute_path(home),
        )
        if transaction.state == "staging_promoted":
            _require_owned_directory(
                transaction.home,
                _HOME_OWNER_MARKER,
                transaction.transaction_id,
            )
            if _entry_exists(transaction.backup):
                ensure_secure_directory(transaction.backup)
                shutil.rmtree(transaction.backup)
            if _entry_exists(transaction.staging):
                _remove_owned_tree(
                    transaction.staging,
                    _HOME_OWNER_MARKER,
                    transaction.transaction_id,
                )
        else:
            transaction.restore()
        transaction.close()
    except Exception as exc:
        raise RuntimeError(
            f"无法安全恢复中断的 Portable Data 导入；已保留事务现场：{journal}"
        ) from exc
    return True


def recover_interrupted_import() -> bool:
    """Restore a crash-interrupted import before opening the default database."""
    if _IMPORT_ACTIVE:
        return False
    with _IMPORT_LOCK:
        home = _lexical_absolute_path(app_home())
        with _cross_process_import_lock(home):
            return _recover_interrupted_import_locked(home)


@contextlib.contextmanager
def _exclusive_import_session() -> Iterator[None]:
    global _IMPORT_ACTIVE
    with _IMPORT_LOCK:
        if background_data_operations_active():
            raise ValidationError(
                "后台智能生成仍在运行；为避免混合数据，请等待生成完成后再恢复备份"
            )
        home = _lexical_absolute_path(app_home())
        with _cross_process_import_lock(home):
            _recover_interrupted_import_locked(home)
            _IMPORT_ACTIVE = True
            try:
                yield
            finally:
                _IMPORT_ACTIVE = False


@contextlib.contextmanager
def _home_import_transaction() -> Iterator[None]:
    transaction: _ImportTransaction | None = None
    try:
        transaction = _ImportTransaction()
        with transaction.activated():
            yield
        transaction.commit()
    except BaseException as primary_error:
        if transaction is not None:
            if transaction.promotion_recorded:
                raise ImportRollbackError(
                    "数据已原子提升，但提交后的清理未完成；事务日志已保留，"
                    f"下次启动将继续恢复：{transaction.journal}。"
                    f"原始错误：{primary_error!r}"
                ) from primary_error
            try:
                transaction.restore()
            except BaseException as rollback_error:
                raise ImportRollbackError(
                    "数据导入失败且自动回滚也失败；原数据仍保存在回滚日志中，"
                    f"请勿删除 {transaction.journal}。"
                    f"原始错误：{primary_error!r}；回滚错误：{rollback_error!r}"
                ) from rollback_error
            else:
                transaction.close()
        raise


@contextlib.contextmanager
def atomic_home_update() -> Iterator[None]:
    """Apply a whole-home update through the existing recoverable import journal."""
    with _exclusive_import_session():
        with _home_import_transaction():
            yield


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode_row(row: sqlite3.Row) -> dict:
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


def _metadata(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute("SELECT value FROM app_metadata WHERE key=?", (key,)).fetchone()
    if not row:
        raise ValidationError(f"数据库缺少元数据：{key}")
    return str(row[0])


def _revision(
    entity_kind: str,
    entity_id: str,
    payload: dict,
    created_at: str,
    author_device_id: str,
    *,
    deleted: bool = False,
) -> DomainRevision:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    revision_uuid = uuid.uuid5(UUID_NAMESPACE, f"{entity_kind}\0{entity_id}\0{canonical}\0{int(deleted)}")
    return validate_revision(
        {
            "schema_version": DOMAIN_SCHEMA_VERSION,
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "revision_id": f"rev_{revision_uuid}",
            "parent_revision_ids": [],
            "created_at": created_at or utc_now(),
            "author_device_id": author_device_id,
            "deleted": deleted,
            "payload": payload,
        }
    )


def _preference_id(kind: str) -> str:
    return f"preferences_{uuid.uuid5(UUID_NAMESPACE, kind)}"


def _collect_assets(connection: sqlite3.Connection) -> tuple[dict[str, dict], dict[str, Path]]:
    references: set[str] = set()
    for row in connection.execute("SELECT image_path FROM tasks WHERE image_path IS NOT NULL"):
        references.add(str(row[0]))
    for row in connection.execute("SELECT package_photo_path FROM food_items WHERE package_photo_path IS NOT NULL"):
        references.add(str(row[0]))
    mapping: dict[str, dict] = {}
    sources: dict[str, Path] = {}
    for reference in sorted(references):
        try:
            path = resolve_managed_media_path(reference)
        except ValidationError:
            mapping[reference] = {"external_reference": reference, "unresolved": True}
            continue
        if path.stat().st_size > MAX_ASSET_BYTES:
            mapping[reference] = {"external_reference": reference, "unresolved": True}
            continue
        digest = _sha256_path(path)
        extension = path.suffix.lower() or ".bin"
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        asset_id = f"asset_{digest}"
        archive_path = f"assets/{digest}{extension}"
        mapping[reference] = {"asset_id": asset_id}
        sources[archive_path] = path
    return mapping, sources


def collect_revisions() -> tuple[list[DomainRevision], dict[str, Path]]:
    init_db()
    revisions: list[DomainRevision] = []
    asset_sources: dict[str, Path] = {}
    with connect() as connection:
        asset_rows = {
            row["id"]: row
            for row in connection.execute("SELECT * FROM managed_assets")
        }
        for row in connection.execute(
            "SELECT * FROM domain_revisions ORDER BY entity_kind,entity_id,created_at,revision_id"
        ):
            payload = json.loads(row["payload_json"])
            if row["entity_kind"] == "asset":
                asset = asset_rows.get(row["entity_id"])
                if asset is None or not asset["relative_path"]:
                    raise ValidationError(
                        f"受管资产尚未下载，无法生成完整数据包：{row['entity_id']}"
                    )
                try:
                    path = resolve_managed_media_path(asset["relative_path"])
                except ValidationError as exc:
                    raise ValidationError(
                        f"受管资产路径无效：{row['entity_id']}"
                    ) from exc
                if _sha256_path(path) != asset["sha256"]:
                    raise ValidationError(f"受管资产缺失或哈希不一致：{row['entity_id']}")
                archive_path = f"assets/{asset['sha256']}{asset['extension']}"
                payload["archive_path"] = archive_path
                asset_sources[archive_path] = path
            revisions.append(
                validate_revision(
                    {
                        "schema_version": row["schema_version"],
                        "entity_id": row["entity_id"],
                        "entity_kind": row["entity_kind"],
                        "revision_id": row["revision_id"],
                        "parent_revision_ids": json.loads(row["parent_revision_ids_json"]),
                        "created_at": row["created_at"],
                        "author_device_id": row["author_device_id"],
                        "deleted": bool(row["deleted"]),
                        "payload": payload,
                    }
                )
            )
    revisions.sort(key=lambda item: (item.entity_kind, item.entity_id, item.revision_id))
    return revisions, asset_sources


def _current_heads() -> dict[str, str]:
    init_db()
    with connect() as connection:
        return {
            row["entity_id"]: row["revision_id"]
            for row in connection.execute("SELECT entity_id,revision_id FROM entity_heads")
        }


def _jsonl(items: list[DomainRevision]) -> bytes:
    return (
        "\n".join(
            json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in items
        )
        + ("\n" if items else "")
    ).encode("utf-8")


def _bind_asset_descriptors(
    revisions: list[DomainRevision],
    heads: dict[str, str],
    descriptors: list[dict],
) -> list[tuple[dict, DomainRevision]]:
    """Bind each manifest asset to exactly one authoritative ASSET head."""
    revision_by_id = {item.revision_id: item for item in revisions}
    if len(revision_by_id) != len(revisions):
        raise ValidationError("Portable Data 包含重复 revision")

    asset_heads: dict[str, DomainRevision] = {}
    for entity_id, revision_id in heads.items():
        if not isinstance(entity_id, str) or not isinstance(revision_id, str):
            raise ValidationError("manifest entity_heads 字段类型无效")
        revision = revision_by_id.get(revision_id)
        if revision is None or revision.entity_id != entity_id:
            raise ValidationError(f"manifest entity head 无效：{entity_id}")
        if revision.entity_kind == "asset":
            asset_heads[entity_id] = revision

    asset_metadata: dict[str, dict[str, object]] = {}
    for asset_id, revision in asset_heads.items():
        payload = revision.payload
        for field in ("sha256", "media_type", "extension", "archive_path"):
            if not isinstance(payload.get(field), str):
                raise ValidationError(f"ASSET head 的 {field} 必须是字符串：{asset_id}")
        if type(payload.get("byte_count")) is not int:
            raise ValidationError(f"ASSET head 的 byte_count 必须是整数：{asset_id}")

        digest = payload["sha256"]
        media_type = payload["media_type"]
        extension = payload["extension"]
        byte_count = payload["byte_count"]
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValidationError(f"ASSET head 的 sha256 必须是 64 位小写十六进制字符串：{asset_id}")
        allowed_extensions = ASSET_MEDIA_EXTENSIONS.get(media_type)
        if allowed_extensions is None:
            raise ValidationError(f"ASSET head 的 media_type 不受支持：{asset_id}")
        if extension not in allowed_extensions:
            raise ValidationError(f"ASSET head 的 extension 与 media_type 不匹配：{asset_id}")
        if byte_count < 0 or byte_count > MAX_ASSET_BYTES:
            raise ValidationError(f"ASSET head 的 byte_count 超过安全边界：{asset_id}")
        expected_path = f"assets/{digest}{extension}"
        if payload["archive_path"] != expected_path:
            raise ValidationError(f"ASSET head 的 archive_path 与认证元数据不一致：{asset_id}")
        asset_metadata[asset_id] = {
            "sha256": digest,
            "media_type": media_type,
            "bytes": byte_count,
            "path": expected_path,
        }

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    bindings: list[tuple[dict, DomainRevision]] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) not in {
            ASSET_DESCRIPTOR_FIELDS,
            LEGACY_ASSET_DESCRIPTOR_FIELDS,
        }:
            raise ValidationError("资产 descriptor 字段必须精确匹配协议")
        string_fields = ("sha256", "path")
        if set(descriptor) == ASSET_DESCRIPTOR_FIELDS:
            string_fields += ("id", "media_type")
        for field in string_fields:
            if not isinstance(descriptor[field], str):
                raise ValidationError(f"资产字段 {field} 必须是字符串")
        if type(descriptor["bytes"]) is not int:  # bool is not a JSON integer here.
            raise ValidationError("资产字段 bytes 必须是整数")

        path = descriptor["path"]
        if not path:
            raise ValidationError("manifest 资产标识无效")
        if set(descriptor) == LEGACY_ASSET_DESCRIPTOR_FIELDS:
            candidates = [
                asset_id
                for asset_id, metadata in asset_metadata.items()
                if metadata["sha256"] == descriptor["sha256"]
                and metadata["path"] == path
                and metadata["bytes"] == descriptor["bytes"]
            ]
            if len(candidates) != 1:
                raise ValidationError("历史资产 descriptor 无法唯一绑定到 ASSET head")
            asset_id = candidates[0]
            descriptor = {
                **descriptor,
                "id": asset_id,
                "media_type": asset_metadata[asset_id]["media_type"],
            }
        else:
            asset_id = descriptor["id"]
        if not asset_id:
            raise ValidationError("manifest 资产标识无效")
        if asset_id in seen_ids:
            raise ValidationError(f"Portable Data 包含重复资产 ID：{asset_id}")
        if path in seen_paths:
            raise ValidationError(f"Portable Data 包含重复资产路径：{path}")
        seen_ids.add(asset_id)
        seen_paths.add(path)

        revision = asset_heads.get(asset_id)
        if revision is None:
            raise ValidationError(f"资产 descriptor 没有对应的 ASSET head：{asset_id}")
        metadata = asset_metadata[asset_id]
        if descriptor["sha256"] != metadata["sha256"]:
            raise ValidationError(f"资产 descriptor 的 sha256 与 ASSET head 不一致：{asset_id}")
        if descriptor["media_type"] != metadata["media_type"]:
            raise ValidationError(f"资产 descriptor 的 media_type 与 ASSET head 不一致：{asset_id}")
        if descriptor["bytes"] != metadata["bytes"]:
            raise ValidationError(f"资产 descriptor 的 bytes 与 ASSET head 不一致：{asset_id}")
        if path != metadata["path"]:
            raise ValidationError(
                f"资产 descriptor 的 path/archive_path 与 ASSET head 不一致：{asset_id}"
            )
        bindings.append((descriptor, revision))

    if seen_ids != set(asset_heads):
        raise ValidationError("资产 descriptor 与 ASSET heads 不是一一对应关系")
    return bindings


def _referenced_asset_ids(revisions: Iterable[DomainRevision]) -> set[str]:
    referenced: set[str] = set()
    pending: list[object] = []
    for revision in revisions:
        if revision.entity_kind == "asset":
            continue
        pending.append(revision.payload)
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.endswith("asset_id") and isinstance(child, str):
                        referenced.add(child)
                    pending.append(child)
            elif isinstance(value, list):
                pending.extend(value)
    return referenced


def _require_asset_references(
    revisions: Iterable[DomainRevision], available_asset_ids: set[str]
) -> None:
    missing = sorted(_referenced_asset_ids(revisions) - available_asset_ids)
    if missing:
        raise ValidationError(f"领域实体引用了缺失资产：{missing}")


def _build_zip(target: Path) -> dict:
    revisions, assets = collect_revisions()
    heads = _current_heads()
    grouped: dict[str, list[DomainRevision]] = {}
    for revision in revisions:
        grouped.setdefault(revision.entity_kind, []).append(revision)
    contents: dict[str, bytes] = {
        f"entities/{kind}.jsonl": _jsonl(items) for kind, items in sorted(grouped.items())
    }
    revision_by_id = {item.revision_id: item for item in revisions}
    asset_descriptors = []
    for entity_id, revision_id in sorted(heads.items()):
        revision = revision_by_id[revision_id]
        if revision.entity_kind != "asset":
            continue
        payload = revision.payload
        asset_descriptors.append(
            {
                "id": entity_id,
                "sha256": payload["sha256"],
                "path": payload["archive_path"],
                "bytes": payload["byte_count"],
                "media_type": payload["media_type"],
            }
        )
    bindings = _bind_asset_descriptors(revisions, heads, asset_descriptors)
    _require_asset_references(
        revisions,
        {descriptor["id"] for descriptor, _ in bindings},
    )
    archived_assets: dict[str, Path] = {}
    for descriptor, _ in bindings:
        path = descriptor["path"]
        source = assets.get(path)
        if source is None:
            raise ValidationError(f"受管资产缺少归档源文件：{descriptor['id']}")
        if source.stat().st_size != descriptor["bytes"] or _sha256_path(source) != descriptor["sha256"]:
            raise ValidationError(f"受管资产文件与认证元数据不一致：{descriptor['id']}")
        archived_assets[path] = source
    manifest = {
        "format": PORTABLE_FORMAT,
        "format_version": PORTABLE_VERSION,
        "domain_schema_version": DOMAIN_SCHEMA_VERSION,
        "application_version": __version__,
        "created_at": utc_now(),
        "entity_heads": heads,
        "content": {
            path: {"count": len(grouped[path.removeprefix("entities/").removesuffix(".jsonl")]), "sha256": _sha256_bytes(data)}
            for path, data in sorted(contents.items())
        },
        "assets": asset_descriptors,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for path, data in contents.items():
            archive.writestr(path, data)
        for path, source in archived_assets.items():
            archive.write(source, path)
    return manifest


def _encrypt_zip(source: Path, target: Path) -> str:
    recovery_secret = random_key()
    salt = os.urandom(32)
    key = derive_key(recovery_secret, salt=salt, info=b"mealcircuit-portable-v1")
    header = {
        "format": "mealcircuit.mcx",
        "version": 1,
        "algorithm": "AES-256-GCM",
        "kdf": "HKDF-SHA256",
        "salt": base64.b64encode(salt).decode("ascii"),
        "chunk_bytes": CHUNK_BYTES,
    }
    header_line = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, target.open("wb") as output_stream:
        output_stream.write(ENCRYPTED_MAGIC)
        output_stream.write(header_line + b"\n")
        index = 0
        for block in iter(lambda: input_stream.read(CHUNK_BYTES), b""):
            aad = b"MealCircuit Portable v1\0" + header_line + struct.pack(">Q", index)
            nonce, ciphertext = encrypt(key, block, aad)
            output_stream.write(struct.pack(">I", len(ciphertext)))
            output_stream.write(nonce)
            output_stream.write(ciphertext)
            index += 1
        output_stream.write(struct.pack(">I", 0))
    return format_recovery_key(recovery_secret)


@process_data_locked()
def export_data(output: str | Path, *, encrypted: bool = True) -> dict:
    target = Path(output).expanduser().resolve()
    if target.exists():
        raise ValidationError(f"导出目标已存在：{target}")
    fd, temporary_name = tempfile.mkstemp(prefix="mealcircuit-portable-", suffix=".zip")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        manifest = _build_zip(temporary)
        if encrypted:
            recovery_key = _encrypt_zip(temporary, target)
        else:
            shutil.copyfile(temporary, target)
            recovery_key = None
        return {
            "status": "exported",
            "path": str(target),
            "encrypted": encrypted,
            "recovery_key": recovery_key,
            "entity_count": sum(item["count"] for item in manifest["content"].values()),
            "asset_count": len(manifest["assets"]),
            "sha256": _sha256_path(target),
        }
    finally:
        temporary.unlink(missing_ok=True)


def _decrypt_archive(source: Path, recovery_key: str, target: Path) -> None:
    secret = parse_recovery_key(recovery_key)
    with source.open("rb") as input_stream:
        if input_stream.read(len(ENCRYPTED_MAGIC)) != ENCRYPTED_MAGIC:
            raise ValidationError("不是 MealCircuit 加密数据包")
        header_line = input_stream.readline(MAX_MCX_HEADER_BYTES + 1)
        if len(header_line) > MAX_MCX_HEADER_BYTES:
            raise ValidationError("加密数据包头过大")
        if not header_line.endswith(b"\n"):
            raise ValidationError("加密数据包头无效")
        header_line = header_line.rstrip(b"\n")
        try:
            header = json.loads(header_line)
            salt = base64.b64decode(header["salt"], validate=True)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("加密数据包头无效") from exc
        if header.get("version") != 1 or header.get("algorithm") != "AES-256-GCM":
            raise ValidationError("不支持的加密数据包版本或算法")
        key = derive_key(secret, salt=salt, info=b"mealcircuit-portable-v1")
        total = 0
        index = 0
        with target.open("wb") as output_stream:
            while True:
                raw_length = input_stream.read(4)
                if len(raw_length) != 4:
                    raise ValidationError("加密数据包被截断")
                length = struct.unpack(">I", raw_length)[0]
                if length == 0:
                    break
                if length < 17 or length > CHUNK_BYTES + 16:
                    raise ValidationError("加密数据块过大")
                nonce = input_stream.read(12)
                ciphertext = input_stream.read(length)
                if len(nonce) != 12 or len(ciphertext) != length:
                    raise ValidationError("加密数据包被截断")
                aad = b"MealCircuit Portable v1\0" + header_line + struct.pack(">Q", index)
                plaintext = decrypt(key, nonce, ciphertext, aad)
                total += len(plaintext)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValidationError("解密数据包超过大小限制")
                output_stream.write(plaintext)
                index += 1
            if input_stream.read(1):
                raise ValidationError("加密数据包尾部包含未知数据")


@contextlib.contextmanager
def _zip_path(source: Path, recovery_key: str | None) -> Iterator[Path]:
    with source.open("rb") as stream:
        encrypted = stream.read(len(ENCRYPTED_MAGIC)) == ENCRYPTED_MAGIC
    if not encrypted:
        yield source
        return
    if not recovery_key:
        raise ValidationError("导入加密数据包需要恢复密钥")
    fd, temporary_name = tempfile.mkstemp(prefix="mealcircuit-import-", suffix=".zip")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        _decrypt_archive(source, recovery_key, temporary)
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _snapshot_import_source(source: Path) -> Iterator[Path]:
    root = _portable_temp_root()
    ensure_no_reparse_components(root, Path(root.anchor))
    _cleanup_stale_portable_temp(root)
    root = ensure_secure_directory(root)
    ensure_no_reparse_components(root, Path(root.anchor))
    fd, temporary_name = tempfile.mkstemp(
        prefix="mealcircuit-source-", suffix=".portable", dir=root
    )
    temporary = Path(temporary_name)
    try:
        ensure_no_reparse_components(temporary, root)
        temporary_stat = temporary.lstat()
        if not stat.S_ISREG(temporary_stat.st_mode):
            raise ValidationError(f"Portable Data 临时文件无效：{temporary}")
        raw_output = os.fdopen(fd, "wb")
        fd = -1
        with raw_output as output_stream:
            with source.open("rb") as input_stream:
                total = 0
                while True:
                    chunk = input_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_IMPORT_SOURCE_BYTES:
                        raise ValidationError("导入数据包源文件超过大小限制")
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        ensure_no_reparse_components(temporary, root)
        if not stat.S_ISREG(temporary.lstat().st_mode):
            raise ValidationError(f"Portable Data 临时文件无效：{temporary}")
        yield temporary
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _portable_temp_root() -> Path:
    configured_home = os.environ.get("MEALCIRCUIT_HOME")
    home_candidate = (
        Path(os.path.abspath(os.fspath(Path(configured_home).expanduser())))
        if configured_home
        else app_home()
    )
    home = ensure_secure_directory(home_candidate)
    ensure_no_reparse_components(home, Path(home.anchor))
    identity = hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:16]
    root = home.parent / f".mealcircuit-portable-temp-{identity}"
    root = ensure_secure_directory(root)
    ensure_no_reparse_components(root, Path(root.anchor))
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValidationError(f"Portable Data 临时根无效：{root}")
    return root


def _cleanup_stale_portable_temp(root: Path) -> None:
    cutoff = time.time() - PORTABLE_TEMP_STALE_SECONDS
    for child in root.iterdir():
        try:
            if child.stat(follow_symlinks=False).st_mtime > cutoff:
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child, ignore_errors=True)
        except FileNotFoundError:
            continue


def _safe_member(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or ".." in path.parts or "\\" in info.filename or not info.filename:
        raise ValidationError(f"数据包包含不安全路径：{info.filename}")
    if info.filename == "manifest.json":
        limit = MAX_MANIFEST_BYTES
    elif info.filename.startswith("assets/"):
        limit = MAX_ASSET_BYTES
    else:
        limit = MAX_METADATA_ENTRY_BYTES
    if info.file_size < 0 or info.file_size > limit:
        raise ValidationError(f"数据包条目大小无效：{info.filename}")
    if info.compress_size and info.file_size / info.compress_size > 1000:
        raise ValidationError(f"数据包条目压缩比异常：{info.filename}")


def _read_member(archive: zipfile.ZipFile, path: str, limit: int) -> bytes:
    try:
        info = archive.getinfo(path)
    except KeyError as exc:
        raise ValidationError(f"数据包缺少条目：{path}") from exc
    if info.file_size < 0 or info.file_size > limit:
        raise ValidationError(f"数据包条目大小无效：{path}")
    with archive.open(info, "r") as stream:
        value = stream.read(limit + 1)
        if len(value) > limit or stream.read(1):
            raise ValidationError(f"数据包条目超过大小限制：{path}")
        return value


def _validate_revision_graph(revision_by_id: dict[str, DomainRevision]) -> None:
    """Validate parent links and cycles iteratively so deep histories are safe."""
    for revision in revision_by_id.values():
        missing_parents = [
            parent_id
            for parent_id in revision.parent_revision_ids
            if parent_id not in revision_by_id
        ]
        if missing_parents:
            raise ValidationError(f"revision 缺少父版本：{sorted(missing_parents)}")
        for parent_id in revision.parent_revision_ids:
            parent = revision_by_id[parent_id]
            if (
                parent.entity_id != revision.entity_id
                or parent.entity_kind != revision.entity_kind
            ):
                raise ValidationError(
                    f"revision 父版本必须属于同一实体和类型：{revision.revision_id}"
                )

    visiting = 1
    visited = 2
    states: dict[str, int] = {}
    for start_id in revision_by_id:
        if states.get(start_id) == visited:
            continue
        stack: list[tuple[str, bool]] = [(start_id, False)]
        while stack:
            revision_id, exiting = stack.pop()
            if exiting:
                states[revision_id] = visited
                continue
            state = states.get(revision_id)
            if state == visited:
                continue
            if state == visiting:
                raise ValidationError("revision 图包含循环")
            states[revision_id] = visiting
            stack.append((revision_id, True))
            for parent_id in reversed(revision_by_id[revision_id].parent_revision_ids):
                parent_state = states.get(parent_id)
                if parent_state == visiting:
                    raise ValidationError("revision 图包含循环")
                if parent_state != visited:
                    stack.append((parent_id, False))


def _read_validated(zip_path: Path) -> tuple[dict, list[DomainRevision]]:
    try:
        archive = zipfile.ZipFile(zip_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValidationError("无法读取 Portable Data 数据包") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES or sum(item.file_size for item in infos) > MAX_ARCHIVE_BYTES:
            raise ValidationError("数据包超过文件数或总大小限制")
        if len({item.filename for item in infos}) != len(infos):
            raise ValidationError("数据包包含重复路径")
        for info in infos:
            _safe_member(info)
        names = {info.filename for info in infos}
        if "manifest.json" not in names:
            raise ValidationError("数据包缺少 manifest.json")
        try:
            manifest = json.loads(_read_member(archive, "manifest.json", MAX_MANIFEST_BYTES))
        except (json.JSONDecodeError, KeyError) as exc:
            raise ValidationError("manifest.json 无效") from exc
        if manifest.get("format") != PORTABLE_FORMAT or manifest.get("format_version") != PORTABLE_VERSION:
            raise ValidationError("不支持的 Portable Data 格式版本")
        revisions: list[DomainRevision] = []
        seen_revisions: set[str] = set()
        for path, descriptor in sorted((manifest.get("content") or {}).items()):
            if path not in names or not path.startswith("entities/") or not path.endswith(".jsonl"):
                raise ValidationError(f"manifest 内容路径无效：{path}")
            raw = _read_member(archive, path, MAX_METADATA_ENTRY_BYTES)
            if _sha256_bytes(raw) != descriptor.get("sha256"):
                raise ValidationError(f"内容哈希不一致：{path}")
            lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
            if len(lines) != descriptor.get("count"):
                raise ValidationError(f"内容计数不一致：{path}")
            for line in lines:
                try:
                    revision = validate_revision(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValidationError(f"领域 JSONL 无效：{path}") from exc
                if revision.revision_id in seen_revisions:
                    raise ValidationError("数据包包含重复 revision")
                seen_revisions.add(revision.revision_id)
                revisions.append(revision)
        revision_by_id = {item.revision_id: item for item in revisions}
        for revision in revisions:
            missing_parents = set(revision.parent_revision_ids) - set(revision_by_id)
            if missing_parents:
                raise ValidationError(f"revision 缺少父版本：{sorted(missing_parents)}")
        _validate_revision_graph(revision_by_id)
        heads = manifest.get("entity_heads")
        if not isinstance(heads, dict):
            raise ValidationError("manifest 缺少 entity_heads")
        for entity_id, revision_id in heads.items():
            revision = revision_by_id.get(revision_id)
            if revision is None or revision.entity_id != entity_id:
                raise ValidationError(f"manifest entity head 无效：{entity_id}")
        asset_items = manifest.get("assets")
        if not isinstance(asset_items, list) or any(not isinstance(item, dict) for item in asset_items):
            raise ValidationError("manifest 资产列表无效")
        bindings = _bind_asset_descriptors(revisions, heads, asset_items)
        archive_asset_paths = {
            info.filename
            for info in infos
            if not info.is_dir() and info.filename.startswith("assets/")
        }
        descriptor_paths = {descriptor["path"] for descriptor, _ in bindings}
        if archive_asset_paths != descriptor_paths:
            raise ValidationError("归档中的资产文件必须与 manifest descriptor 精确对应")
        for descriptor, _ in bindings:
            path = descriptor["path"]
            if path not in names or not str(path).startswith("assets/"):
                raise ValidationError(f"资产路径无效：{path}")
            if (
                descriptor["bytes"] < 0
                or descriptor["bytes"] > MAX_ASSET_BYTES
            ):
                raise ValidationError(f"资产大小无效：{path}")
            raw = _read_member(archive, str(path), MAX_ASSET_BYTES)
            if len(raw) != descriptor.get("bytes") or _sha256_bytes(raw) != descriptor.get("sha256"):
                raise ValidationError(f"资产校验失败：{path}")
        _require_asset_references(
            revisions,
            {descriptor["id"] for descriptor, _ in bindings},
        )
        return manifest, revisions


def _head_revisions(manifest: dict, revisions: list[DomainRevision]) -> list[DomainRevision]:
    by_id = {item.revision_id: item for item in revisions}
    return [by_id[revision_id] for _, revision_id in sorted(manifest["entity_heads"].items())]


def _storage_revision(revision: DomainRevision) -> DomainRevision:
    if revision.entity_kind != "asset" or "archive_path" not in revision.payload:
        return revision
    payload = dict(revision.payload)
    payload.pop("archive_path", None)
    value = revision.to_dict()
    value["payload"] = payload
    return validate_revision(value)


def _current_payloads() -> dict[tuple[str, str], dict]:
    init_db()
    with connect() as connection:
        result = {}
        for row in connection.execute(
            """SELECT h.entity_kind,h.entity_id,r.payload_json FROM entity_heads h
               JOIN domain_revisions r ON r.revision_id=h.revision_id"""
        ):
            payload = json.loads(row["payload_json"])
            if row["entity_kind"] == "asset":
                asset = connection.execute(
                    "SELECT sha256,extension FROM managed_assets WHERE id=?", (row["entity_id"],)
                ).fetchone()
                if asset:
                    payload["archive_path"] = f"assets/{asset['sha256']}{asset['extension']}"
            result[(row["entity_kind"], row["entity_id"])] = payload
        return result


def _validate_local_revision_identities(revisions: list[DomainRevision]) -> None:
    """A revision ID is immutable and cannot be reused for different content."""
    init_db()
    with connect() as connection:
        for incoming in revisions:
            row = connection.execute(
                "SELECT * FROM domain_revisions WHERE revision_id=?",
                (incoming.revision_id,),
            ).fetchone()
            if row is None:
                continue
            if _row_revision(row) != _storage_revision(incoming):
                raise ValidationError(
                    f"revision ID 与本机已有不同内容冲突：{incoming.revision_id}"
                )


def preview_import(
    source: str | Path, *, recovery_key: str | None = None, mode: str = "restore"
) -> dict:
    if mode not in {"restore", "merge"}:
        raise ValidationError("导入模式只能是 restore 或 merge")
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise ValidationError(f"导入文件不存在：{path}")
    with _zip_path(path, recovery_key) as plain_zip:
        manifest, revisions = _read_validated(plain_zip)
    _validate_local_revision_identities(revisions)
    heads = _head_revisions(manifest, revisions)
    current = _current_payloads()
    incoming = {(item.entity_kind, item.entity_id): item.payload for item in heads}
    if mode == "restore":
        user_kinds = {"task", "food_item", "daily_record", "checkin_day", "daily_review", "memory", "adjustment"}
        comparison = {key: value for key, value in current.items() if key[0] in user_kinds}
        identical = sorted(key for key, payload in incoming.items() if key in comparison and comparison[key] == payload)
        conflicts = sorted(key for key, payload in incoming.items() if key in comparison and comparison[key] != payload)
        new = sorted(key for key in incoming if key not in comparison)
    else:
        identical = sorted(key for key, payload in incoming.items() if current.get(key) == payload)
        current_kind_by_id = {entity_id: kind for kind, entity_id in current}
        kind_conflicts = {
            key
            for key in incoming
            if key[1] in current_kind_by_id and current_kind_by_id[key[1]] != key[0]
        }
        conflicts = sorted(
            {key for key, payload in incoming.items() if key in current and current[key] != payload}
            | kind_conflicts
        )
        new = sorted(key for key in incoming if key not in current and key not in kind_conflicts)
    if mode == "restore":
        existing_user = [key for key in current if key[0] in user_kinds]
        if existing_user:
            conflicts.append(("restore_target", "not_empty"))
    return {
        "mode": mode,
        "path": str(path),
        "format_version": manifest["format_version"],
        "entity_count": len(heads),
        "revision_count": len(revisions),
        "asset_count": len(manifest.get("assets") or []),
        "new": [{"kind": kind, "id": entity_id} for kind, entity_id in new],
        "identical": [{"kind": kind, "id": entity_id} for kind, entity_id in identical],
        "conflicts": [{"kind": kind, "id": entity_id} for kind, entity_id in conflicts],
        "ready": mode == "merge" or not conflicts,
    }


def _encode_row(row: dict) -> dict:
    encoded = {}
    for key, value in row.items():
        if (key.endswith("_json") or key in {"before_json", "after_json"}) and value is not None and not isinstance(value, str):
            encoded[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            encoded[key] = value
    return encoded


def _insert_row(connection: sqlite3.Connection, table: str, row: dict) -> None:
    allowed = {item[1] for item in connection.execute(f'PRAGMA table_info("{table}")')}
    if not row or not set(row).issubset(allowed):
        raise ValidationError(f"{table} 包含未知字段：{sorted(set(row) - allowed)}")
    encoded = _encode_row(row)
    columns = list(encoded)
    sql = f'INSERT INTO "{table}"({",".join(columns)}) VALUES({",".join("?" for _ in columns)})'
    connection.execute(sql, tuple(encoded[column] for column in columns))


def _upsert_row(connection: sqlite3.Connection, table: str, row: dict, key: str = "id") -> None:
    allowed = {item[1] for item in connection.execute(f'PRAGMA table_info("{table}")')}
    if not row or key not in row or not set(row).issubset(allowed):
        raise ValidationError(f"{table} 包含未知字段或缺少 {key}")
    encoded = _encode_row(row)
    columns = list(encoded)
    updates = [column for column in columns if column != key]
    sql = (
        f'INSERT INTO "{table}"({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) '
        f'ON CONFLICT({key}) DO UPDATE SET '
        + ",".join(f'{column}=excluded.{column}' for column in updates)
    )
    connection.execute(sql, tuple(encoded[column] for column in columns))


def _asset_paths(
    archive: zipfile.ZipFile,
    manifest: dict,
    revisions: list[DomainRevision],
) -> dict[str, str]:
    result: dict[str, str] = {}
    bindings = _bind_asset_descriptors(revisions, manifest["entity_heads"], manifest["assets"])
    home = app_home().resolve()
    root = managed_asset_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.relative_to(home)
    except ValueError as exc:
        raise ValidationError("受管资产目录逃逸 MealCircuit 私人目录") from exc
    init_db()
    with connect() as connection:
        for descriptor, revision in bindings:
            payload = revision.payload
            local_by_id = connection.execute(
                "SELECT id,sha256,media_type,extension,byte_count FROM managed_assets WHERE id=?",
                (revision.entity_id,),
            ).fetchone()
            if local_by_id is not None and (
                local_by_id["sha256"] != payload["sha256"]
                or local_by_id["media_type"] != payload["media_type"]
                or local_by_id["extension"] != payload["extension"]
                or local_by_id["byte_count"] != payload["byte_count"]
            ):
                raise ValidationError(f"本机资产 {revision.entity_id} 的元数据与导入包不一致")
            local_by_digest = connection.execute(
                "SELECT id FROM managed_assets WHERE sha256=?", (payload["sha256"],)
            ).fetchone()
            if local_by_digest is not None and local_by_digest["id"] != revision.entity_id:
                raise ValidationError(f"导入资产 {revision.entity_id} 的哈希已属于另一项本机资产")
            local_head = connection.execute(
                """SELECT r.payload_json FROM entity_heads h
                   JOIN domain_revisions r ON r.revision_id=h.revision_id
                   WHERE h.entity_id=? AND r.entity_kind='asset'""",
                (revision.entity_id,),
            ).fetchone()
            if local_head is not None:
                local_payload = json.loads(local_head["payload_json"])
                if any(
                    local_payload.get(field) != payload[field]
                    for field in ("sha256", "media_type", "extension", "byte_count")
                ):
                    raise ValidationError(
                        f"本机资产 {revision.entity_id} 的活动修订与导入包元数据不一致"
                    )

    for descriptor, revision in bindings:
        payload = revision.payload
        archive_path = descriptor["path"]
        digest = descriptor["sha256"]
        extension = payload["extension"]
        data = _read_member(archive, archive_path, MAX_ASSET_BYTES)
        if len(data) != descriptor["bytes"] or _sha256_bytes(data) != digest:
            raise ValidationError(f"资产哈希不一致：{archive_path}")
        expected_name = f"{digest}{extension}"
        target = (root / expected_name).resolve()
        if target.parent != root or target.name != expected_name:
            raise ValidationError("资产路径逃逸受管目录")
        if target.exists() and (
            not target.is_file()
            or target.stat().st_size != descriptor["bytes"]
            or _sha256_path(target) != digest
        ):
            raise ValidationError(f"本机资产冲突：{target}")
        if not target.exists():
            fd, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".part", dir=root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if temporary.stat().st_size != descriptor["bytes"] or _sha256_path(temporary) != digest:
                    raise ValidationError(f"导入资产校验失败：{revision.entity_id}")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        relative = target.relative_to(app_home()).as_posix()
        result[revision.entity_id] = relative
    return result


def _materialize_goal_contract_projection(connection: sqlite3.Connection, projection: dict) -> None:
    rows = projection.get("versioned_rows")
    if not isinstance(rows, dict) or not isinstance(rows.get("profile"), dict):
        return
    profile = dict(rows["profile"])
    goals = [dict(item) for item in rows.get("goals") or [] if isinstance(item, dict)]
    strategy = dict(rows["strategy"]) if isinstance(rows.get("strategy"), dict) else None
    targets = [dict(item) for item in rows.get("targets") or [] if isinstance(item, dict)]
    connection.execute("UPDATE profile_versions SET active=0 WHERE active=1")
    connection.execute("UPDATE goal_versions SET active=0 WHERE active=1")
    connection.execute("UPDATE strategy_versions SET active=0 WHERE active=1")
    connection.execute("UPDATE nutrition_target_versions SET active=0 WHERE active=1")
    if connection.execute("SELECT 1 FROM profile_versions WHERE id=?", (profile["id"],)).fetchone() is None:
        profile["version"] = int(connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM profile_versions"
        ).fetchone()[0])
    profile["active"] = 1
    _upsert_row(connection, "profile_versions", profile)
    for goal in goals:
        if connection.execute("SELECT 1 FROM goal_versions WHERE id=?", (goal["id"],)).fetchone() is None:
            goal["version"] = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM goal_versions WHERE goal_key=?",
                (goal["goal_key"],),
            ).fetchone()[0])
        goal["profile_version_id"] = profile["id"]
        goal["active"] = 1
        _upsert_row(connection, "goal_versions", goal)
    if strategy:
        if connection.execute("SELECT 1 FROM strategy_versions WHERE id=?", (strategy["id"],)).fetchone() is None:
            strategy["version"] = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM strategy_versions"
            ).fetchone()[0])
        strategy["profile_version_id"] = profile["id"]
        strategy["active"] = 1
        _upsert_row(connection, "strategy_versions", strategy)
    for target in targets:
        if connection.execute("SELECT 1 FROM nutrition_target_versions WHERE id=?", (target["id"],)).fetchone() is None:
            target["version"] = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM nutrition_target_versions WHERE target_key=?",
                (target["target_key"],),
            ).fetchone()[0])
        target["profile_version_id"] = profile["id"]
        target["active"] = 1
        _upsert_row(connection, "nutrition_target_versions", target)


def _materialize_meal_episode_projection(connection: sqlite3.Connection, projection: dict) -> None:
    for item in projection.get("episodes") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        _upsert_row(connection, "meal_episode_projections", item)


def _apply_revision(connection: sqlite3.Connection, revision: DomainRevision, asset_paths: dict[str, str]) -> None:
    payload = revision.payload
    if revision.entity_kind == "task":
        task = dict(payload["task"])
        task.pop("external_reference", None)
        task.pop("unresolved", None)
        task["image_path"] = asset_paths.get(task.pop("asset_id", ""), None)
        _upsert_row(connection, "tasks", task)
        for item in payload.get("input_history", []):
            _insert_row(connection, "task_input_history", item)
        for item in payload.get("corrections", []):
            _insert_row(connection, "task_corrections", item)
    elif revision.entity_kind == "task_input":
        image_path = asset_paths.get(payload.get("asset_id", ""))
        connection.execute(
            """UPDATE tasks SET original_input=?,input_version=?,image_path=? WHERE id=?""",
            (
                payload["original_input"],
                payload["input_version"],
                image_path,
                payload["task_id"],
            ),
        )
        connection.execute("DELETE FROM task_input_history WHERE task_id=?", (payload["task_id"],))
        for item in payload.get("input_history", []):
            _insert_row(connection, "task_input_history", item)
    elif revision.entity_kind == "correction":
        connection.execute("DELETE FROM task_corrections WHERE id=?", (revision.entity_id,))
        _insert_row(connection, "task_corrections", payload)
    elif revision.entity_kind == "food_item":
        food = dict(payload["food"])
        asset_id = food.pop("package_photo_asset_id", None)
        food["package_photo_path"] = asset_paths.get(asset_id or "")
        food.pop("package_photo_external_reference", None)
        food.pop("package_photo_unresolved", None)
        _insert_row(connection, "food_items", food)
        for item in payload.get("history", []):
            _insert_row(connection, "food_item_history", item)
    elif revision.entity_kind == "daily_record":
        _insert_row(connection, "daily_records", payload)
    elif revision.entity_kind == "checkin_day":
        _insert_row(connection, "daily_checkins", payload["checkin"])
        for item in payload.get("modules", []):
            _insert_row(connection, "daily_checkin_modules", item["module"])
            for history in item.get("history", []):
                _insert_row(connection, "daily_checkin_module_history", history)
    elif revision.entity_kind == "daily_review":
        _insert_row(connection, "daily_reviews", payload["review"])
        for item in payload.get("history", []):
            _insert_row(connection, "daily_review_history", item)
    elif revision.entity_kind == "memory":
        _insert_row(connection, "memories", payload)
    elif revision.entity_kind == "adjustment":
        _insert_row(connection, "adjustments", payload)
    elif revision.entity_kind == "preferences" and payload.get("kind") == "checkin_settings":
        try:
            items = json.loads(str(payload.get("content", "[]")))
        except json.JSONDecodeError as exc:
            raise ValidationError("导入的状态模块设置不是合法 JSON") from exc
        if not isinstance(items, list):
            raise ValidationError("导入的状态模块设置必须是数组")
        for item in items:
            if not isinstance(item, dict) or "module_key" not in item:
                raise ValidationError("导入的状态模块设置条目无效")
            connection.execute(
                """UPDATE checkin_module_settings SET enabled=?,sort_order=?,frequency=?,updated_at=?
                   WHERE module_key=?""",
                (item["enabled"], item["sort_order"], item["frequency"], item["updated_at"], item["module_key"]),
            )
    elif revision.entity_kind == "preferences" and payload.get("kind") in {
        "agent_user_model", "goal_contract", "meal_episode_projection",
    }:
        kind = str(payload.get("kind"))
        content = str(payload.get("content", ""))
        try:
            projection = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValidationError("导入的 Agent 投影不是合法 JSON") from exc
        required_list = {
            "agent_user_model": "claims",
            "goal_contract": "goals",
            "meal_episode_projection": "episodes",
        }[kind]
        if not isinstance(projection, dict) or not isinstance(projection.get(required_list), list):
            raise ValidationError("导入的 Agent 投影结构无效")
        connection.execute(
            """INSERT INTO config_documents(kind,content,content_sha256,revision_id,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(kind) DO UPDATE SET content=excluded.content,
                   content_sha256=excluded.content_sha256,revision_id=excluded.revision_id,
                   updated_at=excluded.updated_at""",
            (kind, content, _sha256_bytes(content.encode("utf-8")), revision.revision_id, revision.created_at),
        )
        if kind == "goal_contract":
            _materialize_goal_contract_projection(connection, projection)
        elif kind == "meal_episode_projection":
            _materialize_meal_episode_projection(connection, projection)
    elif revision.entity_kind == "asset":
        path = asset_paths[revision.entity_id]
        connection.execute(
            """INSERT OR IGNORE INTO managed_assets(
                   id,sha256,media_type,extension,byte_count,relative_path,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                revision.entity_id,
                payload["sha256"],
                payload["media_type"],
                payload["extension"],
                payload["byte_count"],
                path,
                revision.created_at,
            ),
        )
    connection.execute(
        """INSERT OR IGNORE INTO domain_revisions(
               revision_id,entity_id,entity_kind,parent_revision_ids_json,payload_json,
               schema_version,author_device_id,deleted,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            revision.revision_id,
            revision.entity_id,
            revision.entity_kind,
            json.dumps(list(revision.parent_revision_ids), ensure_ascii=False),
            json.dumps(revision.payload, ensure_ascii=False, sort_keys=True),
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
           conflicted=excluded.conflicted,updated_at=excluded.updated_at""",
        (revision.entity_id, revision.entity_kind, revision.revision_id, 0, revision.created_at),
    )


def _write_preferences(revisions: list[DomainRevision]) -> None:
    destinations = {
        "profile": profile_path(),
        "settings": settings_path(),
        "doctrine": private_doctrine_path(),
    }
    for revision in revisions:
        if revision.entity_kind != "preferences":
            continue
        kind = revision.payload.get("kind")
        content = revision.payload.get("content")
        if kind == "settings":
            try:
                load_value = json.loads(str(content))
            except json.JSONDecodeError as exc:
                raise ValidationError("导入的 settings 不是合法 JSON") from exc
            from .configuration import validate_settings

            validate_settings(load_value)
        destination = destinations.get(str(kind))
        if destination is None:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(str(content), encoding="utf-8")
        os.replace(temporary, destination)


def _store_revision_only(connection: sqlite3.Connection, revision: DomainRevision) -> None:
    revision = _storage_revision(revision)
    existing = connection.execute(
        "SELECT * FROM domain_revisions WHERE revision_id=?", (revision.revision_id,)
    ).fetchone()
    if existing is not None:
        if _row_revision(existing) != revision:
            raise ValidationError(
                f"revision ID 与本机已有不同内容冲突：{revision.revision_id}"
            )
        return
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
            json.dumps(revision.payload, ensure_ascii=False, sort_keys=True),
            revision.schema_version,
            revision.author_device_id,
            int(revision.deleted),
            revision.created_at,
        ),
    )


def _row_revision(row: sqlite3.Row) -> DomainRevision:
    return validate_revision(
        {
            "schema_version": row["schema_version"],
            "entity_id": row["entity_id"],
            "entity_kind": row["entity_kind"],
            "revision_id": row["revision_id"],
            "parent_revision_ids": json.loads(row["parent_revision_ids_json"]),
            "created_at": row["created_at"],
            "author_device_id": row["author_device_id"],
            "deleted": bool(row["deleted"]),
            "payload": json.loads(row["payload_json"]),
        }
    )


def _ancestor_distances(revisions: dict[str, DomainRevision], start: str) -> dict[str, int]:
    distances = {start: 0}
    queue = [start]
    while queue:
        current = queue.pop(0)
        revision = revisions.get(current)
        if revision is None:
            continue
        for parent in revision.parent_revision_ids:
            if parent not in distances:
                distances[parent] = distances[current] + 1
                queue.append(parent)
    return distances


def _common_ancestor(
    revisions: dict[str, DomainRevision], local: DomainRevision, remote: DomainRevision
) -> DomainRevision | None:
    local_distances = _ancestor_distances(revisions, local.revision_id)
    remote_distances = _ancestor_distances(revisions, remote.revision_id)
    common = set(local_distances) & set(remote_distances)
    if not common:
        return None
    best = min(common, key=lambda item: (local_distances[item] + remote_distances[item], item))
    return revisions.get(best)


def _record_import_conflict(
    connection: sqlite3.Connection,
    base: DomainRevision | None,
    local: DomainRevision,
    remote: DomainRevision,
    paths: list[str],
) -> None:
    connection.execute(
        """INSERT INTO sync_conflicts(
               id,entity_id,entity_kind,base_revision_json,local_revision_json,
               remote_revision_json,conflicting_paths_json,status,created_at
           ) VALUES(?,?,?,?,?,?,?,'unresolved',?)""",
        (
            new_id("conflict"),
            local.entity_id,
            local.entity_kind,
            json.dumps(base.to_dict(), ensure_ascii=False, sort_keys=True) if base else None,
            json.dumps(local.to_dict(), ensure_ascii=False, sort_keys=True),
            json.dumps(remote.to_dict(), ensure_ascii=False, sort_keys=True),
            json.dumps(sorted(set(paths)), ensure_ascii=False),
            utc_now(),
        ),
    )
    connection.execute(
        "UPDATE entity_heads SET conflicted=1 WHERE entity_id=?", (local.entity_id,)
    )


def apply_import(
    source: str | Path, *, recovery_key: str | None = None, mode: str = "restore"
) -> dict:
    with _exclusive_import_session():
        archive = Path(source).expanduser().resolve()
        with _snapshot_import_source(archive) as snapshot:
            preview = preview_import(snapshot, recovery_key=recovery_key, mode=mode)
            if not preview["ready"]:
                raise ValidationError(f"导入存在冲突：{preview['conflicts']}")
            with _home_import_transaction():
                result = _apply_import_unprotected(snapshot, recovery_key=recovery_key, mode=mode)
            return result


def _apply_import_unprotected(
    source: str | Path, *, recovery_key: str | None = None, mode: str = "restore"
) -> dict:
    preview = preview_import(source, recovery_key=recovery_key, mode=mode)
    if not preview["ready"]:
        raise ValidationError(f"导入存在冲突：{preview['conflicts']}")
    path = Path(source).expanduser().resolve()
    incoming_keys = {(item["kind"], item["id"]) for item in preview["new"]}
    with _zip_path(path, recovery_key) as plain_zip:
        manifest, revisions = _read_validated(plain_zip)
        heads = [_storage_revision(item) for item in _head_revisions(manifest, revisions)]
        with zipfile.ZipFile(plain_zip, "r") as archive:
            asset_paths = _asset_paths(archive, manifest, revisions)
        init_db()
        applied_preferences: list[DomainRevision] = []
        merge_conflicts = 0
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                stored_revisions = [_storage_revision(item) for item in revisions]
                for revision in stored_revisions:
                    _store_revision_only(connection, revision)
                graph = {
                    row["revision_id"]: _row_revision(row)
                    for row in connection.execute("SELECT * FROM domain_revisions")
                }
                priority = {"asset": 0, "task": 1, "task_input": 2, "correction": 3}
                ordered_heads = sorted(
                    heads,
                    key=lambda item: (priority.get(item.entity_kind, 10), item.entity_kind, item.entity_id),
                )
                from .domain_store import enqueue_revision, materialize_revision

                def apply_materialized(revision: DomainRevision) -> None:
                    if revision.entity_kind == "asset":
                        _apply_revision(connection, revision, asset_paths)
                    else:
                        materialize_revision(connection, revision)

                for remote in ordered_heads:
                    key = (remote.entity_kind, remote.entity_id)
                    head_row = connection.execute(
                        "SELECT entity_kind,revision_id FROM entity_heads WHERE entity_id=?",
                        (remote.entity_id,),
                    ).fetchone()
                    if head_row is not None and head_row["entity_kind"] != remote.entity_kind:
                        local = graph[head_row["revision_id"]]
                        _record_import_conflict(
                            connection, None, local, remote, ["$entity_kind"]
                        )
                        merge_conflicts += 1
                        continue
                    if key in incoming_keys or head_row is None:
                        apply_materialized(remote)
                        enqueue_revision(connection, remote)
                        if remote.entity_kind == "preferences":
                            applied_preferences.append(remote)
                        continue
                    local = graph[head_row["revision_id"]]
                    if local.payload == remote.payload and local.deleted == remote.deleted:
                        continue
                    local_ancestors = _ancestor_distances(graph, local.revision_id)
                    remote_ancestors = _ancestor_distances(graph, remote.revision_id)
                    if local.revision_id in remote_ancestors:
                        apply_materialized(remote)
                        enqueue_revision(connection, remote)
                        if remote.entity_kind == "preferences":
                            applied_preferences.append(remote)
                        continue
                    if remote.revision_id in local_ancestors:
                        continue
                    base = _common_ancestor(graph, local, remote)
                    if base is None:
                        _record_import_conflict(connection, None, local, remote, ["$"])
                        merge_conflicts += 1
                        continue
                    merged_payload, paths = three_way_merge(base.payload, local.payload, remote.payload)
                    local_edited = local.payload != base.payload
                    remote_edited = remote.payload != base.payload
                    if local.deleted != remote.deleted and (
                        (local.deleted != base.deleted and remote_edited)
                        or (remote.deleted != base.deleted and local_edited)
                    ):
                        paths.append("$deleted")
                    if paths:
                        _record_import_conflict(connection, base, local, remote, paths)
                        merge_conflicts += 1
                        continue
                    if local.deleted == remote.deleted:
                        deleted = local.deleted
                    elif local.deleted == base.deleted:
                        deleted = remote.deleted
                    else:
                        deleted = local.deleted
                    device_id = _metadata(connection, "device_id")
                    merged = make_revision(
                        local.entity_kind,
                        merged_payload,
                        entity_id=local.entity_id,
                        parent_revision_ids=[local.revision_id, remote.revision_id],
                        author_device_id=device_id,
                        deleted=deleted,
                    )
                    apply_materialized(merged)
                    enqueue_revision(connection, merged)
                    if merged.entity_kind == "preferences":
                        applied_preferences.append(merged)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        _write_preferences(applied_preferences)
    actual = _current_payloads()
    expected = {
        (revision.entity_kind, revision.entity_id): (
            {**revision.payload, "archive_path": next(
                (
                    item.payload["archive_path"]
                    for item in _head_revisions(manifest, revisions)
                    if item.revision_id == revision.revision_id and item.entity_kind == "asset"
                ),
                revision.payload.get("archive_path"),
            )} if revision.entity_kind == "asset" else revision.payload
        )
        for revision in heads
    }
    conflict_keys = {
        (item["kind"], item["id"]) for item in preview["conflicts"]
    }
    missing = sorted(
        key for key, payload in expected.items()
        if key not in conflict_keys and actual.get(key) != payload
    )
    if missing:
        raise ValidationError(f"导入后 round-trip 验证失败：{missing}")
    return {
        "status": "imported",
        "mode": mode,
        "path": str(path),
        "imported": len(incoming_keys),
        "conflicts": merge_conflicts,
        "asset_count": preview["asset_count"],
        "round_trip": "ok",
    }
