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
            "author_device_id": row["author_device_id"],
            "deleted": bool(row["deleted"]),
            "payload": json.loads(row["payload_json"]),
        }
    )


def prepare_outbox(limit: int = 100) -> list[dict]:
    init_db()
    with connect() as connection:
        config = connection.execute(
            "SELECT * FROM sync_configuration WHERE singleton=1"
        ).fetchone()
        if not config or not config["enabled"]:
            return []
        account_data_key = get_secret("sync.account_data_key", binary=True)
        if not isinstance(account_data_key, bytes):
            raise ValidationError("当前会话没有 Account Data Key；请重新解锁同步")
        cipher = AccountCipher(config["account_id"], account_data_key, config["key_version"])
        rows = connection.execute(
            """SELECT o.*,r.entity_kind,r.parent_revision_ids_json,r.payload_json,
                      r.schema_version,r.author_device_id,r.deleted,r.created_at AS revision_created_at
               FROM sync_outbox o JOIN domain_revisions r ON r.revision_id=o.revision_id
               WHERE o.state='pending' ORDER BY o.local_sequence LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            revision = validate_revision(
                {
                    "schema_version": row["schema_version"],
                    "entity_id": row["entity_id"],
                    "entity_kind": row["entity_kind"],
                    "revision_id": row["revision_id"],
                    "parent_revision_ids": json.loads(row["parent_revision_ids_json"]),
                    "created_at": row["revision_created_at"],
                    "author_device_id": row["author_device_id"],
                    "deleted": bool(row["deleted"]),
                    "payload": json.loads(row["payload_json"]),
                }
            )
            envelope = cipher.seal(revision)
            encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """UPDATE sync_outbox SET opaque_remote_id=?,encrypted_envelope=?,key_version=?,updated_at=?
                   WHERE local_sequence=?""",
                (envelope["remote_id"], encoded, cipher.key_version, utc_now(), row["local_sequence"]),
            )
            result.append(
                {
                    "op_id": row["op_id"],
                    "remote_id": envelope["remote_id"],
                    "base_server_version": row["base_server_version"],
                    "key_version": cipher.key_version,
                    "envelope": envelope,
                }
            )
        return result


class SyncTransport(Protocol):
    def push(self, operations: list[dict]) -> dict: ...

    def pull(self, cursor: int, limit: int = 500, snapshot_offset: int = 0) -> dict: ...

    def ack(self, cursor: int) -> dict: ...

    def create_blob(self, blob_id: str, byte_count: int, chunk_count: int, key_version: int) -> dict: ...

    def upload_blob_chunk(self, blob_id: str, index: int, value: bytes) -> None: ...

    def complete_blob(self, blob_id: str) -> dict: ...

    def download_blob_chunk(self, blob_id: str, index: int) -> bytes | None: ...


class SyncHttpError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> dict:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        try:
            value = json.loads(exc.read().decode("utf-8"))
            detail = str(value.get("detail") or f"HTTP {exc.code}")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            detail = f"HTTP {exc.code}"
        raise SyncHttpError(exc.code, detail) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ValidationError(f"无法连接同步服务：{type(exc).__name__}") from None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("同步服务返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("同步服务响应格式无效")
    return value


def _http_bytes(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> bytes:
    headers = {"Accept": "application/octet-stream"}
    if body is not None:
        headers["Content-Type"] = "application/octet-stream"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise SyncHttpError(exc.code, f"HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ValidationError(f"无法连接同步服务：{type(exc).__name__}") from None


@dataclass
class HttpSyncTransport:
    server_url: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self.server_url = validate_server_url(self.server_url, allow_insecure_localhost=True)

    def _refresh(self) -> str:
        refresh_token = get_secret("sync.refresh_token")
        if not isinstance(refresh_token, str):
            raise ValidationError("当前会话没有 refresh token；请重新登录同步")
        try:
            result = _http_json(
                f"{self.server_url}/v1/sessions/refresh",
                method="POST",
                body={"refresh_token": refresh_token},
                timeout=self.timeout,
            )
        except SyncHttpError as exc:
            raise ValidationError(f"同步登录已失效：{exc.detail}") from None
        access = result.get("access_token")
        refresh = result.get("refresh_token")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise ValidationError("同步服务刷新响应缺少令牌")
        set_secret("sync.access_token", access)
        set_secret("sync.refresh_token", refresh)
        return access

    def _authorized(self, path: str, *, method: str = "GET", body: dict | None = None) -> dict:
        access = get_secret("sync.access_token")
        if not isinstance(access, str):
            raise ValidationError("当前会话没有 access token；请重新登录同步")
        try:
            return _http_json(
                f"{self.server_url}{path}", method=method, body=body, token=access, timeout=self.timeout
            )
        except SyncHttpError as exc:
            if exc.status != 401:
                raise ValidationError(f"同步服务拒绝请求：{exc.detail}") from None
        access = self._refresh()
        try:
            return _http_json(
                f"{self.server_url}{path}", method=method, body=body, token=access, timeout=self.timeout
            )
        except SyncHttpError as exc:
            raise ValidationError(f"同步服务拒绝请求：{exc.detail}") from None

    def push(self, operations: list[dict]) -> dict:
        return self._authorized("/v1/sync/push", method="POST", body={"operations": operations})

    def capabilities(self) -> dict:
        return _http_json(f"{self.server_url}/v1/capabilities", timeout=self.timeout)

    def pull(self, cursor: int, limit: int = 500, snapshot_offset: int = 0) -> dict:
        query = urlencode({"cursor": cursor, "limit": limit, "snapshot_offset": snapshot_offset})
        return self._authorized(f"/v1/sync/pull?{query}")

    def ack(self, cursor: int) -> dict:
        return self._authorized("/v1/sync/ack", method="POST", body={"cursor": cursor})

    def create_blob(self, blob_id: str, byte_count: int, chunk_count: int, key_version: int) -> dict:
        return self._authorized(
            "/v1/blobs",
            method="POST",
            body={
                "blob_id": blob_id,
                "byte_count": byte_count,
                "chunk_count": chunk_count,
                "key_version": key_version,
            },
        )

    def _authorized_bytes(
        self, path: str, *, method: str = "GET", body: bytes | None = None
    ) -> bytes:
        access = get_secret("sync.access_token")
        if not isinstance(access, str):
            raise ValidationError("当前会话没有 access token；请重新登录同步")
        try:
            return _http_bytes(
                f"{self.server_url}{path}", method=method, body=body, token=access, timeout=self.timeout
            )
        except SyncHttpError as exc:
            if exc.status != 401:
                raise
        access = self._refresh()
        return _http_bytes(
            f"{self.server_url}{path}", method=method, body=body, token=access, timeout=self.timeout
        )

    def upload_blob_chunk(self, blob_id: str, index: int, value: bytes) -> None:
        try:
            self._authorized_bytes(f"/v1/blobs/{blob_id}/chunks/{index}", method="PUT", body=value)
        except SyncHttpError as exc:
            raise ValidationError(f"同步服务拒绝资产分块：{exc.detail}") from None

    def complete_blob(self, blob_id: str) -> dict:
        return self._authorized(f"/v1/blobs/{blob_id}/complete", method="POST", body={})

    def download_blob_chunk(self, blob_id: str, index: int) -> bytes | None:
        try:
            return self._authorized_bytes(f"/v1/blobs/{blob_id}/chunks/{index}")
        except SyncHttpError as exc:
            if exc.status == 404:
                return None
            raise ValidationError(f"同步服务拒绝资产下载：{exc.detail}") from None

    def begin_key_rotation(self) -> dict:
        return self._authorized("/v1/key-rotations", method="POST", body={})

    def key_rotation_status(self) -> dict:
        return self._authorized("/v1/key-rotations/current")

    def abort_key_rotation(self) -> None:
        self._authorized("/v1/key-rotations/current", method="DELETE")

    def commit_key_rotation(self, body: dict) -> dict:
        return self._authorized("/v1/key-rotations/current/commit", method="POST", body=body)

    def devices(self) -> dict:
        return self._authorized("/v1/devices")

    def revoke_device(self, device_id: str) -> None:
        safe = quote(device_id, safe="")
        self._authorized(f"/v1/devices/{safe}", method="DELETE")

    def delete_account(self, password: str) -> None:
        self._authorized("/v1/account", method="DELETE", body={"password": password})


def register_sync(
    *,
    server_url: str,
    login_name: str,
    password: str,
    device_name: str,
    confirm_recovery_key: Callable[[str], bool],
    allow_insecure_localhost: bool = False,
) -> dict:
    url = validate_server_url(server_url, allow_insecure_localhost=allow_insecure_localhost)
    try:
        session = _http_json(
            f"{url}/v1/accounts",
            method="POST",
            body={"login_name": login_name, "password": password, "device_name": device_name},
        )
    except SyncHttpError as exc:
        raise ValidationError(f"无法创建同步账户：{exc.detail}") from None
    account_id = session.get("account_id")
    if not isinstance(account_id, str):
        raise ValidationError("同步服务注册响应缺少 account_id")
    keys = create_key_material(account_id)
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise ValidationError("同步服务注册响应缺少令牌")
    try:
        _http_json(
            f"{url}/v1/key-envelopes/recovery",
            method="PUT",
            body={"envelope": keys["recovery_envelope"]},
            token=access_token,
        )
    except SyncHttpError as exc:
        raise ValidationError(f"无法保存恢复密钥包：{exc.detail}") from None
    if not confirm_recovery_key(keys["recovery_key"]):
        try:
            _http_json(
                f"{url}/v1/account",
                method="DELETE",
                body={"password": password},
                token=access_token,
            )
        except (SyncHttpError, ValidationError):
            pass
        raise ValidationError("恢复密钥确认失败；同步未启用")
    configured = configure_sync(
        server_url=url,
        account_id=account_id,
        device_name=device_name,
        remote_device_id=session.get("device_id"),
        account_data_key=keys["account_data_key"],
        access_token=access_token,
        refresh_token=refresh_token,
        allow_insecure_localhost=allow_insecure_localhost,
    )
    configured["recovery_key_confirmed"] = True
    return configured


def login_sync(
    *,
    server_url: str,
    login_name: str,
    password: str,
    device_name: str,
    recovery_key: str,
    allow_insecure_localhost: bool = False,
) -> dict:
    url = validate_server_url(server_url, allow_insecure_localhost=allow_insecure_localhost)
    try:
        session = _http_json(
            f"{url}/v1/sessions",
            method="POST",
            body={"login_name": login_name, "password": password, "device_name": device_name},
        )
        recovery = _http_json(
            f"{url}/v1/key-envelopes/recovery",
            token=session.get("access_token"),
        )
    except SyncHttpError as exc:
        raise ValidationError(f"无法登录同步账户：{exc.detail}") from None
    account_id = session.get("account_id")
    if not isinstance(account_id, str):
        raise ValidationError("同步服务登录响应缺少 account_id")
    account_data_key = recover_account_data_key(account_id, recovery_key, recovery.get("envelope"))
    envelope = recovery.get("envelope")
    key_version = envelope.get("key_version") if isinstance(envelope, dict) else None
    if not isinstance(key_version, int) or key_version <= 0:
        raise ValidationError("同步服务恢复包缺少有效 key_version")
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise ValidationError("同步服务登录响应缺少令牌")
    return configure_sync(
        server_url=url,
        account_id=account_id,
        device_name=device_name,
        remote_device_id=session.get("device_id"),
        account_data_key=account_data_key,
        access_token=access_token,
        refresh_token=refresh_token,
        key_version=key_version,
        allow_insecure_localhost=allow_insecure_localhost,
    )


def bootstrap_sync(
    *,
    server_url: str,
    login_name: str,
    password: str,
    device_name: str,
    confirm_recovery_key: Callable[[str], bool],
    allow_insecure_localhost: bool = False,
) -> dict:
    """Create client-side key material for an administrator-precreated empty account."""
    url = validate_server_url(server_url, allow_insecure_localhost=allow_insecure_localhost)
    try:
        session = _http_json(
            f"{url}/v1/sessions",
            method="POST",
            body={"login_name": login_name, "password": password, "device_name": device_name},
        )
    except SyncHttpError as exc:
        raise ValidationError(f"无法登录预建同步账户：{exc.detail}") from None
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    account_id = session.get("account_id")
    if not all(isinstance(item, str) for item in (access_token, refresh_token, account_id)):
        raise ValidationError("同步服务登录响应缺少必要字段")
    try:
        _http_json(f"{url}/v1/key-envelopes/recovery", token=access_token)
    except SyncHttpError as exc:
        if exc.status != 404:
            raise ValidationError(f"无法检查恢复密钥包：{exc.detail}") from None
    else:
        raise ValidationError("该账户已经初始化密钥；请使用普通登录")
    keys = create_key_material(account_id)
    if not confirm_recovery_key(keys["recovery_key"]):
        raise ValidationError("恢复密钥确认失败；账户密钥尚未初始化")
    try:
        _http_json(
            f"{url}/v1/key-envelopes/recovery",
            method="PUT",
            body={"envelope": keys["recovery_envelope"]},
            token=access_token,
        )
    except SyncHttpError as exc:
        raise ValidationError(f"无法保存恢复密钥包：{exc.detail}") from None
    result = configure_sync(
        server_url=url,
        account_id=account_id,
        device_name=device_name,
        remote_device_id=session.get("device_id"),
        account_data_key=keys["account_data_key"],
        access_token=access_token,
        refresh_token=refresh_token,
        allow_insecure_localhost=allow_insecure_localhost,
    )
    result["recovery_key_confirmed"] = True
    return result


def _revision_json(revision: DomainRevision) -> str:
    return json.dumps(revision.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_revision(connection: sqlite3.Connection, revision_id: str) -> DomainRevision:
    row = connection.execute(
        "SELECT * FROM domain_revisions WHERE revision_id=?", (revision_id,)
    ).fetchone()
    if row is None:
        raise ValidationError(f"本机缺少领域 revision：{revision_id}")
    return _revision_from_row(row)


def _store_revision(connection: sqlite3.Connection, revision: DomainRevision) -> None:
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


def _set_head(connection: sqlite3.Connection, revision: DomainRevision, *, conflicted: bool = False) -> None:
    connection.execute(
        """INSERT INTO entity_heads(entity_id,entity_kind,revision_id,conflicted,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET
           entity_kind=excluded.entity_kind,revision_id=excluded.revision_id,
           conflicted=excluded.conflicted,updated_at=excluded.updated_at""",
        (
            revision.entity_id, revision.entity_kind, revision.revision_id,
            int(conflicted), revision.created_at,
        ),
    )


def _upsert_shadow(
    connection: sqlite3.Connection,
    remote_id: str,
    server_version: int,
    revision: DomainRevision,
) -> None:
    connection.execute(
        """INSERT INTO sync_shadow(
               opaque_remote_id,entity_id,server_version,revision_id,payload_json,updated_at
           ) VALUES(?,?,?,?,?,?) ON CONFLICT(opaque_remote_id) DO UPDATE SET
           entity_id=excluded.entity_id,server_version=excluded.server_version,
           revision_id=excluded.revision_id,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
        (
            remote_id,
            revision.entity_id,
            server_version,
            revision.revision_id,
            json.dumps(revision.payload, ensure_ascii=False, sort_keys=True),
            utc_now(),
        ),
    )


def _queue_revision(connection: sqlite3.Connection, revision: DomainRevision, base_version: int) -> None:
    config = connection.execute(
        "SELECT key_version FROM sync_configuration WHERE singleton=1"
    ).fetchone()
    connection.execute("DELETE FROM sync_outbox WHERE entity_id=?", (revision.entity_id,))
    timestamp = utc_now()
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
            base_version,
            config["key_version"],
            timestamp,
            timestamp,
        ),
    )


