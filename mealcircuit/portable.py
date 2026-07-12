from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import struct
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterator

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
    validate_revision,
)
from .storage import (
    app_home,
    db_path,
    managed_asset_root,
    private_doctrine_path,
    profile_path,
    resolve_data_path,
    settings_path,
)
from .validation import ValidationError


PORTABLE_FORMAT = "mealcircuit.portable"
PORTABLE_VERSION = 1
ENCRYPTED_MAGIC = b"MCX1\n"
CHUNK_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_FILES = 100_000
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_METADATA_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_MCX_HEADER_BYTES = 64 * 1024
UUID_NAMESPACE = uuid.UUID("2a7c0c93-763f-4d3c-93f1-c8a5768da92a")
_IMPORT_LOCK = threading.RLock()
_IMPORT_ACTIVE = False


class _ImportTransaction:
    """Build an import in a sibling directory, then atomically promote it."""

    ENVIRONMENT_KEYS = ("MEALCIRCUIT_HOME", "MEALCIRCUIT_DB", "DIETOS_DB", "MEALCIRCUIT_DOCTRINE")

    def __init__(self) -> None:
        self.home = app_home().resolve()
        self.home_existed = self.home.exists()
        database = db_path().resolve()
        doctrine = private_doctrine_path().resolve()
        try:
            self.database_relative = database.relative_to(self.home)
            self.doctrine_relative = doctrine.relative_to(self.home)
        except ValueError as exc:
            raise ValidationError(
                "原子导入要求数据库与私人 doctrine 位于 MEALCIRCUIT_HOME 内；请先迁回统一数据目录"
            ) from exc
        identity = hashlib.sha256(str(self.home).encode("utf-8")).hexdigest()[:16]
        self.journal = self.home.parent / f".mealcircuit-import-rollback-{identity}"
        if self.journal.exists():
            raise RuntimeError(f"存在未恢复的导入事务：{self.journal}")
        self.journal.mkdir(parents=True, mode=0o700)
        self.staging = self.home.parent / f".mealcircuit-import-staging-{identity}-{uuid.uuid4().hex}"
        self.backup = self.journal / "previous-home"
        self.state = "preparing"
        self._write_manifest()
        if self.home_existed:
            if any(path.is_symlink() for path in self.home.rglob("*")):
                self.close()
                raise ValidationError("MEALCIRCUIT_HOME 含符号链接，无法保证原子导入边界")
            shutil.copytree(self.home, self.staging, copy_function=shutil.copy2)
        else:
            self.staging.mkdir(parents=True, mode=0o700)
        self.state = "prepared"
        self._write_manifest()

    def _write_manifest(self) -> None:
        value = {
            "version": 2,
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
    def from_manifest(cls, journal: Path, value: dict) -> "_ImportTransaction":
        if value.get("version") != 2:
            raise RuntimeError("导入回滚日志版本不受支持")
        item = cls.__new__(cls)
        item.journal = journal
        item.state = str(value["state"])
        item.home = Path(value["home"]).resolve()
        item.home_existed = bool(value["home_existed"])
        item.staging = Path(value["staging"]).resolve()
        item.backup = Path(value["backup"]).resolve()
        item.database_relative = Path(value["database_relative"])
        item.doctrine_relative = Path(value["doctrine_relative"])
        identity = hashlib.sha256(str(item.home).encode("utf-8")).hexdigest()[:16]
        expected_journal = item.home.parent / f".mealcircuit-import-rollback-{identity}"
        if journal.resolve() != expected_journal.resolve():
            raise RuntimeError("导入事务日志身份不匹配")
        if item.backup != (journal / "previous-home").resolve():
            raise RuntimeError("导入事务备份路径逃逸")
        if item.staging.parent != item.home.parent or not item.staging.name.startswith(
            f".mealcircuit-import-staging-{identity}-"
        ):
            raise RuntimeError("导入事务 staging 路径逃逸")
        for relative in (item.database_relative, item.doctrine_relative):
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("导入事务相对路径逃逸")
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
        if self.state != "prepared" or not self.staging.is_dir():
            raise RuntimeError("导入 staging 未准备完成")
        if self.home_existed:
            os.replace(self.home, self.backup)
        self.state = "original_moved"
        self._write_manifest()
        os.replace(self.staging, self.home)
        self.state = "staging_promoted"
        self._write_manifest()
        shutil.rmtree(self.backup, ignore_errors=True)
        self.close()

    def restore(self) -> None:
        if self.backup.exists():
            if self.home.exists():
                shutil.rmtree(self.home)
            os.replace(self.backup, self.home)
        elif not self.home_existed and self.state != "staging_promoted":
            shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.staging, ignore_errors=True)

    def close(self) -> None:
        shutil.rmtree(self.journal, ignore_errors=True)


