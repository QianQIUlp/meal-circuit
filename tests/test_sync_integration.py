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
            "protein_g": 13