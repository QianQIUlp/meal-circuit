from __future__ import annotations

import io
import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401

    from sync_server.app import create_app
except ImportError:
    TestClient = None

from mealcircuit import personalization, service
from mealcircuit import secret_store as secret_store_module
from mealcircuit import sync as sync_module
from mealcircuit.configuration import initialize_private_home, load_resolved_settings
from mealcircuit.crypto import encrypt
from mealcircuit.db import connect, init_db
from mealcircuit.domain import make_revision
from mealcircuit.secret_store import delete_secret, get_secret, set_secret
from mealcircuit.storage import resolve_data_path
from mealcircuit.validation import ValidationError
from mealcircuit.sync import (
    AccountCipher,
    configure_sync,
    create_key_material,
    list_conflicts,
    recover_account_data_key,
    resolve_conflict,
    rotate_account_key,
    sync_now,
    sync_status,
)


SETTINGS = {
    "schema_version": 1,
    "timezone": "UTC",
    "meal_environment": "同步测试",
    "protein_target_g": [90, 120],
    "portion_method": "测试份量",
    "missing_training_default": "保持未知",
    "compensation_boundary": "恢复标准份量",
    "home_cooking": {"enabled": False},
}


def daily_result(review_date: str, line: str) -> dict:
    tomorrow = (date.fromisoformat(review_date) + timedelta(days=1)).isoformat()
    return {
        "system_status": "observe",
        "facts": ["合成同步事实"],
        "inferences": ["合成同步推断"],
        "core_advice": ["保持三餐并撤掉重复加餐"],
        "do_not_adjust": ["不跳餐、不清零主食"],
        "risk_signals": [],
        "priority_food_decisions": [],
        "tomorrow_menu": {
            "date": tomorrow,
            "environment": "同步测试",
            "protein_target_g": [90, 120],
            "meals": [
                {"name": "早餐", "foods": ["鸡蛋"], "portion_guidance": "标准份", "protein_g": [18, 25], "substitutions": []},
                {"name": "午餐", "foods": ["瘦肉", "米饭", "蔬菜"], "portion_guidance": "标准份", "protein_g": [35, 48], "substitutions": []},
                {"name": "晚餐", "foods": ["鱼", "主食", "蔬菜"], "portion_guidance": "标准份", "protein_g": [37, 50], "substitutions": []},
            ],
            "conditional_snack": {"condition": "三餐后仍有缺口", "options": ["无糖豆浆"]},
            "training_adjustment": "训练日增加一份主食。",
            "gut_adjustment": "不适时降低油辣。",
        },
        "one_line_review": line,
    }


def configure_home(path: Path) -> None:
    os.environ["MEALCIRCUIT_HOME"] = str(path)
    os.environ.pop("MEALCIRCUIT_DB", None)
    initialize_private_home()
    (path / "settings.json").write_text(json.dumps(SETTINGS, ensure_ascii=False), encoding="utf-8")
    (path / "profile.md").write_text("# 同步测试档案\n", encoding="utf-8")
    (path / "doctrine.private.md").write_text("# 同步测试规则\n", encoding="utf-8")