def _record_conflict(
    connection: sqlite3.Connection,
    *,
    base: DomainRevision | None,
    local: DomainRevision,
    remote: DomainRevision,
    paths: list[str],
) -> str:
    conflict_id = new_id("conflict")
    connection.execute(
        """INSERT INTO sync_conflicts(
               id,entity_id,entity_kind,base_revision_json,local_revision_json,
               remote_revision_json,conflicting_paths_json,status,created_at
           ) VALUES(?,?,?,?,?,?,?,'unresolved',?)""",
        (
            conflict_id,
            local.entity_id,
            local.entity_kind,
            _revision_json(base) if base else None,
            _revision_json(local),
            _revision_json(remote),
            json.dumps(paths, ensure_ascii=False),
            utc_now(),
        ),
    )
    connection.execute(
        "UPDATE entity_heads SET conflicted=1 WHERE entity_id=?", (local.entity_id,)
    )
    connection.execute(
        "UPDATE sync_outbox SET state='conflict',updated_at=? WHERE entity_id=?",
        (utc_now(), local.entity_id),
    )
    return conflict_id


def _logical_key(revision: DomainRevision) -> tuple[str, str] | None:
    try:
        if revision.entity_kind in {"checkin_day", "checkin_draft"}:
            return ("checkin_day", str(revision.payload["checkin"]["checkin_date"]))
        if revision.entity_kind == "daily_review":
            return ("daily_review", str(revision.payload["review"]["review_date"]))
    except (KeyError, TypeError):
        return None
    return None