def _journal_root() -> Path:
    home = app_home()
    identity = hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:16]
    return home.parent / f".mealcircuit-import-rollback-{identity}"


def recover_interrupted_import() -> bool:
    """Restore a crash-interrupted import before opening the default database."""
    if _IMPORT_ACTIVE:
        return False
    with _IMPORT_LOCK:
        journal = _journal_root()
        if not journal.exists():
            return False
        manifest_path = journal / "manifest.json"
        if not manifest_path.is_file():
            shutil.rmtree(journal, ignore_errors=True)
            return False
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            transaction = _ImportTransaction.from_manifest(journal, value)
            if transaction.state == "staging_promoted":
                shutil.rmtree(transaction.backup, ignore_errors=True)
                shutil.rmtree(transaction.staging, ignore_errors=True)
            else:
                transaction.restore()
            transaction.close()
        except Exception as exc:
            raise RuntimeError(f"无法恢复中断的 Portable Data 导入：{journal}") from exc
        return True


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
        path = resolve_data_path(reference)
        if not path.is_file():
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
                path = resolve_data_path(asset["relative_path"])
                if not path.is_file() or _sha256_path(path) != asset["sha256"]:
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


def _build_zip(target: Path) -> dict:
    revisions, assets = collect_revisions()
    grouped: dict[str, list[DomainRevision]] = {}
    for revision in revisions:
        grouped.setdefault(revision.entity_kind, []).append(revision)
    contents: dict[str, bytes] = {
        f"entities/{kind}.jsonl": _jsonl(items) for kind, items in sorted(grouped.items())
    }
    manifest = {
        "format": PORTABLE_FORMAT,
        "format_version": PORTABLE_VERSION,
        "domain_schema_version": DOMAIN_SCHEMA_VERSION,
        "application_version": __version__,
        "created_at": utc_now(),
        "entity_heads": _current_heads(),
        "content": {
            path: {"count": len(grouped[path.removeprefix("entities/").removesuffix(".jsonl")]), "sha256": _sha256_bytes(data)}
            for path, data in sorted(contents.items())
        },
        "assets": [
            {"path": path, "bytes": source.stat().st_size, "sha256": _sha256_path(source)}
            for path, source in sorted(assets.items())
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for path, data in contents.items():
            archive.writestr(path, data)
        for path, source in assets.items():
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
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(revision_id: str) -> None:
            if revision_id in visited:
                return
            if revision_id in visiting:
                raise ValidationError("revision 图包含循环")
            visiting.add(revision_id)
            for parent in revision_by_id[revision_id].parent_revision_ids:
                visit(parent)
            visiting.remove(revision_id)
            visited.add(revision_id)

        for revision_id in revision_by_id:
            visit(revision_id)
        heads = manifest.get("entity_heads")
        if not isinstance(heads, dict):
            raise ValidationError("manifest 缺少 entity_heads")
        for entity_id, revision_id in heads.items():
            revision = revision_by_id.get(revision_id)
            if revision is None or revision.entity_id != entity_id:
                raise ValidationError(f"manifest entity head 无效：{entity_id}")
        asset_items = manifest.get("assets") or []
        if not isinstance(asset_items, list) or any(not isinstance(item, dict) for item in asset_items):
            raise ValidationError("manifest 资产列表无效")
        asset_paths = [item.get("path") for item in asset_items]
        asset_ids = [item.get("id") for item in asset_items if item.get("id") is not None]
        if any(not isinstance(value, str) or not value for value in asset_paths + asset_ids):
            raise ValidationError("manifest 资产标识无效")
        if len(set(asset_paths)) != len(asset_paths) or len(set(asset_ids)) != len(asset_ids):
            raise ValidationError("manifest 包含重复资产")
        assets = {item.get("path"): item for item in asset_items}
        for path, descriptor in assets.items():
            if path not in names or not str(path).startswith("assets/"):
                raise ValidationError(f"资产路径无效：{path}")
            if (
                not isinstance(descriptor.get("bytes"), int)
                or descriptor["bytes"] < 0
                or descriptor["bytes"] > MAX_ASSET_BYTES
            ):
                raise ValidationError(f"资产大小无效：{path}")
            raw = _read_member(archive, str(path), MAX_ASSET_BYTES)
            if len(raw) != descriptor.get("bytes") or _sha256_bytes(raw) != descriptor.get("sha256"):
                raise ValidationError(f"资产校验失败：{path}")
        available_assets = {
            revision.entity_id
            for revision in revisions
            if revision.entity_kind == "asset"
        }
        def asset_references(value: object) -> Iterator[str]:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.endswith("asset_id") and isinstance(child, str):
                        yield child
                    yield from asset_references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from asset_references(child)

        for revision in revisions:
            missing_assets = sorted(set(asset_references(revision.payload)) - available_assets)
            if missing_assets:
                raise ValidationError(f"领域实体引用了缺失资产：{missing_assets}")
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
        conflicts = sorted(key for key, payload in incoming.items() if key in current and current[key] != payload)
        new = sorted(key for key in incoming if key not in current)
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


def _asset_paths(archive: zipfile.ZipFile, revisions: list[DomainRevision]) -> dict[str, str]:
    result: dict[str, str] = {}
    root = managed_asset_root()
    root.mkdir(parents=True, exist_ok=True)
    for revision in revisions:
        if revision.entity_kind != "asset":
            continue
        payload = revision.payload
        archive_path = payload.get("archive_path")
        digest = payload.get("sha256")
        extension = payload.get("extension")
        if not isinstance(archive_path, str) or not isinstance(digest, str) or not isinstance(extension, str):
            raise ValidationError("资产 revision 字段无效")
        data = _read_member(archive, archive_path, MAX_ASSET_BYTES)
        if _sha256_bytes(data) != digest:
            raise ValidationError(f"资产哈希不一致：{archive_path}")
        target = root / f"{digest}{extension}"
        if target.exists() and _sha256_path(target) != digest:
            raise ValidationError(f"本机资产冲突：{target}")
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, target)
        relative = target.relative_to(app_home()).as_posix()
        result[revision.entity_id] = relative
    return result


def _apply_revision(connection: sqlite3.Connection, revision: DomainRevision, asset_paths: dict[str, str]) -> None:
    payload = revision.payload
    if revision.entity_kind == "task":
        task = dict(payload["task"])
        if "asset_id" in task or "external_reference" in task:
            task["image_path"] = asset_paths.get(task.pop("asset_id", ""))
            if not task["image_path"]:
                task["image_path"] = task.pop("external_reference", None)
                task.pop("unresolved", None)
        _upsert_row(connection, "tasks", task)
        for item in payload.get("input_history", []):
            _insert_row(connection, "task_input_history", item)
        for item in payload.get("corrections", []):
            _insert_row(connection, "task_corrections", item)
    elif revision.entity_kind == "task_input":
        image_path = asset_paths.get(payload.get("asset_id", ""))
        if not image_path:
            image_path = payload.get("external_reference")
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
        if not food["package_photo_path"]:
            food["package_photo_path"] = food.pop("package_photo_external_reference", None)
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
    global _IMPORT_ACTIVE
    with _IMPORT_LOCK:
        recover_interrupted_import()
        _IMPORT_ACTIVE = True
        transaction: _ImportTransaction | None = None
        try:
            archive = Path(source).expanduser().resolve()
            preview = preview_import(archive, recovery_key=recovery_key, mode=mode)
            if not preview["ready"]:
                raise ValidationError(f"导入存在冲突：{preview['conflicts']}")
            transaction = _ImportTransaction()
            with transaction.activated():
                result = _apply_import_unprotected(archive, recovery_key=recovery_key, mode=mode)
            transaction.commit()
            return result
        except BaseException:
            if transaction is not None:
                transaction.restore()
                transaction.close()
            raise
        finally:
            _IMPORT_ACTIVE = False


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
            asset_paths = _asset_paths(archive, revisions)
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
                        "SELECT revision_id FROM entity_heads WHERE entity_id=?", (remote.entity_id,)
                    ).fetchone()
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