def complete_standard_onboarding() -> None:
    current = personalization.start_onboarding()
    payloads = {
        "welcome": {"privacy_ack": True},
        "goals": {
            "primary_goal": "body_recomposition",
            "secondary_goals": [],
            "motivation": "验证离线生成的两份复盘都能在同步冲突中保留。",
            "success_metrics": ["execution_rate"],
            "target_weight_kg": None,
        },
        "baseline": {
            "age_years": 30,
            "height_cm": 170,
            "weight_kg": 70,
            "physiological_input": "male",
            "activity_level": "moderate",
        },
        "safety": {
            "life_stage": "adult",
            "therapeutic_diet": False,
            "medication_affects_nutrition": False,
            "eating_disorder_risk": False,
            "rapid_unexplained_change": False,
            "severe_persistent_symptoms": False,
            "severe_allergy_management": False,
        },
        "training": {"types": ["strength"], "frequency_per_week": 3},
        "constraints": {
            "meal_environment": SETTINGS["meal_environment"],
            "portion_method": SETTINGS["portion_method"],
            "cooking_time_minutes": 20,
            "equipment": ["stovetop_pan"],
            "food_exclusions": [],
            "preferences": [],
            "question_budget": 2,
        },
    }
    for step, payload in payloads.items():
        current = personalization.save_onboarding_step(
            current["id"], step, payload, current["version"]
        )
    personalization.complete_onboarding(
        current["id"], current["version"],
        {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
    )


class ClientTransport:
    def __init__(self, client: TestClient, access_token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def push(self, operations: list[dict]) -> dict:
        response = self.client.post("/v1/sync/push", headers=self.headers, json={"operations": operations})
        response.raise_for_status()
        return response.json()

    def pull(self, cursor: int, limit: int = 500, snapshot_offset: int = 0) -> dict:
        response = self.client.get(
            "/v1/sync/pull",
            headers=self.headers,
            params={"cursor": cursor, "limit": limit, "snapshot_offset": snapshot_offset},
        )
        response.raise_for_status()
        return response.json()

    def ack(self, cursor: int) -> dict:
        response = self.client.post("/v1/sync/ack", headers=self.headers, json={"cursor": cursor})
        response.raise_for_status()
        return response.json()

    def create_blob(self, blob_id: str, byte_count: int, chunk_count: int, key_version: int) -> dict:
        response = self.client.post(
            "/v1/blobs",
            headers=self.headers,
            json={
                "blob_id": blob_id,
                "byte_count": byte_count,
                "chunk_count": chunk_count,
                "key_version": key_version,
            },
        )
        response.raise_for_status()
        return response.json()

    def upload_blob_chunk(self, blob_id: str, index: int, value: bytes) -> None:
        response = self.client.put(
            f"/v1/blobs/{blob_id}/chunks/{index}", headers=self.headers, content=value
        )
        response.raise_for_status()

    def complete_blob(self, blob_id: str) -> dict:
        response = self.client.post(f"/v1/blobs/{blob_id}/complete", headers=self.headers)
        response.raise_for_status()
        return response.json()

    def download_blob_chunk(self, blob_id: str, index: int) -> bytes | None:
        response = self.client.get(f"/v1/blobs/{blob_id}/chunks/{index}", headers=self.headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content

    def key_rotation_status(self) -> dict:
        response = self.client.get("/v1/key-rotations/current", headers=self.headers)
        response.raise_for_status()
        return response.json()

    def begin_key_rotation(self) -> dict:
        response = self.client.post("/v1/key-rotations", headers=self.headers, json={})
        response.raise_for_status()
        return response.json()

    def abort_key_rotation(self) -> None:
        response = self.client.delete("/v1/key-rotations/current", headers=self.headers)
        response.raise_for_status()

    def commit_key_rotation(self, body: dict) -> dict:
        response = self.client.post("/v1/key-rotations/current/commit", headers=self.headers, json=body)
        response.raise_for_status()
        return response.json()


class TransportFault:
    """Delegate every protocol method except the fault explicitly injected by a test."""

    def __init__(self, inner: ClientTransport):
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class LoseFirstPushResponse(TransportFault):
    def __init__(self, inner: ClientTransport):
        super().__init__(inner)
        self.lost = False

    def push(self, operations: list[dict]) -> dict:
        response = self.inner.push(operations)
        if not self.lost:
            self.lost = True
            raise ConnectionError("synthetic response loss after server commit")
        return response


class LoseFirstAckResponse(TransportFault):
    def __init__(self, inner: ClientTransport):
        super().__init__(inner)
        self.lost = False

    def ack(self, cursor: int) -> dict:
        response = self.inner.ack(cursor)
        if not self.lost:
            self.lost = True
            raise ConnectionError("synthetic acknowledgement response loss")
        return response


class ReversePullOrder(TransportFault):
    def pull(self, cursor: int, limit: int = 500, snapshot_offset: int = 0) -> dict:
        response = self.inner.pull(cursor, limit=limit, snapshot_offset=snapshot_offset)
        return {**response, "changes": list(reversed(response.get("changes", [])))}


class MemorySyncSecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes | str] = {}
        self.fail_next_write = False
        self.fail_deletes: set[str] = set()

    def set(self, name: str, value: bytes | str) -> str:
        if self.fail_next_write:
            self.fail_next_write = False
            raise secret_store_module.SecretStorageError(
                "synthetic Windows Credential Manager failure"
            )
        self.values[name] = value
        return "system"

    def get(self, name: str, *, binary: bool = False):
        value = self.values.get(name)
        if binary:
            return value if isinstance(value, bytes) else None
        return value if isinstance(value, str) else None

    def delete(self, name: str) -> bool:
        if name in self.fail_deletes:
            return False
        self.values.pop(name, None)
        return True


class FakeHttpResponse:
    def __init__(self, payload: bytes, *, content_length: int | str | None = None) -> None:
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.stream = io.BytesIO(payload)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.stream.read(size)

    def close(self) -> None:
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class WindowsSecurityBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_home = os.environ.get("MEALCIRCUIT_HOME")
        self.old_db = os.environ.get("MEALCIRCUIT_DB")
        with secret_store_module._LOCK:
            secret_store_module._SESSION.clear()

    def tearDown(self) -> None:
        with secret_store_module._LOCK:
            secret_store_module._SESSION.clear()
        if self.old_home is None:
            os.environ.pop("MEALCIRCUIT_HOME", None)
        else:
            os.environ["MEALCIRCUIT_HOME"] = self.old_home
        if self.old_db is None:
            os.environ.pop("MEALCIRCUIT_DB", None)
        else:
            os.environ["MEALCIRCUIT_DB"] = self.old_db
        self.temp.cleanup()

    def configure_test_home(self, name: str) -> None:
        os.environ["MEALCIRCUIT_HOME"] = str(self.root / name)
        os.environ.pop("MEALCIRCUIT_DB", None)
        initialize_private_home()
        init_db()

    def secret_patches(self, store: MemorySyncSecretStore):
        return patch.multiple(
            sync_module,
            set_secret=store.set,
            get_secret=store.get,
            delete_secret=store.delete,
        )

    @staticmethod
    def seed_legacy_credentials(store: MemorySyncSecretStore) -> None:
        store.values.update(
            {
                "sync.account_data_key": b"o" * 32,
                "sync.access_token": "old-access",
                "sync.refresh_token": "old-refresh",
            }
        )

    def test_sync_http_rejects_redirect_without_forwarding_authorization(self) -> None:
        requests: list[tuple[str, str | None]] = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_address[1]}/target",
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = b'{"followed":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(sync_module.SyncHttpError) as raised:
                sync_module._http_json(
                    f"http://127.0.0.1:{server.server_address[1]}/start",
                    token="sensitive-token",
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(302, raised.exception.status)
        self.assertEqual([("/start", "Bearer sensitive-token")], requests)

    def test_sync_http_json_prechecks_declared_response_size(self) -> None:
        response = FakeHttpResponse(b"{}", content_length=9)
        with (
            patch.object(sync_module, "MAX_SYNC_JSON_RESPONSE_BYTES", 8),
            patch.object(sync_module, "urlopen", return_value=response),
            self.assertRaisesRegex(ValidationError, "响应超过大小限制"),
        ):
            sync_module._http_json("https://sync.example.test/v1/capabilities")
        self.assertEqual([], response.read_sizes)

    def test_sync_http_binary_stream_read_is_bounded(self) -> None:
        response = FakeHttpResponse(b"123456789")
        with (
            patch.object(sync_module, "MAX_SYNC_BINARY_RESPONSE_BYTES", 8),
            patch.object(sync_module, "urlopen", return_value=response),
            self.assertRaisesRegex(ValidationError, "响应超过大小限制"),
        ):
            sync_module._http_bytes("https://sync.example.test/v1/blob")
        self.assertEqual([9], response.read_sizes)

    def test_sync_http_error_body_stream_read_is_bounded(self) -> None:
        response = FakeHttpResponse(b'{"x":123}')
        error = HTTPError(
            "https://sync.example.test/v1/failure",
            400,
            "Bad Request",
            response.headers,
            response,
        )
        with (
            patch.object(sync_module, "MAX_SYNC_ERROR_RESPONSE_BYTES", 8),
            patch.object(sync_module, "urlopen", side_effect=error),
            self.assertRaisesRegex(ValidationError, "响应超过大小限制"),
        ):
            sync_module._http_json("https://sync.example.test/v1/failure")
        self.assertEqual([9], response.read_sizes)

    def test_sync_http_limits_preserve_valid_json_and_binary_responses(self) -> None:
        json_response = FakeHttpResponse(b'{"ok":true}', content_length=11)
        binary_response = FakeHttpResponse(b"ciphertext", content_length=10)
        with patch.object(
            sync_module,
            "urlopen",
            side_effect=[json_response, binary_response],
        ):
            self.assertEqual(
                {"ok": True},
                sync_module._http_json("https://sync.example.test/v1/status"),
            )
            self.assertEqual(
                b"ciphertext",
                sync_module._http_bytes("https://sync.example.test/v1/blob"),
            )
        self.assertNotIn(-1, json_response.read_sizes)
        self.assertNotIn(-1, binary_response.read_sizes)

    def test_staged_credential_slot_is_registered_for_crash_cleanup(self) -> None:
        self.configure_test_home("credential-orphan-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            slot, backend = sync_module._stage_sync_credentials(
                b"k" * 32,
                "access",
                "refresh",
            )
            self.assertEqual("system", backend)
            with connect() as connection:
                self.assertEqual(
                    [slot],
                    sync_module._obsolete_credential_slots(connection),
                )
            secret_name = f"{sync_module._SYNC_CREDENTIAL_PREFIX}{slot}"
            self.assertIn(secret_name, store.values)
            self.assertEqual([], sync_module._cleanup_obsolete_credential_slots())
            self.assertNotIn(secret_name, store.values)

    def test_rotation_finalization_requeues_heads_created_during_network_window(self) -> None:
        self.configure_test_home("rotation-network-window-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="account",
                device_name="desktop",
                account_data_key=b"o" * 32,
                access_token="access",
                refresh_token="refresh",
            )
            service.create_material_task("before rotation inventory")
            with connect() as connection:
                connection.execute("DELETE FROM sync_outbox")
            inventory, _ = sync_module._rotation_inventory()

            created_during_network = service.create_material_task("created during rotation network wait")
            with connect() as connection:
                created_head = connection.execute(
                    "SELECT revision_id FROM entity_heads WHERE entity_id=?",
                    (created_during_network["id"],),
                ).fetchone()[0]
            self.assertNotIn(created_head, {item.revision_id for item in inventory})

            sync_module._finalize_local_rotation(
                2,
                {"account_data_key": b"n" * 32},
                {},
            )

            with connect() as connection:
                heads = {
                    row["entity_id"]: row["revision_id"]
                    for row in connection.execute(
                        "SELECT entity_id,revision_id FROM entity_heads"
                    )
                }
                queued = {
                    row["entity_id"]: row["revision_id"]
                    for row in connection.execute(
                        "SELECT entity_id,revision_id FROM sync_outbox WHERE state='pending'"
                    )
                }
                queued_epochs = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT key_version FROM sync_outbox WHERE state='pending'"
                    )
                }
                encrypted_count = connection.execute(
                    "SELECT COUNT(*) FROM sync_outbox WHERE encrypted_envelope IS NOT NULL"
                ).fetchone()[0]
            self.assertEqual(heads, queued)
            self.assertEqual(created_head, queued[created_during_network["id"]])
            self.assertEqual({2}, queued_epochs)
            self.assertEqual(0, encrypted_count)

    def test_sync_rejects_managed_asset_paths_outside_private_home(self) -> None:
        self.configure_test_home("asset-read-boundary-home")
        payload = b"private file outside app home"
        outside = self.root / "outside-private.bin"
        outside.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        with connect() as connection:
            connection.execute(
                """INSERT INTO managed_assets(
                       id,sha256,media_type,extension,byte_count,relative_path,unresolved,created_at
                   ) VALUES(?,?,?,?,?,?,0,?)""",
                (
                    "asset_outside_home",
                    digest,
                    "application/octet-stream",
                    ".bin",
                    len(payload),
                    str(outside),
                    "2026-07-31T00:00:00Z",
                ),
            )
            row = connection.execute(
                "SELECT * FROM managed_assets WHERE id='asset_outside_home'"
            ).fetchone()

        class RejectingTransport:
            def create_blob(self, *args, **kwargs):
                raise AssertionError("unsafe asset path reached network transport")

        cipher = AccountCipher("account", b"k" * 32)
        uploaded, errors = sync_module._sync_upload_assets(RejectingTransport(), cipher)
        self.assertEqual(0, uploaded)
        self.assertTrue(errors)
        with self.assertRaisesRegex(ValidationError, "受管目录"):
            sync_module._upload_rotation_assets(RejectingTransport(), cipher, [row])

    def test_sync_credentials_activate_as_one_copy_on_write_slot(self) -> None:
        self.configure_test_home("activate-home")
        store = MemorySyncSecretStore()
        self.seed_legacy_credentials(store)
        with self.secret_patches(store):
            result = configure_sync(
                server_url="https://sync.example.test",
                account_id="new-account",
                device_name="windows-desktop",
                account_data_key=b"n" * 32,
                access_token="new-access",
                refresh_token="new-refresh",
            )
            with connect() as connection:
                slot = connection.execute(
                    "SELECT value FROM app_metadata WHERE key=?",
                    (sync_module._SYNC_ACTIVE_CREDENTIAL_SLOT,),
                ).fetchone()[0]
                self.assertEqual([], sync_module._obsolete_credential_slots(connection))
            self.assertEqual("system", result["key_backend"])
            self.assertEqual(
                {
                    "account_data_key": b"n" * 32,
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                },
                sync_module._current_sync_credentials(),
            )
        self.assertIn(f"{sync_module._SYNC_CREDENTIAL_PREFIX}{slot}", store.values)
        self.assertFalse(any(name in store.values for name in sync_module._SYNC_LEGACY_CREDENTIALS))

    def test_configure_write_failure_preserves_legacy_account_and_database(self) -> None:
        self.configure_test_home("write-failure-home")
        store = MemorySyncSecretStore()
        self.seed_legacy_credentials(store)
        with connect() as connection:
            connection.execute(
                """UPDATE sync_configuration SET enabled=1,server_url=?,account_id=?,device_name=?
                   WHERE singleton=1""",
                ("https://old.example.test", "old-account", "old-device"),
            )
        store.fail_next_write = True
        with (
            self.secret_patches(store),
            self.assertRaises(secret_store_module.SecretStorageError),
        ):
            configure_sync(
                server_url="https://new.example.test",
                account_id="new-account",
                device_name="new-device",
                account_data_key=b"n" * 32,
                access_token="new-access",
                refresh_token="new-refresh",
            )
        with self.secret_patches(store):
            self.assertEqual("old-account", sync_status()["account_id"])
            self.assertEqual(b"o" * 32, sync_module._get_sync_secret("sync.account_data_key", binary=True))
            self.assertEqual("old-access", sync_module._get_sync_secret("sync.access_token"))
        self.assertFalse(any(name.startswith(sync_module._SYNC_CREDENTIAL_PREFIX) for name in store.values))

    def test_configure_database_failure_discards_unactivated_slot(self) -> None:
        self.configure_test_home("database-failure-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://old.example.test",
                account_id="old-account",
                device_name="old-device",
                account_data_key=b"o" * 32,
                access_token="old-access",
                refresh_token="old-refresh",
            )
            with connect() as connection:
                old_slot = sync_module._active_credential_slot(connection)
        with (
            self.secret_patches(store),
            patch.object(sync_module, "_activate_credential_slot", side_effect=RuntimeError("activation failed")),
            self.assertRaisesRegex(RuntimeError, "activation failed"),
        ):
            configure_sync(
                server_url="https://new.example.test",
                account_id="new-account",
                device_name="new-device",
                account_data_key=b"n" * 32,
                access_token="new-access",
                refresh_token="new-refresh",
            )
        with connect() as connection:
            account_id = connection.execute(
                "SELECT account_id FROM sync_configuration WHERE singleton=1"
            ).fetchone()[0]
            pointer = connection.execute(
                "SELECT value FROM app_metadata WHERE key=?",
                (sync_module._SYNC_ACTIVE_CREDENTIAL_SLOT,),
            ).fetchone()
        self.assertEqual("old-account", account_id)
        self.assertEqual(old_slot, pointer[0])
        with self.secret_patches(store):
            self.assertEqual(
                {
                    "account_data_key": b"o" * 32,
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                },
                sync_module._current_sync_credentials(),
            )
        self.assertEqual(
            [f"{sync_module._SYNC_CREDENTIAL_PREFIX}{old_slot}"],
            [name for name in store.values if name.startswith(sync_module._SYNC_CREDENTIAL_PREFIX)],
        )

    def test_failed_old_slot_cleanup_is_recorded_and_retried(self) -> None:
        self.configure_test_home("cleanup-retry-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://old.example.test",
                account_id="old-account",
                device_name="old-device",
                account_data_key=b"o" * 32,
                access_token="old-access",
                refresh_token="old-refresh",
            )
            with connect() as connection:
                old_slot = sync_module._active_credential_slot(connection)
            old_name = f"{sync_module._SYNC_CREDENTIAL_PREFIX}{old_slot}"
            store.fail_deletes.add(old_name)
            configure_sync(
                server_url="https://new.example.test",
                account_id="new-account",
                device_name="new-device",
                account_data_key=b"n" * 32,
                access_token="new-access",
                refresh_token="new-refresh",
            )
            with connect() as connection:
                self.assertEqual(
                    [old_slot],
                    sync_module._obsolete_credential_slots(connection),
                )
            self.assertIn(old_name, store.values)
            store.fail_deletes.clear()
            self.assertEqual([], sync_module._cleanup_obsolete_credential_slots())
            with connect() as connection:
                self.assertEqual([], sync_module._obsolete_credential_slots(connection))
            self.assertNotIn(old_name, store.values)

    def test_token_refresh_write_failure_keeps_whole_previous_bundle(self) -> None:
        self.configure_test_home("refresh-failure-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="account",
                device_name="desktop",
                account_data_key=b"k" * 32,
                access_token="old-access",
                refresh_token="old-refresh",
            )
            with connect() as connection:
                before_slot = sync_module._active_credential_slot(connection)
            store.fail_next_write = True
            with (
                patch.object(
                    sync_module,
                    "_http_json",
                    return_value={
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                    },
                ),
                self.assertRaises(secret_store_module.SecretStorageError),
            ):
                sync_module.HttpSyncTransport("https://sync.example.test")._refresh()
            with connect() as connection:
                after_slot = sync_module._active_credential_slot(connection)
            self.assertEqual(before_slot, after_slot)
            self.assertEqual(
                {
                    "account_data_key": b"k" * 32,
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                },
                sync_module._current_sync_credentials(),
            )

    def test_token_refresh_replaces_tokens_without_changing_data_key(self) -> None:
        self.configure_test_home("refresh-success-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="account",
                device_name="desktop",
                account_data_key=b"k" * 32,
                access_token="old-access",
                refresh_token="old-refresh",
            )
            with connect() as connection:
                before_slot = sync_module._active_credential_slot(connection)
            with patch.object(
                sync_module,
                "_http_json",
                return_value={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                },
            ):
                self.assertEqual(
                    "new-access",
                    sync_module.HttpSyncTransport("https://sync.example.test")._refresh(),
                )
            with connect() as connection:
                after_slot = sync_module._active_credential_slot(connection)
            self.assertNotEqual(before_slot, after_slot)
            self.assertEqual(
                {
                    "account_data_key": b"k" * 32,
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                },
                sync_module._current_sync_credentials(),
            )

    def test_token_refresh_does_not_cross_an_account_change_during_network_wait(self) -> None:
        self.configure_test_home("refresh-race-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="old-account",
                device_name="desktop",
                account_data_key=b"o" * 32,
                access_token="old-access",
                refresh_token="old-refresh",
            )

            def switch_account_during_refresh(*args, **kwargs):
                configure_sync(
                    server_url="https://sync.example.test",
                    account_id="new-account",
                    device_name="desktop",
                    account_data_key=b"n" * 32,
                    access_token="current-access",
                    refresh_token="current-refresh",
                )
                return {
                    "access_token": "stale-response-access",
                    "refresh_token": "stale-response-refresh",
                }

            with (
                patch.object(sync_module, "_http_json", side_effect=switch_account_during_refresh),
                self.assertRaisesRegex(ValidationError, "令牌刷新期间同步账户已改变"),
            ):
                sync_module.HttpSyncTransport("https://sync.example.test")._refresh()

            self.assertEqual("new-account", sync_status()["account_id"])
            self.assertEqual(
                {
                    "account_data_key": b"n" * 32,
                    "access_token": "current-access",
                    "refresh_token": "current-refresh",
                },
                sync_module._current_sync_credentials(),
            )

    def test_unlink_removes_active_slot_and_legacy_credentials(self) -> None:
        self.configure_test_home("unlink-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="account",
                device_name="desktop",
                account_data_key=b"k" * 32,
                access_token="access",
                refresh_token="refresh",
            )
            store.values["sync.account_data_key"] = b"legacy" * 4
            store.values["sync.access_token"] = "legacy-access"
            store.values["sync.refresh_token"] = "legacy-refresh"
            result = sync_module.unlink_sync()
            self.assertFalse(result["enabled"])
            with connect() as connection:
                self.assertIsNone(sync_module._active_credential_slot(connection))
                self.assertEqual([], sync_module._obsolete_credential_slots(connection))
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT enabled FROM sync_configuration WHERE singleton=1"
                    ).fetchone()[0],
                )
        self.assertFalse(any(name.startswith("sync.") for name in store.values))

    def test_asset_download_locks_only_local_file_and_database_activation(self) -> None:
        self.configure_test_home("asset-lock-home")
        plaintext = b"downloaded asset"
        digest = hashlib.sha256(plaintext).hexdigest()
        with connect() as connection:
            connection.execute(
                """INSERT INTO managed_assets(
                       id,sha256,media_type,extension,byte_count,relative_path,unresolved,created_at
                   ) VALUES(?,?,?,?,?,NULL,1,?)""",
                (
                    "asset_lock_test",
                    digest,
                    "application/octet-stream",
                    ".bin",
                    len(plaintext),
                    "2026-07-31T00:00:00Z",
                ),
            )

        lock_active = False

        class TrackingLock:
            def __enter__(self):
                nonlocal lock_active
                self_outer.assertFalse(lock_active)
                lock_active = True

            def __exit__(self, exc_type, exc, traceback):
                nonlocal lock_active
                self_outer.assertTrue(lock_active)
                lock_active = False

        class Cipher:
            key_version = 1

            @staticmethod
            def blob_id(asset_id: str) -> str:
                return f"blob-{asset_id}"

            @staticmethod
            def open_blob_chunk(blob_id: str, index: int, count: int, value: bytes) -> bytes:
                return value

        class Transport:
            @staticmethod
            def download_blob_chunk(blob_id: str, index: int):
                self_outer.assertFalse(lock_active, "network wait must stay outside the data lock")
                return plaintext

        self_outer = self
        real_replace = os.replace

        def checked_replace(source, target) -> None:
            self.assertTrue(lock_active)
            real_replace(source, target)

        def checked_restore(connection, asset_id: str, relative_path: str) -> None:
            self.assertTrue(lock_active)

        with (
            patch.object(sync_module, "process_data_lock", side_effect=lambda: TrackingLock()),
            patch.object(sync_module.os, "replace", side_effect=checked_replace),
            patch.object(sync_module, "_restore_asset_references", side_effect=checked_restore),
        ):
            downloaded, errors = sync_module._sync_download_assets(Transport(), Cipher())

        self.assertEqual(1, downloaded)
        self.assertEqual([], errors)
        self.assertFalse(lock_active)
        with connect() as connection:
            row = connection.execute(
                "SELECT relative_path,unresolved FROM managed_assets WHERE id='asset_lock_test'"
            ).fetchone()
        self.assertEqual(f"assets/{digest}.bin", row["relative_path"])
        self.assertEqual(0, row["unresolved"])

    def test_preference_conflict_commits_database_and_mirror_under_one_lock(self) -> None:
        self.configure_test_home("conflict-lock-home")
        with connect() as connection:
            device_id = connection.execute(
                "SELECT value FROM app_metadata WHERE key='device_id'"
            ).fetchone()[0]
            local = make_revision(
                "preferences",
                {"kind": "settings", "content": "{\"source\":\"local\"}"},
                entity_id="preferences_conflict_test",
                author_device_id=device_id,
            )
            remote = make_revision(
                "preferences",
                {"kind": "settings", "content": "{\"source\":\"remote\"}"},
                entity_id="preferences_conflict_test",
                author_device_id=device_id,
            )
            conflict_id = sync_module._record_conflict(
                connection,
                base=None,
                local=local,
                remote=remote,
                paths=["content"],
            )

        lock_active = False
        mirror_observed = False
        self_outer = self

        class TrackingLock:
            def __enter__(self):
                nonlocal lock_active
                self_outer.assertFalse(lock_active)
                lock_active = True

            def __exit__(self, exc_type, exc, traceback):
                nonlocal lock_active
                self_outer.assertTrue(lock_active)
                lock_active = False

        def checked_write_preferences(revisions) -> None:
            nonlocal mirror_observed
            self.assertTrue(lock_active)
            self.assertEqual(1, len(revisions))
            with connect() as connection:
                status = connection.execute(
                    "SELECT status FROM sync_conflicts WHERE id=?",
                    (conflict_id,),
                ).fetchone()[0]
            self.assertEqual("resolved", status)
            mirror_observed = True

        with (
            patch.object(sync_module, "process_data_lock", side_effect=lambda: TrackingLock()),
            patch("mealcircuit.portable._write_preferences", side_effect=checked_write_preferences),
        ):
            result = sync_module.resolve_conflict(conflict_id, "local")

        self.assertEqual("resolved", result["status"])
        self.assertTrue(mirror_observed)
        self.assertFalse(lock_active)

    def test_sync_pull_keeps_network_outside_and_page_commit_inside_data_lock(self) -> None:
        self.configure_test_home("pull-lock-home")
        with connect() as connection:
            connection.execute(
                """UPDATE sync_configuration SET enabled=1,server_url=?,account_id=?,device_name=?
                   WHERE singleton=1""",
                ("https://sync.example.test", "account", "desktop"),
            )

        lock_active = False
        mirror_observed = False
        self_outer = self

        class TrackingLock:
            def __enter__(self):
                nonlocal lock_active
                self_outer.assertFalse(lock_active)
                lock_active = True

            def __exit__(self, exc_type, exc, traceback):
                nonlocal lock_active
                self_outer.assertTrue(lock_active)
                lock_active = False

        class Transport:
            @staticmethod
            def capabilities() -> dict:
                self_outer.assertFalse(lock_active)
                return {
                    "protocol": "mealcircuit.sync",
                    "min_version": 1,
                    "max_version": 1,
                    "max_batch": 100,
                    "max_pull": 500,
                    "e2ee_required": True,
                }

            @staticmethod
            def pull(cursor: int, limit: int = 500, snapshot_offset: int = 0) -> dict:
                self_outer.assertFalse(lock_active, "network pull must stay outside the data lock")
                return {"cursor": 5, "has_more": False, "changes": []}

            @staticmethod
            def ack(cursor: int) -> None:
                self_outer.assertFalse(lock_active)
                self_outer.assertEqual(5, cursor)

        def checked_process_pull(connection, cipher, pulled):
            self.assertTrue(lock_active)
            return (
                {"applied": 1, "merged": 0, "conflicts": 0, "unknown": 0},
                [object()],
                [],
            )

        def checked_write_preferences(revisions) -> None:
            nonlocal mirror_observed
            self.assertTrue(lock_active)
            self.assertEqual(1, len(revisions))
            with connect() as connection:
                cursor = connection.execute(
                    "SELECT cursor_value FROM sync_cursor WHERE scope='account'"
                ).fetchone()[0]
            self.assertEqual(5, cursor)
            mirror_observed = True

        with (
            patch.object(sync_module, "process_data_lock", side_effect=lambda: TrackingLock()),
            patch.object(sync_module, "_get_sync_secret", return_value=b"k" * 32),
            patch.object(sync_module, "prepare_outbox", return_value=[]),
            patch.object(sync_module, "_process_pull", side_effect=checked_process_pull),
            patch.object(sync_module, "_queue_remote_daily_source_updates", return_value=[]),
            patch.object(sync_module, "_sync_upload_assets", return_value=(0, [])),
            patch.object(sync_module, "_sync_download_assets", return_value=(0, [])),
            patch("mealcircuit.portable._write_preferences", side_effect=checked_write_preferences),
        ):
            result = sync_now(Transport())

        self.assertEqual(5, result["cursor"])
        self.assertEqual(1, result["applied"])
        self.assertTrue(mirror_observed)
        self.assertFalse(lock_active)

    def test_persistent_write_failure_does_not_mask_stale_system_secret(self) -> None:
        class FakeKeyringError(Exception):
            pass

        class FailingKeyring:
            def __init__(self) -> None:
                self.values = {"credential": "stale-system-value"}

            def get_keyring(self):
                return SimpleNamespace(priority=1)

            def get_password(self, service: str, name: str):
                return self.values.get(name)

            def set_password(self, service: str, name: str, value: str) -> None:
                raise FakeKeyringError("credential manager unavailable")

            def delete_password(self, service: str, name: str) -> None:
                raise FakeKeyringError("credential manager unavailable")

        backend = FailingKeyring()
        with patch.object(
            secret_store_module,
            "_keyring",
            return_value=(backend, FakeKeyringError),
        ):
            with self.assertRaisesRegex(
                secret_store_module.SecretStorageError,
                "Windows 凭据存储写入失败",
            ):
                set_secret("credential", "fresh-session-value")
            self.assertEqual("stale-system-value", get_secret("credential"))
            self.assertFalse(delete_secret("credential"))
            self.assertIsNone(get_secret("credential"))

    def test_key_rotation_does_not_commit_when_credential_write_fails(self) -> None:
        self.configure_test_home("rotation-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="account",
                device_name="desktop",
                account_data_key=b"o" * 32,
                access_token="access",
                refresh_token="refresh",
            )
            with connect() as connection:
                before = connection.execute(
                    "SELECT key_version FROM sync_configuration WHERE singleton=1"
                ).fetchone()[0]
                before_slot = sync_module._active_credential_slot(connection)
            store.fail_next_write = True
            with self.assertRaises(secret_store_module.SecretStorageError):
                sync_module._finalize_local_rotation(
                    int(before) + 1,
                    {"account_data_key": b"n" * 32},
                    {},
                )
            with connect() as connection:
                after = connection.execute(
                    "SELECT key_version FROM sync_configuration WHERE singleton=1"
                ).fetchone()[0]
                after_slot = sync_module._active_credential_slot(connection)
            self.assertEqual(before, after)
            self.assertEqual(before_slot, after_slot)
            self.assertEqual(b"o" * 32, sync_module._get_sync_secret("sync.account_data_key", binary=True))

    def test_key_rotation_activates_key_and_database_epoch_together(self) -> None:
        self.configure_test_home("rotation-success-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="account",
                device_name="desktop",
                account_data_key=b"o" * 32,
                access_token="access",
                refresh_token="refresh",
            )
            with connect() as connection:
                before_slot = sync_module._active_credential_slot(connection)
            sync_module._finalize_local_rotation(
                2,
                {"account_data_key": b"n" * 32},
                {},
            )
            with connect() as connection:
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT key_version FROM sync_configuration WHERE singleton=1"
                    ).fetchone()[0],
                )
                after_slot = sync_module._active_credential_slot(connection)
            self.assertNotEqual(before_slot, after_slot)
            self.assertEqual(
                {
                    "account_data_key": b"n" * 32,
                    "access_token": "access",
                    "refresh_token": "refresh",
                },
                sync_module._current_sync_credentials(),
            )

    def test_key_rotation_finalize_rejects_stale_account_generation(self) -> None:
        self.configure_test_home("rotation-race-home")
        store = MemorySyncSecretStore()
        with self.secret_patches(store):
            configure_sync(
                server_url="https://sync.example.test",
                account_id="old-account",
                device_name="desktop",
                account_data_key=b"o" * 32,
                access_token="old-access",
                refresh_token="old-refresh",
            )
            with connect() as connection:
                old_slot = sync_module._active_credential_slot(connection)
            configure_sync(
                server_url="https://sync.example.test",
                account_id="new-account",
                device_name="desktop",
                account_data_key=b"c" * 32,
                access_token="current-access",
                refresh_token="current-refresh",
            )
            with self.assertRaisesRegex(ValidationError, "密钥轮换期间同步账户已改变"):
                sync_module._finalize_local_rotation(
                    2,
                    {"account_data_key": b"r" * 32},
                    {},
                    expected_account_id="old-account",
                    expected_key_version=1,
                    expected_slot=old_slot,
                )
            self.assertEqual("new-account", sync_status()["account_id"])
            self.assertEqual(1, sync_status()["key_version"])
            self.assertEqual(
                {
                    "account_data_key": b"c" * 32,
                    "access_token": "current-access",
                    "refresh_token": "current-refresh",
                },
                sync_module._current_sync_credentials(),
            )

    def test_working_system_secret_backend_preserves_normal_round_trip(self) -> None:
        class FakeKeyringError(Exception):
            pass

        class MemoryKeyring:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}

            def get_keyring(self):
                return SimpleNamespace(priority=1)

            def get_password(self, service: str, name: str):
                return self.values.get(name)

            def set_password(self, service: str, name: str, value: str) -> None:
                self.values[name] = value

            def delete_password(self, service: str, name: str) -> None:
                self.values.pop(name)

        backend = MemoryKeyring()
        with patch.object(
            secret_store_module,
            "_keyring",
            return_value=(backend, FakeKeyringError),
        ):
            self.assertEqual("system", set_secret("credential", "system-value"))
            self.assertEqual("system-value", get_secret("credential"))
            self.assertTrue(delete_secret("credential"))
            self.assertIsNone(get_secret("credential"))

    @unittest.skipIf(TestClient is None, "install sync extras to run encrypted asset tests")
    def test_sync_asset_download_rejects_unsafe_legacy_database_metadata(self) -> None:
        home = self.root / "home"
        configure_home(home)
        init_db()
        plaintext = b"asset payload"
        digest = hashlib.sha256(plaintext).hexdigest()
        asset_id = "asset_test"
        with connect() as connection:
            connection.execute(
                """INSERT INTO managed_assets(
                       id,sha256,media_type,extension,byte_count,relative_path,unresolved,created_at
                   ) VALUES(?,?,?,?,?,NULL,1,?)""",
                (
                    asset_id,
                    digest,
                    "application/octet-stream",
                    r"\..\..\..\escaped.bin",
                    len(plaintext),
                    "2026-07-31T00:00:00Z",
                ),
            )
        cipher = AccountCipher("account_test", b"k" * 32)
        blob_id = cipher.blob_id(asset_id)
        encrypted = cipher.seal_blob_chunk(blob_id, 0, 1, plaintext)
        self_outer = self

        class Transport:
            def download_blob_chunk(self, requested_blob_id: str, index: int):
                self_outer.assertEqual(blob_id, requested_blob_id)
                self_outer.assertEqual(0, index)
                return encrypted

        downloaded, errors = sync_module._sync_download_assets(Transport(), cipher)
        self.assertEqual(0, downloaded)
        self.assertTrue(errors)
        self.assertFalse((self.root / "escaped.bin").exists())

    @unittest.skipIf(TestClient is None, "install sync extras to run encrypted asset tests")
    def test_sync_asset_download_keeps_valid_asset_inside_private_home(self) -> None:
        home = self.root / "home"
        configure_home(home)
        init_db()
        plaintext = b"valid asset payload"
        digest = hashlib.sha256(plaintext).hexdigest()
        asset_id = "asset_valid"
        with connect() as connection:
            connection.execute(
                """INSERT INTO managed_assets(
                       id,sha256,media_type,extension,byte_count,relative_path,unresolved,created_at
                   ) VALUES(?,?,?,?,?,NULL,1,?)""",
                (
                    asset_id,
                    digest,
                    "application/octet-stream",
                    ".bin",
                    len(plaintext),
                    "2026-07-31T00:00:00Z",
                ),
            )
        cipher = AccountCipher("account_test", b"k" * 32)
        blob_id = cipher.blob_id(asset_id)
        encrypted = cipher.seal_blob_chunk(blob_id, 0, 1, plaintext)

        class Transport:
            def download_blob_chunk(self, requested_blob_id: str, index: int):
                return encrypted

        downloaded, errors = sync_module._sync_download_assets(Transport(), cipher)
        self.assertEqual(1, downloaded)
        self.assertEqual([], errors)
        target = home / "assets" / f"{digest}.bin"
        self.assertEqual(plaintext, target.read_bytes())