def _find_logical_sibling(
    connection: sqlite3.Connection, remote: DomainRevision
) -> DomainRevision | None:
    key = _logical_key(remote)
    if key is None or remote.deleted:
        return None
    rows = connection.execute(
        """SELECT r.* FROM entity_heads h JOIN domain_revisions r ON r.revision_id=h.revision_id
           WHERE h.entity_kind=? AND h.entity_id<>? ORDER BY h.entity_id""",
        (remote.entity_kind, remote.entity_id),
    ).fetchall()
    for row in rows:
        candidate = _revision_from_row(row)
        if not candidate.deleted and _logical_key(candidate) == key:
            return candidate
    return None


def _canonicalize_logical_payload(
    entity_kind: str, payload: dict, canonical_id: str
) -> dict:
    value = copy.deepcopy(payload)
    if entity_kind in {"checkin_day", "checkin_draft"}:
        value["checkin"]["id"] = canonical_id
        for item in value.get("modules", []):
            item["module"]["checkin_id"] = canonical_id
    elif entity_kind == "daily_review":
        value["review"]["id"] = canonical_id
        for item in value.get("history", []):
            item["review_id"] = canonical_id
    return value


def _remove_logical_projection(connection: sqlite3.Connection, revision: DomainRevision) -> None:
    if revision.entity_kind in {"checkin_day", "checkin_draft"}:
        module_ids = [
            row[0] for row in connection.execute(
                "SELECT id FROM daily_checkin_modules WHERE checkin_id=?", (revision.entity_id,)
            )
        ]
        for module_id in module_ids:
            connection.execute("DELETE FROM daily_checkin_module_history WHERE module_id=?", (module_id,))
        connection.execute("DELETE FROM daily_checkin_modules WHERE checkin_id=?", (revision.entity_id,))
        connection.execute("DELETE FROM daily_checkins WHERE id=?", (revision.entity_id,))
    elif revision.entity_kind == "daily_review":
        connection.execute("DELETE FROM daily_review_history WHERE review_id=?", (revision.entity_id,))
        connection.execute("DELETE FROM daily_reviews WHERE id=?", (revision.entity_id,))


def _merge_logical_checkins(
    local: DomainRevision, remote: DomainRevision, canonical_id: str
) -> tuple[dict, list[str]]:
    left_items = {item["module"]["module_key"]: item for item in local.payload.get("modules", [])}
    right_items = {item["module"]["module_key"]: item for item in remote.payload.get("modules", [])}
    merged_items: list[dict] = []
    conflicts: list[str] = []

    def active(item: dict) -> bool:
        module = item["module"]
        return (
            module.get("status") != "not_started"
            or int(module.get("version") or 0) > 0
            or bool(module.get("answers_json"))
            or bool(module.get("draft_json"))
        )

    for key in sorted(set(left_items) | set(right_items)):
        left = left_items.get(key)
        right = right_items.get(key)
        if left is None or right is None:
            selected = copy.deepcopy(left or right)
        elif not active(left) and active(right):
            selected = copy.deepcopy(right)
        elif not active(right) and active(left):
            selected = copy.deepcopy(left)
        elif not active(left) and not active(right):
            selected = copy.deepcopy(left)
        else:
            left_module = left["module"]
            right_module = right["module"]
            if left_module.get("status") != right_module.get("status"):
                conflicts.append(f"modules[{key}].status")
                selected = copy.deepcopy(left)
            else:
                selected = copy.deepcopy(left)
                for field in ("answers_json", "draft_json"):
                    before: dict = {}
                    left_value = left_module.get(field) or {}
                    right_value = right_module.get(field) or {}
                    merged, paths = three_way_merge(before, left_value, right_value)
                    if paths:
                        conflicts.extend(f"modules[{key}].{field}.{path}" for path in paths)
                    selected["module"][field] = merged
                history = {
                    item["id"]: copy.deepcopy(item)
                    for item in [*left.get("history", []), *right.get("history", [])]
                }
                selected["history"] = [history[item_id] for item_id in sorted(history)]
                selected["module"]["version"] = max(
                    int(left_module.get("version") or 0), int(right_module.get("version") or 0)
                )
        selected["module"]["checkin_id"] = canonical_id
        merged_items.append(selected)
    payload = _canonicalize_logical_payload(local.entity_kind, local.payload, canonical_id)
    payload["modules"] = merged_items
    return payload, sorted(set(conflicts))


def _merge_logical_pending_reviews(
    local: DomainRevision, remote: DomainRevision, canonical_id: str
) -> dict | None:
    left = local.payload["review"]
    right = remote.payload["review"]
    if (
        left.get("status") != "pending"
        or right.get("status") != "pending"
        or left.get("result_json") is not None
        or right.get("result_json") is not None
    ):
        return None
    payload = _canonicalize_logical_payload("daily_review", local.payload, canonical_id)
    review = payload["review"]
    review["source_record_ids_json"] = sorted(
        set(left.get("source_record_ids_json") or []) | set(right.get("source_record_ids_json") or [])
    )
    versions = dict(left.get("source_checkin_versions_json") or {})
    for key, value in (right.get("source_checkin_versions_json") or {}).items():
        versions[key] = max(int(versions.get(key, 0)), int(value))
    review["source_checkin_versions_json"] = versions
    history = {
        item["id"]: copy.deepcopy(item)
        for item in [*local.payload.get("history", []), *remote.payload.get("history", [])]
    }
    payload["history"] = [history[item_id] for item_id in sorted(history)]
    return payload


