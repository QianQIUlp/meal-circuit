from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .crypto import decrypt, derive_key, encrypt, format_recovery_key, opaque_remote_id, parse_recovery_key, random_key
from .db import connect, init_db
from .domain import DomainRevision, make_revision, new_id, three_way_merge, utc_now, validate_revision
from .secret_store import delete_secret, get_secret, set_secret
from .storage import app_home, managed_asset_root, resolve_data_path
from .validation import ValidationError


SYNC_PROTOCOL_VERSION = 1
BLOB_CHUNK_BYTES = 4 * 1024 * 1024


def _validate_capabilities(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("同步服务 capabilities 响应无效")
    if value.get("protocol") != "mealcircuit.sync" or value.get("e2ee_required") is not True:
        raise ValidationError("同步服务不兼容或未强制端到端加密")
    try:
        minimum = int(value.get("min_version"))
        maximum = int(value.get("max_version"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("同步服务协议版本无效") from exc
    if not minimum <= SYNC_PROTOCOL_VERSION <= maximum:
        raise ValidationError("同步服务协议版本与本客户端不兼容")
    return value


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object, name: str) -> bytes:
    if not isinstance(value, str):
        raise ValidationError(f"{name} 必须是 base64 文本")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValidationError(f"{name} 不是合法 base64") from exc


@dataclass(frozen=True)
class AccountCipher:
    account_id: str
    account_data_key: bytes
    key_version: int = 1

    def __post_init__(self) -> None:
        if len(self.account_data_key) != 32:
            raise ValidationError("Account Data Key 必须是 32 字节")
        if self.key_version <= 0:
            raise ValidationError("key_version 必须是正整数")

    @property
    def _salt(self) -> bytes:
        return hashlib.sha256(self.account_id.encode("utf-8")).digest()

    @property
    def content_key(self) -> bytes:
        return derive_key(self.account_data_key, salt=self._salt, info=b"mealcircuit-content-v1")

    @property
    def index_key(self) -> bytes:
        return derive_key(self.account_data_key, salt=self._salt, info=b"mealcircuit-index-v1")

    @property
    def asset_key(self) -> bytes:
        return derive_key(self.account_data_key, salt=self._salt, info=b"mealcircuit-assets-v1")

    def remote_id(self, revision: DomainRevision) -> str:
        return opaque_remote_id(self.index_key, revision.entity_kind, revision.entity_id)

    def blob_id(self, asset_id: str) -> str:
        return opaque_remote_id(self.index_key, "asset_blob", asset_id)

    def _blob_aad(self, blob_id: str, index: int, chunk_count: int) -> bytes:
        return (
            f"MealCircuit Blob v1\0{self.account_id}\0{blob_id}\0{self.key_version}\0{index}\0{chunk_count}"
        ).encode("utf-8")

    def seal_blob_chunk(self, blob_id: str, index: int, chunk_count: int, plaintext: bytes) -> bytes:
        nonce, ciphertext = encrypt(
            self.asset_key, plaintext, self._blob_aad(blob_id, index, chunk_count)
        )
        return nonce + ciphertext

    def open_blob_chunk(self, blob_id: str, index: int, chunk_count: int, value: bytes) -> bytes:
        if len(value) < 28:
            raise ValidationError("加密资产分块被截断")
        return decrypt(
            self.asset_key,
            value[:12],
            value[12:],
            self._blob_aad(blob_id, index, chunk_count),
        )

    def _aad(self, remote_id: str) -> bytes:
        return (
            f"MealCircuit Sync v{SYNC_PROTOCOL_VERSION}\0{self.account_id}\0{remote_id}\0{self.key_version}"
        ).encode("utf-8")

    def seal(self, revision: DomainRevision) -> dict:
        return self._seal(revision, None)

    def _seal(self, revision: DomainRevision, nonce_override: bytes | None) -> dict:
        remote_id = self.remote_id(revision)
        plaintext = json.dumps(
            revision.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if nonce_override is None:
            nonce, ciphertext = encrypt(self.content_key, plaintext, self._aad(remote_id))
        else:
            if len(nonce_override) != 12:
                raise ValidationError("测试 nonce 必须是 12 字节")
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except ImportError as exc:
                raise ValidationError("该操作需要 cryptography") from exc
            nonce = nonce_override
            ciphertext = AESGCM(self.content_key).encrypt(nonce, plaintext, self._aad(remote_id))
        return {
            "envelope_version": 1,
            "key_version": self.key_version,
            "nonce": _b64(nonce),
            "ciphertext": _b64(ciphertext),
            "remote_id": remote_id,
        }

    def open(self, remote_id: str, envelope: object) -> DomainRevision:
        return validate_revision(self.open_raw(remote_id, envelope))

    def open_raw(self, remote_id: str, envelope: object) -> dict:
        if not isinstance(envelope, dict) or envelope.get("envelope_version") != 1:
            raise ValidationError("不支持的同步密文 envelope")
        if envelope.get("key_version") != self.key_version:
            raise ValidationError("同步密文 key_version 不匹配")
        plaintext = decrypt(
            self.content_key,
            _unb64(envelope.get("nonce"), "nonce"),
            _unb64(envelope.get("ciphertext"), "ciphertext"),
            self._aad(remote_id),
        )
        try:
            revision = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise ValidationError("同步密文内容不是合法 JSON") from exc
        if not isinstance(revision, dict):
            raise ValidationError("同步密文内容不是领域对象")
        entity_kind = revision.get("entity_kind")
        entity_id = revision.get("entity_id")
        if not isinstance(entity_kind, str) or not isinstance(entity_id, str):
            raise ValidationError("同步密文缺少实体身份")
        expected = opaque_remote_id(self.index_key, entity_kind, entity_id)
        if expected != remote_id:
            raise ValidationError("同步密文被关联到错误实体")
        return revision


def create_key_material(account_id: str, key_version: int = 1) -> dict:
    if key_version <= 0:
        raise ValidationError("key_version 必须是正整数")
    account_data_key = random_key()
    recovery_secret = random_key()
    recovery_key = derive_key(
        recovery_secret,
        salt=hashlib.sha256(account_id.encode("utf-8")).digest(),
        info=b"mealcircuit-recovery-wrap-v1",
    )
    aad = f"MealCircuit Recovery v1\0{account_id}\0{key_version}".encode("utf-8")
    nonce, ciphertext = encrypt(recovery_key, account_data_key, aad)
    return {
        "account_data_key": account_data_key,
        "recovery_key": format_recovery_key(recovery_secret),
        "recovery_envelope": {
            "version": 1,
            "key_version": key_version,
            "nonce": _b64(nonce),
            "ciphertext": _b64(ciphertext),
        },
    }


def recover_account_data_key(account_id: str, recovery_key_text: str, envelope: object) -> bytes:
    if not isinstance(envelope, dict) or envelope.get("version") != 1:
        raise ValidationError("恢复密钥包版本无效")
    key_version = envelope.get("key_version")
    if not isinstance(key_version, int) or key_version <= 0:
        raise ValidationError("恢复密钥包 key_version 无效")
    secret = parse_recovery_key(recovery_key_text)
    recovery_key = derive_key(
        secret,
        salt=hashlib.sha256(account_id.encode("utf-8")).digest(),
        info=b"mealcircuit-recovery-wrap-v1",
    )
    aad = f"MealCircuit Recovery v1\0{account_id}\0{key_version}".encode("utf-8")
    return decrypt(
        recovery_key,
        _unb64(envelope.get("nonce"), "nonce"),
        _unb64(envelope.get("ciphertext"), "ciphertext"),
        aad,
    )


def validate_server_url(value: str, *, allow_insecure_localhost: bool = False) -> str:
    clean = str(value or "").strip().rstrip("/")
    parsed = urlsplit(clean)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError("同步服务 URL 无效")
    if parsed.scheme == "https":
        return clean
    if allow_insecure_localhost and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return clean
    raise ValidationError("同步服务必须使用 HTTPS；仅调试 localhost 可使用 HTTP")


def configure_sync(
    *,
    server_url: str,
    account_id: str,
    device_name: str,
    account_data_key: bytes,
    access_token: str,
    refresh_token: str,
    remote_device_id: str | None = None,
    key_version: int = 1,
    allow_insecure_localhost: bool = False,
) -> dict:
    init_db()
    url = validate_server_url(server_url, allow_insecure_localhost=allow_insecure_localhost)
    if not account_id.strip() or not device_name.strip() or key_version <= 0:
        raise ValidationError("account_id 和 device_name 不能为空")
    key_backend = set_secret("sync.account_data_key", account_data_key)
    set_secret("sync.access_token", access_token)
    set_secret("sync.refresh_token", refresh_token)
    timestamp = utc_now()
    with connect() as connection:
        connection.execute(
            """UPDATE sync_configuration SET enabled=1,server_url=?,account_id=?,device_name=?,
               remote_device_id=?,key_version=?,updated_at=? WHERE singleton=1""",
            (url, account_id.strip(), device_name.strip(), remote_device_id, key_version, timestamp),
        )
        from .domain_store import enqueue_all_heads, refresh_configuration_entities, seed_current_entities

        seed_current_entities(connection)
        refresh_configuration_entities(connection)
        enqueue_all_heads(connection)
    return {"enabled": True, "server_url": url, "account_id": account_id, "key_backend": key_backend}


def sync_status() -> dict:
    init_db()
    with connect() as connection:
        row = dict(connection.execute("SELECT * FROM sync_configuration WHERE singleton=1").fetchone())
        row["pending"] = connection.execute(
            "SELECT COUNT(*) FROM sync_outbox WHERE state='pending'"
        ).fetchone()[0]
        row["conflicts"] = connection.execute(
            "SELECT COUNT(*) FROM sync_conflicts WHERE status='unresolved'"
        ).fetchone()[0]
        row["unknown_schema_entities"] = connection.execute(
            "SELECT COUNT(*) FROM sync_unknown_entities"
        ).fetchone()[0]
        from .domain_store import unresolved_asset_references

        row["unresolved_assets"] = len(unresolved_asset_references(connection))
        row["cursor"] = connection.execute(
            "SELECT cursor_value FROM sync_cursor WHERE scope='account'"
        ).fetchone()
    row["cursor"] = row["cursor"][0] if row["cursor"] else 0
    row["account_data_key_available"] = get_secret("sync.account_data_key", binary=True) is not None
    row["access_token_available"] = get_secret("sync.access_token") is not None
    return row


def set_media_policy(value: str) -> dict:
    if value not in {"all", "all_wifi", "on_demand"}:
        raise ValidationError("照片同步策略必须是 all、all_wifi 或 on_demand")
    init_db()
    with connect() as connection:
        current = connection.execute(
            "SELECT enabled FROM sync_configuration WHERE singleton=1"
        ).fetchone()
        if not current or not current["enabled"]:
            raise ValidationError("同步尚未启用")
        connection.execute(
            "UPDATE sync_configuration SET media_policy=?,updated_at=? WHERE singleton=1",
            (value, utc_now()),
        )
    return {"media_policy": value}


def list_sync_devices() -> list[dict]:
    status = sync_status()
    if not status.get("enabled"):
        return []
    payload = HttpSyncTransport(str(status["server_url"])).devices()
    devices = payload.get("devices")
    if not isinstance(devices, list) or any(not isinstance(item, dict) for item in devices):
        raise ValidationError("同步服务设备响应格式无效")
    return devices


def revoke_sync_device(device_id: str) -> dict:
    status = sync_status()
    if not status.get("enabled"):
        raise ValidationError("同步尚未启用")
    devices = list_sync_devices()
    target = next((item for item in devices if item.get("id") == device_id), None)
    if target is None:
        raise ValidationError("设备不存在")
    if target.get("current"):
        raise ValidationError("不能从设备中心撤销当前设备；请使用取消本机同步")
    HttpSyncTransport(str(status["server_url"])).revoke_device(device_id)
    return {"revoked": device_id}


def delete_sync_account(password: str) -> dict:
    status = sync_status()
    if not status.get("enabled"):
        raise ValidationError("同步尚未启用")
    if not password:
        raise ValidationError("账户密码不能为空")
    HttpSyncTransport(str(status["server_url"])).delete_account(password)
    unlink_sync()
    return {"deleted": True, "local_data_preserved": True}


def unlink_sync() -> dict:
    init_db()
    timestamp = utc_now()
    with connect() as connection:
        connection.execute(
            """UPDATE sync_configuration SET enabled=0,server_url=NULL,account_id=NULL,
               remote_device_id=NULL,device_name='',updated_at=? WHERE singleton=1""",
            (timestamp,),
        )
        connection.execute("DELETE FROM sync_outbox")
        connection.execute("DELETE FROM sync_shadow")
        connection.execute("DELETE FROM sync_cursor")
    for name in ("sync.account_data_key", "sync.access_token", "sync.refresh_token"):
        delete_secret(name)
    return {"enabled": False, "local_data_preserved": True}


def _revision_from_row(row) -> DomainRevision:
    return validate_revision(
        {
            "schema_version": row["schema_version"],
            "entity_id": row["entity_id"],
            "entity_kind": row["entity_kind"],
            "revision_id": row["revision_id"],
            "parent_revision_ids": json.loads(row["parent_revision_ids_json"]),
            "created_at": row["created_at"],
            "author_