@unittest.skipIf(TestClient is None, "install sync and server extras to run E2EE integration tests")
class SyncIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_home = os.environ.get("MEALCIRCUIT_HOME")
        self.old_db = os.environ.get("MEALCIRCUIT_DB")
        self.app = create_app(
            f"sqlite:///{(self.root / 'server.db').as_posix()}",
            self.root / "blobs",
            registration_mode="open",
            create_schema=True,
        )
        self.client = TestClient(self.app)
        account = self.client.post(
            "/v1/accounts",
            json={
                "login_name": "sync-user",
                "password": "correct horse battery staple",
                "device_name": "desktop-a",
            },
        ).json()
        self.keys = create_key_material(account["account_id"])
        self.account = account
        self.transport_a = ClientTransport(self.client, account["access_token"])
        phone = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "sync-user",
                "password": "correct horse battery staple",
                "device_name": "desktop-b",
            },
        ).json()
        self.phone = phone
        self.transport_b = ClientTransport(self.client, phone["access_token"])
        self.home_a, self.home_b = self.root / "home-a", self.root / "home-b"

    def tearDown(self) -> None:
        self.client.close()
        self.app.state.engine.dispose()
        if self.old_home is None:
            os.environ.pop("MEALCIRCUIT_HOME", None)
        else:
            os.environ["MEALCIRCUIT_HOME"] = self.old_home
        if self.old_db is None:
            os.environ.pop("MEALCIRCUIT_DB", None)
        else:
            os.environ["MEALCIRCUIT_DB"] = self.old_db
        self.temp.cleanup()

    def configure_client(self, home: Path, session: dict, name: str) -> None:
        configure_home(home)
        configure_sync(
            server_url="http://localhost:8080",
            account_id=self.account["account_id"],
            device_name=name,
            remote_device_id=session["device_id"],
            account_data_key=self.keys["account_data_key"],
            access_token=session["access_token"],
            refresh_token=session["refresh_token"],
            allow_insecure_localhost=True,
        )

    def activate(self, home: Path, session: dict) -> None:
        os.environ["MEALCIRCUIT_HOME"] = str(home)
        set_secret("sync.account_data_key", self.keys["account_data_key"])
        set_secret("sync.access_token", session["access_token"])
        set_secret("sync.refresh_token", session["refresh_token"])

    def test_two_offline_clients_sync_and_preserve_same_field_conflict(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        task = service.create_material_task("初始食材")
        first = sync_now(self.transport_a)
        self.assertGreater(first["accepted"], 0)

        self.configure_client(self.home_b, self.phone, "desktop-b")
        second = sync_now(self.transport_b)
        self.assertGreater(second["applied"], 0)
        self.assertEqual(service.get_task(task["id"])["original_input"], "初始食材")

        self.activate(self.home_a, self.account)
        current_a = service.get_task(task["id"])
        service.update_task_input(task["id"], "设备 A 修改", current_a["input_version"])
        self.activate(self.home_b, self.phone)
        current_b = service.get_task(task["id"])
        service.update_task_input(task["id"], "设备 B 修改", current_b["input_version"])

        self.activate(self.home_a, self.account)
        sync_now(self.transport_a)
        self.activate(self.home_b, self.phone)
        result = sync_now(self.transport_b)
        self.assertEqual(result["conflicts"], 1)
        conflicts = list_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertIn("original_input", conflicts[0]["conflicting_paths"])
        values = {
            conflicts[0]["local_revision"]["payload"]["original_input"],
            conflicts[0]["remote_revision"]["payload"]["original_input"],
        }
        self.assertEqual(values, {"设备 A 修改", "设备 B 修改"})

        resolved = resolve_conflict(conflicts[0]["id"], "local")
        self.assertEqual(resolved["status"], "resolved")
        synced = sync_now(self.transport_b)
        self.assertEqual(synced["accepted"], 1)

    def test_synced_preferences_refresh_editable_file_mirror_after_commit(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        sync_now(self.transport_a)
        self.configure_client(self.home_b, self.phone, "desktop-b")
        sync_now(self.transport_b)

        self.activate(self.home_a, self.account)
        changed = "# 来自设备 A 的同步档案\n\n只用于合成测试。\n"
        (self.home_a / "profile.md").write_text(changed, encoding="utf-8")
        init_db()
        pushed = sync_now(self.transport_a)
        self.assertGreater(pushed["accepted"], 0)

        self.activate(self.home_b, self.phone)
        pulled = sync_now(self.transport_b)
        self.assertGreater(pulled["applied"] + pulled["merged"], 0)
        self.assertEqual((self.home_b / "profile.md").read_text(encoding="utf-8"), changed)

    def test_delete_vs_edit_is_retained_as_explicit_conflict(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        food = service.create_food({
            "name": "合成燕麦", "brand": "", "basis": "100g", "energy_kcal": 380,
            "protein_g": 13, "carbs_g": 67, "fat_g": 7, "fiber_g": 10,
            "sodium_mg": 5, "serving_unit": "", "category": "staple",
            "menu_priority": "normal", "default_portion": "40g", "usage_rule": "",
            "source_key": None, "source_url": "", "package_photo_path": None, "notes": "",
        })
        sync_now(self.transport_a)
        self.configure_client(self.home_b, self.phone, "desktop-b")
        sync_now(self.transport_b)

        self.activate(self.home_a, self.account)
        service.delete_food(food["id"])
        self.activate(self.home_b, self.phone)
        local = service.get_food(food["id"])
        service.update_food(food["id"], {**local, "name": "设备 B 编辑后的燕麦"})

        self.activate(self.home_a, self.account)
        sync_now(self.transport_a)
        self.activate(self.home_b, self.phone)
        result = sync_now(self.transport_b)
        self.assertGreaterEqual(result["conflicts"], 1)
        conflict = next(item for item in list_conflicts() if item["entity_id"] == food["id"])
        self.assertIn("$deleted", conflict["conflicting_paths"])
        self.assertEqual(
            {conflict["local_revision"]["deleted"], conflict["remote_revision"]["deleted"]},
            {False, True},
        )
        self.assertEqual(service.get_food(food["id"])["name"], "设备 B 编辑后的燕麦")

    def test_unknown_schema_ciphertext_is_preserved_without_materialization(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        cipher = AccountCipher(self.account["account_id"], self.keys["account_data_key"])
        revision = make_revision(
            "daily_record",
            {
                "id": "record_00000000-0000-4000-8000-000000000091",
                "record_date": "2026-07-10",
                "raw_input": "future schema canary",
                "created_at": "2026-07-10T03:00:00Z",
            },
            entity_id="record_00000000-0000-4000-8000-000000000091",
            author_device_id="device_00000000-0000-4000-8000-000000000091",
        ).to_dict()
        revision["schema_version"] = 2
        remote_id = cipher.remote_id(make_revision(
            "daily_record",
            revision["payload"],
            entity_id=revision["entity_id"],
            author_device_id=revision["author_device_id"],
        ))
        nonce, ciphertext = encrypt(
            cipher.content_key,
            json.dumps(revision, sort_keys=True, separators=(",", ":")).encode(),
            cipher._aad(remote_id),
        )
        pushed = self.transport_a.push([{
            "op_id": "op_00000000-0000-4000-8000-000000000091",
            "remote_id": remote_id,
            "base_server_version": 0,
            "key_version": 1,
            "envelope": {
                "envelope_version": 1,
                "key_version": 1,
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode(),
            },
        }])
        self.assertEqual(pushed["results"][0]["status"], "accepted")

        self.configure_client(self.home_b, self.phone, "desktop-b")
        received = sync_now(self.transport_b)
        self.assertEqual(received["unknown_schema_entities"], 1)
        self.assertEqual(sync_status()["unknown_schema_entities"], 1)
        from mealcircuit.db import connect
        with connect() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM entity_heads WHERE entity_id=?", (revision["entity_id"],)
            ).fetchone())
            stored = connection.execute(
                "SELECT encrypted_envelope FROM sync_unknown_entities WHERE opaque_remote_id=?",
                (remote_id,),
            ).fetchone()
        self.assertIsNotNone(stored)
        self.assertNotIn("future schema canary", stored[0])

    def test_response_loss_retries_same_ops_and_cursor_ack_recovers(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        task = service.create_material_task("响应丢失仍应幂等")
        pending_before = sync_status()["pending"]
        self.assertGreater(pending_before, 0)

        with self.assertRaisesRegex(ConnectionError, "response loss"):
            sync_now(LoseFirstPushResponse(self.transport_a))
        self.assertEqual(sync_status()["pending"], pending_before)
        server_after_loss = self.transport_a.pull(0)
        remote_versions = {
            (item["remote_id"], item["server_version"])
            for item in server_after_loss["changes"]
        }

        retried = sync_now(self.transport_a)
        self.assertGreater(retried["accepted"], 0)
        self.assertEqual(sync_status()["pending"], 0)
        server_after_retry = self.transport_a.pull(0)
        self.assertEqual(
            remote_versions,
            {(item["remote_id"], item["server_version"]) for item in server_after_retry["changes"]},
        )

        self.configure_client(self.home_b, self.phone, "desktop-b")
        with self.assertRaisesRegex(ConnectionError, "acknowledgement response loss"):
            sync_now(LoseFirstAckResponse(self.transport_b))
        cursor_after_loss = sync_status()["cursor"]
        self.assertGreater(cursor_after_loss, 0)
        self.assertEqual(service.get_task(task["id"])["original_input"], "响应丢失仍应幂等")
        recovered = sync_now(self.transport_b)
        self.assertEqual(recovered["cursor"], cursor_after_loss)
        self.assertEqual(recovered["applied"], 0)

    def test_out_of_order_pull_keeps_highest_entity_version(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        task = service.create_material_task("远端版本一")
        sync_now(self.transport_a)
        current = service.get_task(task["id"])
        service.update_task_input(task["id"], "远端版本二", current["input_version"])
        sync_now(self.transport_a)

        self.configure_client(self.home_b, self.phone, "desktop-b")
        result = sync_now(ReversePullOrder(self.transport_b))
        self.assertGreater(result["applied"], 0)
        self.assertEqual(service.get_task(task["id"])["original_input"], "远端版本二")

    def test_same_day_checkins_from_distinct_offline_ids_merge_by_logical_key(self) -> None:
        day = "2026-07-10"
        self.configure_client(self.home_a, self.account, "desktop-a")
        service.save_checkin_answer(day, "weight", "measured", "no", 0)
        service.complete_checkin_module(day, "weight", 0)

        self.configure_client(self.home_b, self.phone, "desktop-b")
        service.skip_checkin_module(day, "sleep", 0)

        self.activate(self.home_a, self.account)
        sync_now(self.transport_a)
        self.activate(self.home_b, self.phone)
        merged = sync_now(self.transport_b)
        self.assertGreaterEqual(merged["merged"], 1)
        state = service.get_checkin_state(day)
        by_key = {item["module_key"]: item for item in state["modules"]}
        self.assertEqual(by_key["weight"]["status"], "completed")
        self.assertEqual(by_key["sleep"]["status"], "skipped")
        self.assertEqual(list_conflicts(), [])
        converged_b = sync_now(self.transport_b)
        self.assertGreaterEqual(converged_b["accepted"], 1)
        self.activate(self.home_a, self.account)
        sync_now(self.transport_a)
        state_a = service.get_checkin_state(day)
        by_key_a = {item["module_key"]: item for item in state_a["modules"]}
        self.assertEqual(by_key_a["weight"]["status"], "completed")
        self.assertEqual(by_key_a["sleep"]["status"], "skipped")

    def test_same_day_same_checkin_field_from_distinct_ids_enters_conflict_center(self) -> None:
        day = "2026-07-11"
        self.configure_client(self.home_a, self.account, "desktop-a")
        service.save_checkin_answer(day, "weight", "measured", "no", 0)
        service.complete_checkin_module(day, "weight", 0)

        self.configure_client(self.home_b, self.phone, "desktop-b")
        service.save_checkin_answer(day, "weight", "measured", "yes", 0)
        service.save_checkin_answer(day, "weight", "weight_kg", "70.0", 0)
        service.save_checkin_answer(day, "weight", "measurement_context", "morning_fasted", 0)
        service.complete_checkin_module(day, "weight", 0)

        self.activate(self.home_a, self.account)
        sync_now(self.transport_a)
        self.activate(self.home_b, self.phone)
        result = sync_now(self.transport_b)
        self.assertGreaterEqual(result["conflicts"], 1)
        conflicts = list_conflicts()
        self.assertTrue(any(
            "modules[weight]" in path
            for conflict in conflicts
            for path in conflict["conflicting_paths"]
        ))

    def test_same_day_pending_reviews_from_distinct_ids_merge_source_sets(self) -> None:
        day = "2026-07-09"
        self.configure_client(self.home_a, self.account, "desktop-a")
        first = service.add_daily_record(day, "设备 A 的记录")
        self.configure_client(self.home_b, self.phone, "desktop-b")
        second = service.add_daily_record(day, "设备 B 的记录")

        self.activate(self.home_a, self.account)
        sync_now(self.transport_a)
        self.activate(self.home_b, self.phone)
        result = sync_now(self.transport_b)
        self.assertGreaterEqual(result["merged"], 1)
        review = service.get_daily_review(day)
        self.assertEqual(set(review["source_record_ids_json"]), {first["id"], second["id"]})
        self.assertEqual(review["status"], "pending")
        self.assertEqual(list_conflicts(), [])

    def test_completed_review_arriving_with_its_source_stays_published(self) -> None:
        day = "2026-07-10"
        self.configure_client(self.home_a, self.account, "desktop-a")
        complete_standard_onboarding()
        service.add_daily_record(day, "Windows 已处理的记录")
        result = daily_result(day, "Windows 已发布的复盘")
        settings = load_resolved_settings()
        result["tomorrow_menu"]["environment"] = settings["meal_environment"]
        result["tomorrow_menu"]["protein_target_g"] = settings["protein_target_g"]
        service.complete_daily_review(day, result)
        sync_now(self.transport_a)

        self.configure_client(self.home_b, self.phone, "android-reader")
        received = sync_now(self.transport_b)

        self.assertEqual(received["requeued_reviews"], [])
        self.assertEqual(service.get_daily_review(day)["status"], "completed")
        self.assertFalse(any(
            item["entity_kind"] == "daily_review" for item in list_conflicts()
        ))

    def test_remote_android_record_requeues_completed_windows_review_without_conflict(self) -> None:
        day = "2026-07-11"
        self.configure_client(self.home_a, self.account, "desktop-a")
        complete_standard_onboarding()
        service.add_daily_record(day, "Windows 已纳入复盘的记录")
        result = daily_result(day, "Windows 已发布的复盘")
        settings = load_resolved_settings()
        result["tomorrow_menu"]["environment"] = settings["meal_environment"]
        result["tomorrow_menu"]["protein_target_g"] = settings["protein_target_g"]
        service.complete_daily_review(day, result)
        sync_now(self.transport_a)

        remote_record_id = "record_00000000-0000-4000-8000-000000000092"
        remote = make_revision(
            "daily_record",
            {
                "id": remote_record_id,
                "record_date": day,
                "raw_input": "来自 Android 的晚间饥饿反馈",
                "created_at": "2026-07-11T18:00:00Z",
            },
            entity_id=remote_record_id,
            author_device_id="device_00000000-0000-4000-8000-000000000092",
            created_at="2026-07-11T18:00:00Z",
        )
        cipher = AccountCipher(self.account["account_id"], self.keys["account_data_key"])
        pushed = self.transport_b.push([{
            "op_id": "op_00000000-0000-4000-8000-000000000092",
            "remote_id": cipher.remote_id(remote),
            "base_server_version": 0,
            "key_version": 1,
            "envelope": cipher.seal(remote),
        }])
        self.assertEqual(pushed["results"][0]["status"], "accepted")

        self.activate(self.home_a, self.account)
        received = sync_now(self.transport_a)
        review = service.get_daily_review(day)

        self.assertEqual(received["requeued_reviews"], [day])
        self.assertEqual(review["status"], "pending")
        self.assertIn(remote_record_id, review["source_record_ids_json"])
        self.assertEqual(list_conflicts(), [])

    def test_same_day_completed_reviews_preserve_both_results_as_active_result_conflict(self) -> None:
        day = "2026-07-08"
        self.configure_client(self.home_a, self.account, "desktop-a")
        complete_standard_onboarding()
        service.add_daily_record(day, "设备 A")
        result_a = daily_result(day, "设备 A 的复盘")
        settings_a = load_resolved_settings()
        result_a["tomorrow_menu"]["environment"] = settings_a["meal_environment"]
        result_a["tomorrow_menu"]["protein_target_g"] = settings_a["protein_target_g"]
        service.complete_daily_review(day, result_a)
        self.configure_client(self.home_b, self.phone, "desktop-b")
        complete_standard_onboarding()
        service.add_daily_record(day, "设备 B")
        result_b = daily_result(day, "设备 B 的复盘")
        settings_b = load_resolved_settings()
        result_b["tomorrow_menu"]["environment"] = settings_b["meal_environment"]
        result_b["tomorrow_menu"]["protein_target_g"] = settings_b["protein_target_g"]
        service.complete_daily_review(day, result_b)

        self.activate(self.home_a, self.account)
        sync_now(self.transport_a)
        self.activate(self.home_b, self.phone)
        result = sync_now(self.transport_b)
        self.assertGreaterEqual(result["conflicts"], 1)
        conflict = next(
            item for item in list_conflicts()
            if "$active_result" in item["conflicting_paths"]
        )
        lines = {
            conflict["local_revision"]["payload"]["review"]["result_json"]["one_line_review"],
            conflict["remote_revision"]["payload"]["review"]["result_json"]["one_line_review"],
        }
        self.assertEqual(lines, {"设备 A 的复盘", "设备 B 的复盘"})

    def test_encrypted_photo_asset_reaches_new_client_and_stays_off_server_plaintext(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        photo_bytes = b"\xff\xd8\xffSYNTHETIC-PRIVATE-PHOTO-CANARY"
        task = service.create_photo_task(io.BytesIO(photo_bytes), "照片同步")
        result_a = sync_now(self.transport_a)
        self.assertEqual(result_a["assets_uploaded"], 1)

        self.configure_client(self.home_b, self.phone, "desktop-b")
        result_b = sync_now(self.transport_b)
        self.assertEqual(result_b["assets_downloaded"], 1)
        restored = service.get_task(task["id"])
        self.assertEqual(resolve_data_path(restored["image_path"]).read_bytes(), photo_bytes)
        self.assertNotIn(photo_bytes, (self.root / "server.db").read_bytes())
        for path in (self.root / "blobs").rglob("*.chunk"):
            self.assertNotIn(photo_bytes, path.read_bytes())

    def test_client_key_rotation_reencrypts_everything_and_new_device_recovers(self) -> None:
        self.configure_client(self.home_a, self.account, "desktop-a")
        task = service.create_material_task("轮换前本地事实")
        self.assertGreater(sync_now(self.transport_a)["accepted"], 0)
        self.configure_client(self.home_b, self.phone, "desktop-b")
        sync_now(self.transport_b)

        self.activate(self.home_a, self.account)
        rotated = rotate_account_key(lambda value: len(value) > 40, self.transport_a)
        self.assertEqual(rotated["key_version"], 2)
        self.assertTrue(rotated["recovery_key"].startswith("MC1-"))
        self.assertEqual(sync_status()["key_version"], 2)
        self.assertEqual(self.client.get("/v1/devices", headers=self.transport_b.headers).status_code, 401)
        snapshot = self.transport_a.pull(0)
        self.assertTrue(snapshot["requires_full_resync"])
        self.assertTrue(all(item["key_version"] == 2 for item in snapshot["changes"]))

        replacement = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "sync-user",
                "password": "correct horse battery staple",
                "device_name": "replacement",
            },
        ).json()
        replacement_headers = {"Authorization": f"Bearer {replacement['access_token']}"}
        envelope = self.client.get("/v1/key-envelopes/recovery", headers=replacement_headers).json()["envelope"]
        recovered = recover_account_data_key(self.account["account_id"], rotated["recovery_key"], envelope)
        self.assertEqual(
            recovered,
            sync_module._get_sync_secret("sync.account_data_key", binary=True),
        )
        home_c = self.root / "home-c"
        configure_home(home_c)
        configure_sync(
            server_url="http://localhost:8080",
            account_id=self.account["account_id"],
            device_name="replacement",
            remote_device_id=replacement["device_id"],
            account_data_key=recovered,
            access_token=replacement["access_token"],
            refresh_token=replacement["refresh_token"],
            key_version=2,
            allow_insecure_localhost=True,
        )
        replacement_transport = ClientTransport(self.client, replacement["access_token"])
        sync_now(replacement_transport)
        self.assertEqual(service.get_task(task["id"])["original_input"], "轮换前本地事实")


if __name__ == "__main__":
    unittest.main()
