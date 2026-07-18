from __future__ import annotations

import io
import base64
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401

    from sync_server.app import create_app
except ImportError:
    TestClient = None

from mealcircuit import personalization, service
from mealcircuit.configuration import initialize_private_home, load_resolved_settings
from mealcircuit.crypto import encrypt
from mealcircuit.db import init_db
from mealcircuit.domain import make_revision
from mealcircuit.secret_store import get_secret, set_secret
from mealcircuit.storage import resolve_data_path
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
        self.assertEqual(recovered, get_secret("sync.account_data_key", binary=True))
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