def _shadow_version(connection: sqlite3.Connection, entity_id: str) -> int:
    row = connection.execute(
        "SELECT server_version FROM sync_shadow WHERE entity_id=?", (entity_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def _commit_logical_merge(
    connection: sqlite3.Connection,
    *,
    local: DomainRevision,
    remote: DomainRevision,
    payload: dict,
    remote_server_version: int,
    deleted: bool = False,
) -> DomainRevision:
    canonical_id = min(local.entity_id, remote.entity_id)
    alias_id = max(local.entity_id, remote.entity_id)
    device_id = connection.execute("SELECT value FROM app_metadata WHERE key='device_id'").fetchone()[0]
    merged = make_revision(
        local.entity_kind,
        _canonicalize_logical_payload(local.entity_kind, payload, canonical_id),
        entity_id=canonical_id,
        parent_revision_ids=[local.revision_id, remote.revision_id],
        author_device_id=device_id,
        deleted=deleted,
    )
    for revision in (local, remote):
        _remove_logical_projection(connection, revision)
    _store_revision(connection, merged)
    _set_head(connection, merged)
    from .domain_store import materialize_revision

    materialize_revision(connection, merged)
    canonical_base = remote_server_version if canonical_id == remote.entity_id else _shadow_version(connection, local.entity_id)
    _queue_revision(connection, merged, canonical_base)

    alias_source = remote if alias_id == remote.entity_id else local
    tombstone = make_revision(
        alias_source.entity_kind,
        alias_source.payload,
        entity_id=alias_id,
        parent_revision_ids=[alias_source.revision_id],
        author_device_id=device_id,
        deleted=True,
    )
    _store_revision(connection, tombstone)
    _set_head(connection, tombstone)
    alias_base = remote_server_version if alias_id == remote.entity_id else _shadow_version(connection, local.entity_id)
    _queue_revision(connection, tombstone, alias_base)
    return merged


def _merge_remote(
    connection: sqlite3.Connection,
    *,
    remote_id: str,
    server_version: int,
    local: DomainRevision,
    remote: DomainRevision,
) -> tuple[str, DomainRevision | None]:
    shadow = connection.execute(
        "SELECT revision_id FROM sync_shadow WHERE entity_id=?", (local.entity_id,)
    ).fetchone()
    base = _load_revision(connection, shadow["revision_id"]) if shadow else None
    _store_revision(connection, remote)
    _upsert_shadow(connection, remote_id, server_version, remote)
    if local.payload == remote.payload and local.deleted == remote.deleted:
        connection.execute("DELETE FROM sync_outbox WHERE entity_id=?", (local.entity_id,))
        return "identical", None
    if base is None:
        _record_conflict(connection, base=None, local=local, remote=remote, paths=["$"])
        return "conflict", None
    merged_payload, paths = three_way_merge(base.payload, local.payload, remote.payload)
    local_edited = local.payload != base.payload
    remote_edited = remote.payload != base.payload
    delete_edit = local.deleted != remote.deleted and (
        (local.deleted != base.deleted and remote_edited)
        or (remote.deleted != base.deleted and local_edited)
    )
    if delete_edit:
        paths.append("$deleted")
    if paths:
        _record_conflict(connection, base=base, local=local, remote=remote, paths=sorted(set(paths)))
        return "conflict", None
    if local.deleted == remote.deleted:
        merged_deleted = local.deleted
    elif local.deleted == base.deleted:
        merged_deleted = remote.deleted
    else:
        merged_deleted = local.deleted
    device_id = connection.execute(
        "SELECT value FROM app_metadata WHERE key='device_id'"
    ).fetchone()[0]
    merged = make_revision(
        local.entity_kind,
        merged_payload,
        entity_id=local.entity_id,
        parent_revision_ids=[local.revision_id, remote.revision_id],
        author_device_id=device_id,
        deleted=merged_deleted,
    )
    from .domain_store import materialize_revision

    _store_revision(connection, merged)
    _set_head(connection, merged)
    materialize_revision(connection, merged)
    _queue_revision(connection, merged, server_version)
    return "merged", merged


def _process_push(connection: sqlite3.Connection, cipher: AccountCipher, payload: dict) -> dict:
    counts = {"accepted": 0, "conflicts": 0, "merged": 0, "unknown": 0}
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            raise ValidationError("同步 push 响应条目无效")
        outbox = connection.execute(
            "SELECT * FROM sync_outbox WHERE op_id=?", (result.get("op_id"),)
        ).fetchone()
        if outbox is None:
            continue
        local = _load_revision(connection, outbox["revision_id"])
        remote_id = str(result.get("remote_id") or outbox["opaque_remote_id"])
        server_version = int(result.get("server_version", 0))
        if result.get("status") == "accepted":
            _upsert_shadow(connection, remote_id, server_version, local)
            connection.execute("DELETE FROM sync_outbox WHERE op_id=?", (outbox["op_id"],))
            counts["accepted"] += 1
        elif result.get("status") == "conflict" and result.get("envelope"):
            raw = cipher.open_raw(remote_id, result["envelope"])
            try:
                remote = validate_revision(raw)
            except ValidationError:
                timestamp = utc_now()
                connection.execute(
                    """INSERT INTO sync_unknown_entities(
                           opaque_remote_id,server_version,key_version,encrypted_envelope,first_seen_at,updated_at
                       ) VALUES(?,?,?,?,?,?) ON CONFLICT(opaque_remote_id) DO UPDATE SET
                       server_version=excluded.server_version,key_version=excluded.key_version,
                       encrypted_envelope=excluded.encrypted_envelope,updated_at=excluded.updated_at""",
                    (
                        remote_id, server_version, int(result.get("key_version", 0)),
                        json.dumps(result["envelope"], sort_keys=True, separators=(",", ":")),
                        timestamp, timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE sync_outbox SET state='conflict',updated_at=? WHERE op_id=?",
                    (timestamp, outbox["op_id"]),
                )
                counts["unknown"] += 1
                continue
            outcome, _ = _merge_remote(
                connection,
                remote_id=remote_id,
                server_version=server_version,
                local=local,
                remote=remote,
            )
            counts["conflicts" if outcome == "conflict" else "merged"] += 1
        else:
            raise ValidationError("同步 push 响应状态无效")
    return counts


def _json_mapping(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _remote_daily_source_date(revision: DomainRevision) -> str | None:
    if revision.entity_kind == "daily_record":
        value = revision.payload.get("record_date")
    elif revision.entity_kind == "checkin_day":
        checkin = revision.payload.get("checkin")
        value = checkin.get("checkin_date") if isinstance(checkin, dict) else None
    else:
        return None
    return value if isinstance(value, str) else None


def _review_source_revision_id(review: sqlite3.Row, entity_id: str) -> str | None:
    provenance = _json_mapping(review["result_provenance_json"])
    for source in provenance.get("source_revisions") or []:
        if not isinstance(source, dict) or source.get("entity_id") != entity_id:
            continue
        revision_id = source.get("revision_id")
        return revision_id if isinstance(revision_id, str) else None
    return None


def _remote_checkin_versions(revision: DomainRevision) -> dict[str, int]:
    versions: dict[str, int] = {}
    for item in revision.payload.get("modules") or []:
        if not isinstance(item, dict):
            continue
        module = item.get("module")
        if not isinstance(module, dict):
            continue
        key = module.get("module_key")
        version = module.get("version")
        if isinstance(key, str) and isinstance(version, int) and version > 0:
            versions[key] = max(versions.get(key, 0), version)
    return versions


def _remote_daily_source_requires_requeue(
    connection: sqlite3.Connection, revision: DomainRevision
) -> tuple[str, str] | None:
    record_date = _remote_daily_source_date(revision)
    if record_date is None:
        return None
    review = connection.execute(
        "SELECT * FROM daily_reviews WHERE review_date=?", (record_date,)
    ).fetchone()
    reason = f"sync_remote_{revision.entity_kind}"
    if review is None or review["status"] != "completed":
        return record_date, reason

    source_revision_id = _review_source_revision_id(review, revision.entity_id)
    if source_revision_id is not None:
        return None if source_revision_id == revision.revision_id else (record_date, reason)

    if revision.entity_kind == "daily_record":
        source_ids = _json_string_list(review["source_record_ids_json"])
        if revision.entity_id not in source_ids:
            return record_date, reason
    elif revision.deleted:
        return record_date, reason
    else:
        included_versions = _json_mapping(review["source_checkin_versions_json"])
        for module_key, version in _remote_checkin_versions(revision).items():
            try:
                included = int(included_versions.get(module_key, 0))
            except (TypeError, ValueError):
                included = 0
            if version > included:
                return record_date, reason

    # Current reviews persist source revision IDs. This fallback preserves old
    # reviews without provenance: an older synced snapshot can be treated as
    # included, while a post-completion mutation must reopen the review.
    completed_at = review["completed_at"]
    if isinstance(completed_at, str) and revision.created_at > completed_at:
        return record_date, reason
    return None


def _queue_remote_daily_source_updates(revisions: list[DomainRevision]) -> list[str]:
    pending: dict[str, list[tuple[DomainRevision, str]]] = {}
    with connect() as connection:
        for revision in revisions:
            change = _remote_daily_source_requires_requeue(connection, revision)
            if change is None:
                continue
            record_date, reason = change
            pending.setdefault(record_date, []).append((revision, reason))
    if not pending:
        return []

    from . import agent_workspace, service

    for record_date, changes in pending.items():
        for revision, _ in changes:
            if revision.entity_kind != "daily_record" or revision.deleted:
                continue
            try:
                agent_workspace.supersede_source_evidence(
                    "daily_record", revision.entity_id, reason="sync_remote_daily_record"
                )
                agent_workspace.process_natural_language_input(
                    revision.entity_id,
                    str(revision.payload.get("raw_input") or ""),
                    record_date,
                    source_type="daily_record",
                )
            except Exception as exc:
                agent_workspace.record_diagnostic_event(
                    record_date,
                    "sync_remote_intent_processing_failed",
                    str(exc),
                    {"record_id": revision.entity_id},
                )
        reasons = {reason for _, reason in changes}
        service.queue_review_for_external_change(record_date, sorted(reasons)[0])
        agent_workspace.schedule_auto_draft(record_date)
    return sorted(pending)


def _process_pull(
    connection: sqlite3.Connection, cipher: AccountCipher, payload: dict
) -> tuple[dict, list[DomainRevision], list[DomainRevision]]:
    counts = {"applied": 0, "conflicts": 0, "merged": 0, "unknown": 0, "skipped": 0}
    mirror_updates: list[DomainRevision] = []
    daily_source_updates: list[DomainRevision] = []
    from .domain_store import materialize_revision

    for change in payload.get("changes") or []:
        if not isinstance(change, dict):
            raise ValidationError("同步 pull 响应条目无效")
        remote_id = str(change.get("remote_id") or "")
        server_version = int(change.get("server_version", 0))
        current = connection.execute(
            "SELECT server_version FROM sync_shadow WHERE opaque_remote_id=?", (remote_id,)
        ).fetchone()
        if current and int(current["server_version"]) >= server_version:
            counts["skipped"] += 1
            continue
        raw = cipher.open_raw(remote_id, change.get("envelope"))
        try:
            remote = validate_revision(raw)
        except ValidationError:
            timestamp = utc_now()
            connection.execute(
                """INSERT INTO sync_unknown_entities(
                       opaque_remote_id,server_version,key_version,encrypted_envelope,first_seen_at,updated_at
                   ) VALUES(?,?,?,?,?,?) ON CONFLICT(opaque_remote_id) DO UPDATE SET
                   server_version=excluded.server_version,key_version=excluded.key_version,
                   encrypted_envelope=excluded.encrypted_envelope,updated_at=excluded.updated_at""",
                (
                    remote_id,
                    server_version,
                    int(change.get("key_version", 0)),
                    json.dumps(change.get("envelope"), sort_keys=True, separators=(",", ":")),
                    timestamp,
                    timestamp,
                ),
            )
            counts["unknown"] += 1
            continue
        logical_local = _find_logical_sibling(connection, remote)
        if logical_local is not None:
            _store_revision(connection, remote)
            _upsert_shadow(connection, remote_id, server_version, remote)
            if remote.entity_kind == "checkin_day" and not logical_local.deleted and not remote.deleted:
                canonical_id = min(logical_local.entity_id, remote.entity_id)
                merged_payload, logical_paths = _merge_logical_checkins(
                    logical_local, remote, canonical_id
                )
                if not logical_paths:
                    _commit_logical_merge(
                        connection,
                        local=logical_local,
                        remote=remote,
                        payload=merged_payload,
                        remote_server_version=server_version,
                    )
                    counts["merged"] += 1
                    daily_source_updates.append(remote)
                    continue
            elif remote.entity_kind == "daily_review" and not logical_local.deleted and not remote.deleted:
                canonical_id = min(logical_local.entity_id, remote.entity_id)
                review_payload = _merge_logical_pending_reviews(logical_local, remote, canonical_id)
                if review_payload is not None:
                    _commit_logical_merge(
                        connection,
                        local=logical_local,
                        remote=remote,
                        payload=review_payload,
                        remote_server_version=server_version,
                    )
                    counts["merged"] += 1
                    continue
                logical_paths = ["$active_result"]
            else:
                logical_paths = ["$logical_key"]
            _record_conflict(
                connection,
                base=None,
                local=logical_local,
                remote=remote,
                paths=logical_paths or ["$logical_key"],
            )
            counts["conflicts"] += 1
            continue
        pending = connection.execute(
            """SELECT revision_id FROM sync_outbox
               WHERE entity_id=? AND state IN ('pending','sending') ORDER BY local_sequence DESC LIMIT 1""",
            (remote.entity_id,),
        ).fetchone()
        if pending:
            local = _load_revision(connection, pending["revision_id"])
            outcome, merged = _merge_remote(
                connection,
                remote_id=remote_id,
                server_version=server_version,
                local=local,
                remote=remote,
            )
            counts["conflicts" if outcome == "conflict" else "merged"] += 1
            if merged and merged.entity_kind == "preferences":
                mirror_updates.append(merged)
            if outcome != "conflict" and remote.entity_kind in {"daily_record", "checkin_day"}:
                daily_source_updates.append(remote)
            continue
        _store_revision(connection, remote)
        _set_head(connection, remote)
        parent_task_id = (
            remote.payload.get("task_id")
            if remote.entity_kind in {"task_input", "correction"}
            else None
        )
        parent_ready = not isinstance(parent_task_id, str) or connection.execute(
            "SELECT 1 FROM tasks WHERE id=?", (parent_task_id,)
        ).fetchone()
        if parent_ready:
            materialize_revision(connection, remote)
        _upsert_shadow(connection, remote_id, server_version, remote)
        if remote.entity_kind == "preferences":
            mirror_updates.append(remote)
        if remote.entity_kind in {"daily_record", "checkin_day"}:
            daily_source_updates.append(remote)
        counts["applied"] += 1
    # A full snapshot is ordered by opaque remote ID, and a defensive client
    # must also tolerate replayed pages arriving out of order. Child revisions
    # are retained immediately, then materialized once their parent task is
    # present. This keeps server-visible ordering irrelevant without dropping
    # a correction or task-input history.
    for row in connection.execute(
        """SELECT r.* FROM entity_heads h
           JOIN domain_revisions r ON r.revision_id=h.revision_id
           WHERE h.entity_kind IN ('task_input','correction')
           ORDER BY CASE h.entity_kind WHEN 'task_input' THEN 0 ELSE 1 END,h.entity_id"""
    ).fetchall():
        revision = _revision_from_row(row)
        task_id = revision.payload.get("task_id")
        if isinstance(task_id, str) and connection.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone():
            materialize_revision(connection, revision)
    return counts, mirror_updates, daily_source_updates


def _sync_upload_assets(transport: SyncTransport, cipher: AccountCipher) -> tuple[int, list[str]]:
    uploaded = 0
    errors: list[str] = []
    with connect() as connection:
        rows = connection.execute(
            """SELECT a.* FROM managed_assets a
               LEFT JOIN sync_asset_state s ON s.asset_id=a.id
               WHERE a.unresolved=0 AND a.relative_path IS NOT NULL AND COALESCE(s.uploaded,0)=0
               ORDER BY a.created_at,a.id"""
        ).fetchall()
    for row in rows:
        try:
            path = resolve_data_path(row["relative_path"])
            if not path.is_file():
                raise ValidationError("受管资产文件缺失")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != row["sha256"] or path.stat().st_size != row["byte_count"]:
                raise ValidationError("受管资产哈希或大小不一致")
            blob_id = cipher.blob_id(row["id"])
            chunk_count = max(1, math.ceil(row["byte_count"] / BLOB_CHUNK_BYTES))
            state = transport.create_blob(
                blob_id, int(row["byte_count"]), chunk_count, cipher.key_version
            )
            if not state.get("complete"):
                with path.open("rb") as stream:
                    for index in range(chunk_count):
                        plaintext = stream.read(BLOB_CHUNK_BYTES)
                        value = cipher.seal_blob_chunk(blob_id, index, chunk_count, plaintext)
                        transport.upload_blob_chunk(blob_id, index, value)
                transport.complete_blob(blob_id)
            with connect() as connection:
                connection.execute(
                    """INSERT INTO sync_asset_state(asset_id,blob_id,uploaded,downloaded,updated_at)
                       VALUES(?,?,1,1,?) ON CONFLICT(asset_id) DO UPDATE SET
                       blob_id=excluded.blob_id,uploaded=1,downloaded=1,updated_at=excluded.updated_at""",
                    (row["id"], blob_id, utc_now()),
                )
            uploaded += 1
        except ValidationError as exc:
            errors.append(f"{row['id']}: {exc}")
    return uploaded, errors


def _restore_asset_references(connection: sqlite3.Connection, asset_id: str, relative_path: str) -> None:
    for row in connection.execute(
        """SELECT h.entity_kind,r.payload_json FROM entity_heads h
           JOIN domain_revisions r ON r.revision_id=h.revision_id
           WHERE h.entity_kind IN ('task_input','food_item')"""
    ):
        payload = json.loads(row["payload_json"])
        if row["entity_kind"] == "task_input":
            if payload.get("asset_id") == asset_id:
                connection.execute(
                    "UPDATE tasks SET image_path=? WHERE id=?", (relative_path, payload["task_id"])
                )
        else:
            food = payload.get("food") or {}
            if food.get("package_photo_asset_id") == asset_id:
                connection.execute(
                    "UPDATE food_items SET package_photo_path=? WHERE id=?",
                    (relative_path, food["id"]),
                )


def _sync_download_assets(transport: SyncTransport, cipher: AccountCipher) -> tuple[int, list[str]]:
    downloaded = 0
    errors: list[str] = []
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM managed_assets WHERE unresolved=1 OR relative_path IS NULL ORDER BY created_at,id"
        ).fetchall()
    for row in rows:
        try:
            blob_id = cipher.blob_id(row["id"])
            chunk_count = max(1, math.ceil(row["byte_count"] / BLOB_CHUNK_BYTES))
            plaintext_parts: list[bytes] = []
            missing = False
            for index in range(chunk_count):
                value = transport.download_blob_chunk(blob_id, index)
                if value is None:
                    missing = True
                    break
                plaintext_parts.append(cipher.open_blob_chunk(blob_id, index, chunk_count, value))
            if missing:
                continue
            plaintext = b"".join(plaintext_parts)
            if len(plaintext) != row["byte_count"] or hashlib.sha256(plaintext).hexdigest() != row["sha256"]:
                raise ValidationError("下载资产哈希或大小不一致")
            target = managed_asset_root() / f"{row['sha256']}{row['extension']}"
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(plaintext)
            os.replace(temporary, target)
            relative = target.relative_to(app_home()).as_posix()
            with connect() as connection:
                connection.execute(
                    "UPDATE managed_assets SET relative_path=?,external_reference=NULL,unresolved=0 WHERE id=?",
                    (relative, row["id"]),
                )
                connection.execute(
                    """INSERT INTO sync_asset_state(asset_id,blob_id,uploaded,downloaded,updated_at)
                       VALUES(?,?,0,1,?) ON CONFLICT(asset_id) DO UPDATE SET
                       blob_id=excluded.blob_id,downloaded=1,updated_at=excluded.updated_at""",
                    (row["id"], blob_id, utc_now()),
                )
                _restore_asset_references(connection, row["id"], relative)
            downloaded += 1
        except ValidationError as exc:
            errors.append(f"{row['id']}: {exc}")
    return downloaded, errors


def sync_now(
    transport: SyncTransport | None = None, *, include_on_demand_media: bool = False
) -> dict:
    init_db()
    with connect() as connection:
        config = connection.execute(
            "SELECT * FROM sync_configuration WHERE singleton=1"
        ).fetchone()
        if not config or not config["enabled"]:
            raise ValidationError("同步尚未启用")
        account_data_key = get_secret("sync.account_data_key", binary=True)
        if not isinstance(account_data_key, bytes):
            raise ValidationError("当前会话没有 Account Data Key；请重新解锁同步")
        cipher = AccountCipher(config["account_id"], account_data_key, config["key_version"])
        server_url = config["server_url"]
        media_policy = config["media_policy"]
    active_transport = transport or HttpSyncTransport(server_url)
    capabilities = _validate_capabilities(
        active_transport.capabilities()
        if hasattr(active_transport, "capabilities")
        else {
            "protocol": "mealcircuit.sync", "min_version": 1, "max_version": 1,
            "max_batch": 100, "max_pull": 500, "e2ee_required": True,
        }
    )
    batch_limit = max(1, min(100, int(capabilities.get("max_batch", 100))))
    pull_limit = max(1, min(500, int(capabilities.get("max_pull", 500))))
    summary = {
        "pushed": 0,
        "accepted": 0,
        "applied": 0,
        "merged": 0,
        "conflicts": 0,
        "unknown_schema_entities": 0,
        "assets_uploaded": 0,
        "assets_downloaded": 0,
        "asset_errors": [],
        "requeued_reviews": [],
        "cursor": 0,
    }
    mirror_updates: list[DomainRevision] = []
    remote_daily_source_updates: list[DomainRevision] = []
    for _ in range(1000):
        operations = prepare_outbox(batch_limit)
        if not operations:
            break
        pushed = active_transport.push(operations)
        with connect() as connection:
            counts = _process_push(connection, cipher, pushed)
        summary["pushed"] += len(operations)
        summary["accepted"] += counts["accepted"]
        summary["merged"] += counts["merged"]
        summary["conflicts"] += counts["conflicts"]
    else:
        raise ValidationError("同步 outbox 超过安全批次上限")
    with connect() as connection:
        cursor_row = connection.execute(
            "SELECT cursor_value FROM sync_cursor WHERE scope='account'"
        ).fetchone()
        cursor = int(cursor_row[0]) if cursor_row else 0
    snapshot_offset = 0
    full_resync = False
    for _ in range(100):
        pulled = active_transport.pull(cursor, limit=pull_limit, snapshot_offset=snapshot_offset)
        full_resync = full_resync or bool(pulled.get("requires_full_resync"))
        with connect() as connection:
            counts, updates, daily_source_updates = _process_pull(connection, cipher, pulled)
            next_cursor = int(pulled.get("cursor", cursor))
            if next_cursor < cursor:
                raise ValidationError("同步服务游标倒退")
            connection.execute(
                """INSERT INTO sync_cursor(scope,cursor_value,updated_at) VALUES('account',?,?)
                   ON CONFLICT(scope) DO UPDATE SET cursor_value=excluded.cursor_value,updated_at=excluded.updated_at""",
                (next_cursor, utc_now()),
            )
        mirror_updates.extend(updates)
        remote_daily_source_updates.extend(daily_source_updates)
        summary["applied"] += counts["applied"]
        summary["merged"] += counts["merged"]
        summary["conflicts"] += counts["conflicts"]
        summary["unknown_schema_entities"] += counts["unknown"]
        cursor = next_cursor
        if not pulled.get("has_more"):
            break
        snapshot_offset = int(pulled.get("snapshot_offset", 0))
    else:
        raise ValidationError("同步分页超过安全上限")
    summary["requeued_reviews"] = _queue_remote_daily_source_updates(remote_daily_source_updates)
    active_transport.ack(cursor)
    uploaded, upload_errors = _sync_upload_assets(active_transport, cipher)
    if media_policy == "on_demand" and not include_on_demand_media:
        downloaded, download_errors = 0, []
    else:
        downloaded, download_errors = _sync_download_assets(active_transport, cipher)
    summary["assets_uploaded"] = uploaded
    summary["assets_downloaded"] = downloaded
    summary["asset_errors"] = upload_errors + download_errors
    if mirror_updates:
        from .portable import _write_preferences

        _write_preferences(mirror_updates)
    summary["cursor"] = cursor
    summary["full_resync"] = full_resync
    return summary


_ROTATION_KEY = "sync.rotation.account_data_key"
_ROTATION_RECOVERY = "sync.rotation.recovery_key"
_ROTATION_ENVELOPE = "sync.rotation.recovery_envelope"
_ROTATION_VERSION = "sync.rotation.key_version"


def _rotation_material(account_id: str, target: int) -> dict:
    stored_version = get_secret(_ROTATION_VERSION)
    if stored_version is not None:
        try:
            version = int(str(stored_version))
        except ValueError as exc:
            raise ValidationError("暂存的密钥轮换版本损坏；请中止轮换") from exc
        if version != target:
            raise ValidationError("暂存的密钥轮换目标与服务端不一致；请中止轮换")
        data_key = get_secret(_ROTATION_KEY, binary=True)
        recovery_key = get_secret(_ROTATION_RECOVERY)
        envelope_text = get_secret(_ROTATION_ENVELOPE)
        if not isinstance(data_key, bytes) or not isinstance(recovery_key, str) or not isinstance(envelope_text, str):
            raise ValidationError("暂存的密钥轮换材料不完整；请中止轮换")
        try:
            envelope = json.loads(envelope_text)
        except json.JSONDecodeError as exc:
            raise ValidationError("暂存的恢复密钥包损坏；请中止轮换") from exc
        return {"account_data_key": data_key, "recovery_key": recovery_key, "recovery_envelope": envelope}
    material = create_key_material(account_id, target)
    set_secret(_ROTATION_KEY, material["account_data_key"])
    set_secret(_ROTATION_RECOVERY, material["recovery_key"])
    set_secret(
        _ROTATION_ENVELOPE,
        json.dumps(material["recovery_envelope"], sort_keys=True, separators=(",", ":")),
    )
    set_secret(_ROTATION_VERSION, str(target))
    return material


def _clear_rotation_material() -> None:
    for name in (_ROTATION_KEY, _ROTATION_RECOVERY, _ROTATION_ENVELOPE, _ROTATION_VERSION):
        delete_secret(name)


def _rotation_inventory() -> tuple[list[DomainRevision], list[sqlite3.Row]]:
    with connect() as connection:
        revisions = [
            _revision_from_row(row)
            for row in connection.execute(
                """SELECT r.* FROM entity_heads h
                   JOIN domain_revisions r ON r.revision_id=h.revision_id
                   ORDER BY h.entity_kind,h.entity_id"""
            )
        ]
        assets = connection.execute(
            """SELECT * FROM managed_assets
               WHERE unresolved=0 AND relative_path IS NOT NULL ORDER BY created_at,id"""
        ).fetchall()
    return revisions, assets


def _push_rotation_revisions(
    transport: HttpSyncTransport,
    cipher: AccountCipher,
    revisions: list[DomainRevision],
) -> None:
    capabilities = _validate_capabilities(
        transport.capabilities() if hasattr(transport, "capabilities") else {
            "protocol": "mealcircuit.sync", "min_version": 1, "max_version": 1,
            "max_batch": 100, "max_pull": 500, "e2ee_required": True,
        }
    )
    batch_size = max(1, min(100, int(capabilities.get("max_batch", 100))))
    for offset in range(0, len(revisions), batch_size):
        batch = revisions[offset : offset + batch_size]
        operations: list[dict] = []
        by_operation: dict[str, DomainRevision] = {}
        for revision in batch:
            envelope = cipher.seal(revision)
            digest = hashlib.sha256(
                f"rotation:{cipher.account_id}:{cipher.key_version}:{revision.revision_id}".encode("utf-8")
            ).hexdigest()
            op_id = f"op_{digest}"
            operation = {
                "op_id": op_id,
                "remote_id": envelope["remote_id"],
                "base_server_version": 0,
                "key_version": cipher.key_version,
                "envelope": envelope,
            }
            operations.append(operation)
            by_operation[op_id] = revision
        payload = transport.push(operations)
        replacements: list[dict] = []
        for result in payload.get("results") or []:
            if result.get("status") == "accepted":
                continue
            revision = by_operation.get(str(result.get("op_id")))
            if revision is None or result.get("status") != "conflict" or not result.get("envelope"):
                raise ValidationError("密钥轮换实体上传返回无效状态")
            remote_id = str(result.get("remote_id") or "")
            staged = cipher.open(remote_id, result["envelope"])
            if staged.revision_id == revision.revision_id:
                continue
            server_version = int(result.get("server_version", 0))
            envelope = cipher.seal(revision)
            digest = hashlib.sha256(
                f"rotation:{cipher.account_id}:{cipher.key_version}:{revision.revision_id}:{server_version}".encode("utf-8")
            ).hexdigest()
            replacements.append(
                {
                    "op_id": f"op_{digest}",
                    "remote_id": envelope["remote_id"],
                    "base_server_version": server_version,
                    "key_version": cipher.key_version,
                    "envelope": envelope,
                }
            )
        if replacements:
            retried = transport.push(replacements)
            if any(item.get("status") != "accepted" for item in retried.get("results") or []):
                raise ValidationError("密钥轮换期间本地实体继续变化；请重试")


def _upload_rotation_assets(
    transport: HttpSyncTransport,
    cipher: AccountCipher,
    assets: list[sqlite3.Row],
) -> dict[str, str]:
    uploaded: dict[str, str] = {}
    for row in assets:
        path = resolve_data_path(row["relative_path"])
        if not path.is_file() or path.stat().st_size != row["byte_count"]:
            raise ValidationError(f"密钥轮换无法读取资产：{row['id']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValidationError(f"密钥轮换资产哈希不一致：{row['id']}")
        blob_id = cipher.blob_id(row["id"])
        chunk_count = max(1, math.ceil(row["byte_count"] / BLOB_CHUNK_BYTES))
        state = transport.create_blob(blob_id, int(row["byte_count"]), chunk_count, cipher.key_version)
        if not state.get("complete"):
            with path.open("rb") as stream:
                for index in range(chunk_count):
                    value = cipher.seal_blob_chunk(blob_id, index, chunk_count, stream.read(BLOB_CHUNK_BYTES))
                    transport.upload_blob_chunk(blob_id, index, value)
            transport.complete_blob(blob_id)
        uploaded[row["id"]] = blob_id
    return uploaded


def _finalize_local_rotation(target: int, material: dict, asset_ids: dict[str, str]) -> None:
    set_secret("sync.account_data_key", material["account_data_key"])
    with connect() as connection:
        connection.execute(
            "UPDATE sync_configuration SET key_version=?,updated_at=? WHERE singleton=1",
            (target, utc_now()),
        )
        connection.execute("DELETE FROM sync_outbox")
        connection.execute("DELETE FROM sync_shadow")
        connection.execute("DELETE FROM sync_cursor")
        connection.execute("DELETE FROM sync_asset_state")
        for asset_id, blob_id in asset_ids.items():
            connection.execute(
                """INSERT INTO sync_asset_state(asset_id,blob_id,uploaded,downloaded,updated_at)
                   VALUES(?,?,1,1,?)""",
                (asset_id, blob_id, utc_now()),
            )


def prepare_account_key_rotation(transport: HttpSyncTransport | None = None) -> dict:
    """Lock a new key epoch and stage recoverable material in secure storage."""
    init_db()
    status = sync_status()
    if not status.get("enabled"):
        raise ValidationError("同步尚未启用")
    active_transport = transport or HttpSyncTransport(str(status["server_url"]))
    remote = active_transport.key_rotation_status()
    staged_version = get_secret(_ROTATION_VERSION)
    if remote.get("in_progress") and not remote.get("owned_by_current_device"):
        raise ValidationError("另一台设备正在执行安全轮换")
    if not remote.get("in_progress") and staged_version is None:
        sync_now(active_transport)
        status = sync_status()
        if status["pending"] or status["conflicts"] or status["unknown_schema_entities"]:
            raise ValidationError("轮换前必须清空待上传、冲突和未知 schema 实体")
        remote = active_transport.begin_key_rotation()
    target = remote.get("target_key_version")
    if not remote.get("in_progress") and staged_version is not None:
        target = int(str(staged_version))
        if int(remote.get("active_key_version", 0)) != target:
            raise ValidationError("服务端轮换状态与本机暂存密钥不一致")
    if not isinstance(target, int) or target <= 1:
        raise ValidationError("同步服务没有返回有效轮换版本")
    material = _rotation_material(str(status["account_id"]), target)
    return {"key_version": target, "recovery_key": material["recovery_key"]}


def confirm_account_key_rotation(
    recovery_key: str,
    transport: HttpSyncTransport | None = None,
) -> dict:
    """Confirm the displayed recovery key, upload the new epoch, and commit it."""
    init_db()
    status = sync_status()
    if not status.get("enabled"):
        raise ValidationError("同步尚未启用")
    active_transport = transport or HttpSyncTransport(str(status["server_url"]))
    remote = active_transport.key_rotation_status()
    staged_version = get_secret(_ROTATION_VERSION)
    if staged_version is None:
        raise ValidationError("没有待确认的密钥轮换")
    target = int(str(staged_version))
    if remote.get("in_progress"):
        if not remote.get("owned_by_current_device") or int(remote.get("target_key_version", 0)) != target:
            raise ValidationError("服务端轮换锁不属于本设备")
    elif int(remote.get("active_key_version", 0)) != target:
        raise ValidationError("服务端轮换状态与本机暂存密钥不一致")
    material = _rotation_material(str(status["account_id"]), target)
    if str(recovery_key or "").strip().upper() != material["recovery_key"]:
        raise ValidationError("新恢复密钥确认失败；轮换仍处于待确认状态")
    revisions, assets = _rotation_inventory()
    cipher = AccountCipher(str(status["account_id"]), material["account_data_key"], target)
    asset_ids = {row["id"]: cipher.blob_id(row["id"]) for row in assets}
    commit_result: dict
    if remote.get("in_progress"):
        _push_rotation_revisions(active_transport, cipher, revisions)
        asset_ids = _upload_rotation_assets(active_transport, cipher, assets)
        body = {
            "key_version": target,
            "recovery_envelope": material["recovery_envelope"],
            "entity_count": len(revisions),
            "blob_count": len(assets),
        }
        try:
            commit_result = active_transport.commit_key_rotation(body)
        except ValidationError:
            commit_result = active_transport.commit_key_rotation(body)
    else:
        commit_result = {"active_key_version": target, "already_committed": True}
    _finalize_local_rotation(target, material, asset_ids)
    recovery_key = material["recovery_key"]
    _clear_rotation_material()
    post_sync = sync_now(active_transport)
    return {
        "key_version": target,
        "recovery_key": recovery_key,
        "other_devices_revoked": int(commit_result.get("revoked_devices", 0)),
        "resumed_after_commit": bool(commit_result.get("already_committed")),
        "sync": post_sync,
    }


def rotate_account_key(
    confirm_recovery_key: Callable[[str], bool],
    transport: HttpSyncTransport | None = None,
) -> dict:
    """Interactive convenience wrapper used by the desktop CLI."""
    prepared = prepare_account_key_rotation(transport)
    if not confirm_recovery_key(prepared["recovery_key"]):
        abort_account_key_rotation(transport)
        raise ValidationError("新恢复密钥确认失败；密钥轮换已中止")
    return confirm_account_key_rotation(prepared["recovery_key"], transport)


def abort_account_key_rotation(transport: HttpSyncTransport | None = None) -> dict:
    status = sync_status()
    if not status.get("enabled"):
        raise ValidationError("同步尚未启用")
    active_transport = transport or HttpSyncTransport(str(status["server_url"]))
    remote = active_transport.key_rotation_status()
    if remote.get("in_progress"):
        if not remote.get("owned_by_current_device"):
            raise ValidationError("不能中止另一台设备发起的轮换")
        active_transport.abort_key_rotation()
    _clear_rotation_material()
    return {"aborted": True, "local_data_preserved": True}


def list_conflicts() -> list[dict]:
    init_db()
    with connect() as connection:
        return [
            {
                **dict(row),
                "base_revision": json.loads(row["base_revision_json"]) if row["base_revision_json"] else None,
                "local_revision": json.loads(row["local_revision_json"]),
                "remote_revision": json.loads(row["remote_revision_json"]),
                "conflicting_paths": json.loads(row["conflicting_paths_json"]),
            }
            for row in connection.execute(
                "SELECT * FROM sync_conflicts WHERE status='unresolved' ORDER BY created_at,id"
            )
        ]


def resolve_conflict(conflict_id: str, choice: str) -> dict:
    if choice not in {"local", "remote"}:
        raise ValidationError("冲突选择只能是 local 或 remote")
    init_db()
    mirror: DomainRevision | None = None
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM sync_conflicts WHERE id=? AND status='unresolved'", (conflict_id,)
        ).fetchone()
        if row is None:
            raise KeyError(conflict_id)
        local = validate_revision(json.loads(row["local_revision_json"]))
        remote = validate_revision(json.loads(row["remote_revision_json"]))
        selected = local if choice == "local" else remote
        device_id = connection.execute(
            "SELECT value FROM app_metadata WHERE key='device_id'"
        ).fetchone()[0]
        if local.entity_id != remote.entity_id and _logical_key(local) == _logical_key(remote):
            remote_version = _shadow_version(connection, remote.entity_id)
            resolved = _commit_logical_merge(
                connection,
                local=local,
                remote=remote,
                payload=selected.payload,
                remote_server_version=remote_version,
                deleted=selected.deleted,
            )
        else:
            resolved = make_revision(
                local.entity_kind,
                selected.payload,
                entity_id=local.entity_id,
                parent_revision_ids=[local.revision_id, remote.revision_id],
                author_device_id=device_id,
                deleted=selected.deleted,
            )
            from .domain_store import materialize_revision

            _store_revision(connection, resolved)
            _set_head(connection, resolved)
            materialize_revision(connection, resolved)
            shadow = connection.execute(
                "SELECT server_version FROM sync_shadow WHERE entity_id=?", (local.entity_id,)
            ).fetchone()
            _queue_revision(connection, resolved, int(shadow[0]) if shadow else 0)
        connection.execute(
            "UPDATE sync_conflicts SET status='resolved',resolved_at=? WHERE id=?",
            (utc_now(), conflict_id),
        )
        connection.execute(
            "UPDATE entity_heads SET conflicted=0 WHERE entity_id IN (?,?)",
            (local.entity_id, remote.entity_id),
        )
        if resolved.entity_kind == "preferences":
            mirror = resolved
    if mirror:
        from .portable import _write_preferences

        _write_preferences([mirror])
    return {"id": conflict_id, "status": "resolved", "choice": choice, "revision_id": resolved.revision_id}
