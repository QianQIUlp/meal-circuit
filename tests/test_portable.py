from __future__ import annotations

import io
import base64
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from unittest.mock import patch
from pathlib import Path

from mealcircuit import personalization, service
from mealcircuit.configuration import configured_today, configuration_status, initialize_private_home
from mealcircuit.contracts import load_contract, validate_transition
from mealcircuit.crypto import format_recovery_key, parse_recovery_key, random_key
from mealcircuit.db import connect, init_db
from mealcircuit.db_migrations import CURRENT_SCHEMA_VERSION
from mealcircuit import db_migrations
from mealcircuit.domain import make_revision, three_way_merge, validate_revision
from mealcircuit.domain_store import capture_task_input, materialize_revision
from mealcircuit.portable import apply_import, export_data, preview_import
from mealcircuit import portable as portable_module
from mealcircuit.secret_store import delete_secret, set_secret
from mealcircuit.sync import AccountCipher
from mealcircuit.storage import db_path, resolve_data_path
from mealcircuit.validation import ValidationError, validate_daily_review_result, validate_result


try:
    import cryptography  # noqa: F401
except ImportError:
    HAS_CRYPTOGRAPHY = False
else:
    HAS_CRYPTOGRAPHY = True


SETTINGS = {
    "schema_version": 1,
    "timezone": "UTC",
    "meal_environment": "合成测试环境",
    "protein_target_g": [90, 120],
    "portion_method": "合成份量",
    "missing_training_default": "保持未知",
    "compensation_boundary": "恢复标准份量",
    "home_cooking": {"enabled": False},
}


try:
    ZoneInfo("Pacific/Kiritimati")
except ZoneInfoNotFoundError:
    HAS_IANA_TZDATA = False
else:
    HAS_IANA_TZDATA = True


def configure_home(path: Path) -> None:
    os.environ["MEALCIRCUIT_HOME"] = str(path)
    os.environ.pop("MEALCIRCUIT_DB", None)
    initialize_private_home()
    (path / "settings.json").write_text(json.dumps(SETTINGS, ensure_ascii=False), encoding="utf-8")
    (path / "profile.md").write_text("# 合成档案\n", encoding="utf-8")
    (path / "doctrine.private.md").write_text("# 合成规则\n", encoding="utf-8")


def complete_standard_onboarding() -> None:
    current = personalization.start_onboarding()
    payloads = {
        "welcome": {"privacy_ack": True},
        "goals": {
            "primary_goal": "body_recomposition",
            "secondary_goals": [],
            "motivation": "验证可迁移分析结果的来源与失效语义。",
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
        current["id"],
        current["version"],
        {"accept_profile": True, "accept_strategy": True, "planning_mode": "portion_guided"},
    )


class DomainAndPortableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("MEALCIRCUIT_HOME")
        self.old_db = os.environ.get("MEALCIRCUIT_DB")

    def tearDown(self) -> None:
        self.temp.cleanup()
        if self.old_home is None:
            os.environ.pop("MEALCIRCUIT_HOME", None)
        else:
            os.environ["MEALCIRCUIT_HOME"] = self.old_home
        if self.old_db is None:
            os.environ.pop("MEALCIRCUIT_DB", None)
        else:
            os.environ["MEALCIRCUIT_DB"] = self.old_db

    def test_domain_revision_and_three_way_merge(self) -> None:
        revision = make_revision(
            "food_item",
            {
                "food": {
                    "id": "food_019f4a15-5fd1-7582-ae7e-5d45b235d388",
                    "name": "合成燕麦", "basis": "100g",
                    "created_at": "2026-07-10T03:00:00Z",
                    "updated_at": "2026-07-10T03:00:00Z",
                },
                "history": [],
            },
            entity_id="food_019f4a15-5fd1-7582-ae7e-5d45b235d388",
            author_device_id="device_219f4a15-5fd1-7582-ae7e-5d45b235d390",
        )
        self.assertEqual(validate_revision(revision.to_dict()), revision)
        mismatched = revision.to_dict()
        mismatched["payload"]["food"]["id"] = "food_another"
        with self.assertRaisesRegex(ValidationError, "payload ID"):
            validate_revision(mismatched)
        merged, conflicts = three_way_merge(
            {"name": "燕麦", "protein": 10},
            {"name": "合成燕麦", "protein": 10},
            {"name": "燕麦", "protein": 13},
        )
        self.assertEqual(merged, {"name": "合成燕麦", "protein": 13})
        self.assertEqual(conflicts, [])
        _, conflicts = three_way_merge({"name": "燕麦"}, {"name": "A"}, {"name": "B"})
        self.assertEqual(conflicts, ["name"])

    def test_shared_checkin_and_state_machine_contracts(self) -> None:
        contract = load_contract("checkin-modules-v1.json")
        self.assertEqual([item["key"] for item in contract["modules"]], [
            "weight", "training", "hunger", "sleep", "gut",
        ])
        self.assertGreaterEqual(sum(len(item["questions"]) for item in contract["modules"]), 20)
        validate_transition("task", None, "pending")
        validate_transition("task", "pending", "completed")
        with self.assertRaises(ValidationError):
            validate_transition("task", "completed", "pending")

    def test_language_neutral_payloads_materialize_in_python(self) -> None:
        root = Path(self.temp.name) / "android-payloads"
        configure_home(root)
        init_db()
        timestamp = "2026-07-10T03:00:00Z"
        device = "device_219f4a15-5fd1-7582-ae7e-5d45b235d390"
        task_id = "task_019f4a15-5fd1-7582-ae7e-5d45b235d391"
        food_id = "food_019f4a15-5fd1-7582-ae7e-5d45b235d392"
        checkin_id = "checkin_019f4a15-5fd1-7582-ae7e-5d45b235d393"
        review_id = "review_019f4a15-5fd1-7582-ae7e-5d45b235d394"
        values = [
            make_revision("task", {"task": {"id": task_id, "type": "material", "status": "pending", "created_at": timestamp}}, entity_id=task_id, author_device_id=device),
            make_revision("task_input", {"task_id": task_id, "task_type": "material", "input_version": 1, "original_input": "Android 离线输入", "input_history": []}, entity_id="task_input_019f4a15-5fd1-7582-ae7e-5d45b235d395", author_device_id=device),
            make_revision("food_item", {"food": {"id": food_id, "name": "合成食品", "basis": "100g", "created_at": timestamp, "updated_at": timestamp}, "history": []}, entity_id=food_id, author_device_id=device),
            make_revision("checkin_day", {
                "checkin": {"id": checkin_id, "checkin_date": "2026-07-10", "created_at": timestamp, "updated_at": timestamp},
                "modules": [{"module": {"id": "checkin_module_019f4a15-5fd1-7582-ae7e-5d45b235d396", "checkin_id": checkin_id, "module_key": "weight", "status": "skipped", "answers_json": {}, "schema_version": 1, "version": 1, "created_at": timestamp, "updated_at": timestamp, "completed_at": timestamp}, "history": []}],
            }, entity_id=checkin_id, author_device_id=device),
            make_revision("daily_review", {"review": {"id": review_id, "review_date": "2026-07-10", "status": "pending", "source_record_ids_json": [], "result_version": 0, "created_at": timestamp, "updated_at": timestamp}, "history": []}, entity_id=review_id, author_device_id=device),
        ]
        from mealcircuit.db import connect
        with connect() as connection:
            for revision in values:
                materialize_revision(connection, revision)
            self.assertEqual(connection.execute("SELECT original_input FROM tasks WHERE id=?", (task_id,)).fetchone()[0], "Android 离线输入")
            self.assertEqual(connection.execute("SELECT name FROM food_items WHERE id=?", (food_id,)).fetchone()[0], "合成食品")
            self.assertEqual(connection.execute("SELECT status FROM daily_checkin_modules WHERE checkin_id=?", (checkin_id,)).fetchone()[0], "skipped")
            self.assertEqual(connection.execute("SELECT status FROM daily_reviews WHERE id=?", (review_id,)).fetchone()[0], "pending")

    @unittest.skipUnless(HAS_CRYPTOGRAPHY, "install cryptography to run encrypted portable fixture")
    def test_android_and_python_share_portable_mcx_framing_fixture(self) -> None:
        configure_home(Path(self.temp.name) / "portable-fixture")
        fixture_root = Path(__file__).resolve().parents[1] / "protocol" / "fixtures"
        metadata = json.loads((fixture_root / "portable-v1-meta.json").read_text(encoding="utf-8"))
        preview = preview_import(
            fixture_root / "portable-v1.mcx",
            recovery_key=metadata["recovery_key"],
            mode="restore",
        )
        self.assertEqual(preview["entity_count"], 1)
        self.assertEqual(preview["asset_count"], 0)
        with self.assertRaises(ValidationError):
            preview_import(
                fixture_root / "portable-v1.mcx",
                recovery_key=metadata["recovery_key"][:-1] + "A",
                mode="restore",
            )

    def test_schema_migration_creates_metadata_sync_tables_and_backup(self) -> None:
        root = Path(self.temp.name) / "migration"
        configure_home(root)
        legacy = db_path()
        init_db()
        connection = sqlite3.connect(legacy)
        for table in (
            "entity_heads", "domain_revisions", "managed_assets", "config_documents",
            "sync_outbox", "sync_shadow", "sync_cursor", "sync_conflicts",
        ):
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute(
            "UPDATE app_metadata SET value='1' WHERE key='database_schema_version'"
        )
        connection.commit()
        connection.close()
        init_db()
        connection = sqlite3.connect(legacy)
        try:
            version = connection.execute(
                "SELECT value FROM app_metadata WHERE key='database_schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        self.assertIn("domain_revisions", tables)
        self.assertIn("sync_outbox", tables)
        self.assertEqual(
            len(list((legacy.parent / "backups").glob("schema-v*-*.db"))),
            CURRENT_SCHEMA_VERSION - 1,
        )
        first_backup = next((legacy.parent / "backups").glob("schema-v1-*.db"))
        with closing(sqlite3.connect(first_backup)) as backup:
            self.assertIsNone(backup.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='domain_revisions'"
            ).fetchone())

    def test_true_legacy_schema_is_backed_up_before_columns_are_added(self) -> None:
        root = Path(self.temp.name) / "true-legacy"
        configure_home(root)
        legacy = db_path()
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
                    original_input TEXT NOT NULL DEFAULT '', image_path TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT, result_json TEXT,
                    result_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE food_items (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, brand TEXT NOT NULL DEFAULT '',
                    basis TEXT NOT NULL, energy_kcal REAL, protein_g REAL, carbs_g REAL,
                    fat_g REAL, serving_unit TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
                    package_photo_path TEXT, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, deleted_at TEXT
                );
                CREATE TABLE daily_reviews (
                    id TEXT PRIMARY KEY, review_date TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    source_record_ids_json TEXT NOT NULL DEFAULT '[]', result_json TEXT,
                    result_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE daily_review_history (
                    id TEXT PRIMARY KEY, review_id TEXT NOT NULL, version INTEGER NOT NULL,
                    source_record_ids_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    completed_at TEXT, archived_at TEXT NOT NULL
                );
                INSERT INTO tasks(id,type,status,original_input,created_at)
                VALUES('task_legacy123456','material','pending','legacy input','2026-07-10T03:00:00Z');
                """
            )
            connection.commit()

        init_db(legacy)

        with closing(sqlite3.connect(legacy)) as connection:
            self.assertEqual(
                connection.execute("SELECT original_input FROM tasks").fetchone()[0],
                "legacy input",
            )
            self.assertIn("input_version", {
                row[1] for row in connection.execute("PRAGMA table_info(tasks)")
            })
            self.assertIn("source_key", {
                row[1] for row in connection.execute("PRAGMA table_info(food_items)")
            })
            self.assertEqual(
                int(connection.execute(
                    "SELECT value FROM app_metadata WHERE key='database_schema_version'"
                ).fetchone()[0]),
                CURRENT_SCHEMA_VERSION,
            )
        first_backup = next((legacy.parent / "backups").glob("schema-v1-*.db"))
        with closing(sqlite3.connect(first_backup)) as backup:
            self.assertNotIn("input_version", {
                row[1] for row in backup.execute("PRAGMA table_info(tasks)")
            })
            self.assertEqual(
                backup.execute("SELECT original_input FROM tasks").fetchone()[0],
                "legacy input",
            )

    @unittest.skipUnless(HAS_IANA_TZDATA, "install tzdata to verify non-UTC IANA timezone behavior")
    def test_configured_today_uses_user_iana_timezone(self) -> None:
        real_datetime = datetime

        class FixedDatetime:
            @classmethod
            def now(cls, zone):
                return real_datetime(2026, 7, 11, 12, 30, tzinfo=timezone.utc).astimezone(zone)

        with patch("mealcircuit.configuration.datetime", FixedDatetime):
            self.assertEqual(
                "2026-07-12",
                configured_today({"timezone": "Pacific/Kiritimati"}).isoformat(),
            )
            self.assertEqual(
                "2026-07-11",
                configured_today({"timezone": "Pacific/Honolulu"}).isoformat(),
            )

    def test_missing_external_asset_is_retained_and_reported(self) -> None:
        root = Path(self.temp.name) / "missing-external"
        configure_home(root)
        init_db()
        missing = "C:/missing/private-photo.jpg"
        with connect() as connection:
            connection.execute(
                """INSERT INTO tasks(
                       id,type,status,original_input,image_path,created_at,result_version,input_version
                   ) VALUES(?,?,?,?,?,?,0,1)""",
                ("task_legacy123456", "photo", "pending", "", missing, "2026-07-10T03:00:00Z"),
            )
            capture_task_input(connection, "task_legacy123456")
        self.assertIn(missing, configuration_status()["unresolved_assets"])

    def test_failed_multi_step_migration_restores_original_database(self) -> None:
        database = Path(self.temp.name) / "failed-migration.db"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE app_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO app_metadata VALUES('database_schema_version','1')")
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES('original')")
        connection.commit()

        def fail(_connection):
            raise RuntimeError("synthetic migration failure")

        migrations = dict(db_migrations.MIGRATIONS)
        migrations[2] = fail
        with patch.dict(db_migrations.MIGRATIONS, migrations, clear=True):
            with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                db_migrations.migrate(connection, database, existed_before=True)
        self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchone()[0], "original")
        self.assertEqual(
            connection.execute("SELECT value FROM app_metadata WHERE key='database_schema_version'").fetchone()[0],
            "1",
        )
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='domain_revisions'"
        ).fetchone())
        connection.close()

    def test_plain_portable_round_trip_with_asset(self) -> None:
        root = Path(self.temp.name)
        source, target, archive = root / "source", root / "target", root / "portable.zip"
        configure_home(source)
        task = service.create_photo_task(io.BytesIO(b"\xff\xd8\xffsynthetic-photo"), "合成照片")
        service.add_memory("preference", "合成偏好", "合成证据")
        exported = export_data(archive, encrypted=False)
        self.assertEqual(exported["asset_count"], 1)

        configure_home(target)
        preview = preview_import(archive, mode="restore")
        self.assertTrue(preview["ready"])
        imported = apply_import(archive, mode="restore")
        self.assertEqual(imported["round_trip"], "ok")
        restored = service.get_task(task["id"])
        self.assertEqual(restored["original_input"], "合成照片")
        self.assertTrue(resolve_data_path(restored["image_path"]).is_file())

    def test_portable_archive_excludes_device_keys_tokens_and_api_keys(self) -> None:
        root = Path(self.temp.name)
        source, archive = root / "source", root / "portable.zip"
        configure_home(source)
        service.create_material_task("合成食材，不含任何密钥")
        canaries = {
            "sync.account_data_key": "SYNTHETIC-ACCOUNT-DATA-KEY-CANARY",
            "sync.access_token": "SYNTHETIC-ACCESS-TOKEN-CANARY",
            "sync.refresh_token": "SYNTHETIC-REFRESH-TOKEN-CANARY",
            "ai.key.openai": "SYNTHETIC-API-KEY-CANARY",
        }
        try:
            for name, value in canaries.items():
                set_secret(name, value)
            with patch.dict(os.environ, {"MEALCIRCUIT_OPENAI_API_KEY": "SYNTHETIC-ENV-API-KEY-CANARY"}):
                export_data(archive, encrypted=False)
            raw = archive.read_bytes()
            for value in (*canaries.values(), "SYNTHETIC-ENV-API-KEY-CANARY"):
                self.assertNotIn(value.encode(), raw)
        finally:
            for name in canaries:
                delete_secret(name)

    def test_import_failure_after_writes_restores_database_configs_and_assets(self) -> None:
        root = Path(self.temp.name)
        source, target, archive = root / "source", root / "target", root / "portable.zip"
        configure_home(source)
        imported_task = service.create_photo_task(io.BytesIO(b"\xff\xd8\xffrollback-photo"), "待回滚任务")
        export_data(archive, encrypted=False)

        configure_home(target)
        init_db()
        baseline = service.add_memory("preference", "目标端保留数据", "回滚证据")
        settings_before = (target / "settings.json").read_bytes()
        assets_before = {path.relative_to(target) for path in target.rglob("*") if path.is_file()}
        first_preview_state = portable_module._current_payloads()
        with patch(
            "mealcircuit.portable._current_payloads",
            side_effect=[first_preview_state, RuntimeError("synthetic post-write failure")],
        ):
            with self.assertRaisesRegex(RuntimeError, "post-write failure"):
                apply_import(archive, mode="merge")

        self.assertEqual((target / "settings.json").read_bytes(), settings_before)
        self.assertEqual(
            {path.relative_to(target) for path in target.rglob("*") if path.is_file()},
            assets_before,
        )
        self.assertEqual(service.overview()["memories"][0]["id"], baseline["id"])
        with self.assertRaises(KeyError):
            service.get_task(imported_task["id"])

    def test_interrupted_import_journal_recovers_on_next_database_open(self) -> None:
        root = Path(self.temp.name) / "journal-target"
        configure_home(root)
        init_db()
        baseline = service.add_memory("preference", "崩溃前数据", "journal")
        settings_before = (root / "settings.json").read_bytes()
        transaction = portable_module._ImportTransaction()
        self.assertTrue(transaction.journal.is_dir())
        with transaction.activated():
            connection = sqlite3.connect(db_path())
            connection.execute("DELETE FROM memories")
            connection.commit()
            connection.close()
            (transaction.staging / "settings.json").write_text("{}", encoding="utf-8")
            orphan = transaction.staging / "assets" / "interrupted.tmp"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(b"partial")

        # Simulate a process death after the staged directory was promoted but
        # before the durable state advanced to staging_promoted.
        os.replace(transaction.home, transaction.backup)
        transaction.state = "original_moved"
        transaction._write_manifest()
        os.replace(transaction.staging, transaction.home)

        self.assertTrue(portable_module.recover_interrupted_import())
        self.assertFalse(transaction.journal.exists())
        self.assertEqual((root / "settings.json").read_bytes(), settings_before)
        self.assertFalse((root / "assets" / "interrupted.tmp").exists())
        self.assertEqual(service.overview()["memories"][0]["id"], baseline["id"])

    def test_local_write_captures_revision_asset_and_outbox_atomically(self) -> None:
        root = Path(self.temp.name) / "outbox"
        configure_home(root)
        init_db()
        connection = sqlite3.connect(db_path())
        try:
            connection.execute(
                """UPDATE sync_configuration SET enabled=1,server_url='https://sync.invalid',
                   account_id='account_synthetic123',device_name='test',updated_at='2026-07-10T00:00:00Z'
                   WHERE singleton=1"""
            )
            connection.commit()
        finally:
            connection.close()
        task = service.create_photo_task(io.BytesIO(b"\xff\xd8\xffsynthetic-photo"), "原子写入")
        connection = sqlite3.connect(db_path())
        try:
            connection.row_factory = sqlite3.Row
            revisions = connection.execute(
                """SELECT entity_kind,entity_id FROM domain_revisions
                   WHERE entity_kind IN ('asset','task','task_input') ORDER BY entity_kind"""
            ).fetchall()
            outbox = connection.execute(
                "SELECT entity_id,encrypted_envelope,state FROM sync_outbox ORDER BY local_sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertRegex(task["id"], r"^task_[0-9a-f-]{36}$")
        self.assertEqual({row["entity_kind"] for row in revisions}, {"asset", "task", "task_input"})
        self.assertEqual({row["entity_id"] for row in outbox}, {row["entity_id"] for row in revisions})
        self.assertTrue(all(row["encrypted_envelope"] is None and row["state"] == "pending" for row in outbox))

    def test_recovery_key_checksum_and_tampered_manifest(self) -> None:
        secret = random_key()
        shown = format_recovery_key(secret)
        self.assertEqual(parse_recovery_key(shown), secret)
        with self.assertRaises(ValidationError):
            parse_recovery_key(shown[:-1] + ("A" if shown[-1] != "A" else "B"))

        root = Path(self.temp.name)
        source, archive = root / "source", root / "portable.zip"
        configure_home(source)
        service.create_material_task("合成鸡蛋")
        export_data(archive, encrypted=False)
        tampered = root / "tampered.zip"
        with zipfile.ZipFile(archive) as source_zip, zipfile.ZipFile(tampered, "w") as target_zip:
            for info in source_zip.infolist():
                data = source_zip.read(info.filename)
                if info.filename.startswith("entities/"):
                    data += b"{}\n"
                target_zip.writestr(info, data)
        with self.assertRaises(ValidationError):
            preview_import(tampered, mode="restore")

    def test_portable_rejects_path_escape_and_compressed_asset_bomb(self) -> None:
        root = Path(self.temp.name)
        configure_home(root / "target")
        init_db()
        manifest = json.dumps(
            {
                "format": "mealcircuit.portable",
                "format_version": 1,
                "domain_schema_version": 1,
                "entity_heads": {},
                "content": {},
                "assets": [],
            }
        )
        escaping = root / "escaping.zip"
        with zipfile.ZipFile(escaping, "w") as archive:
            archive.writestr("manifest.json", manifest)
            archive.writestr("../outside.txt", "must not escape")
        with self.assertRaisesRegex(ValidationError, "不安全路径"):
            preview_import(escaping, mode="restore")

        bomb = root / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", manifest)
            archive.writestr("assets/oversized.bin", b"0" * (10 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValidationError, "条目大小|压缩比"):
            preview_import(bomb, mode="restore")

    def test_portable_preserves_revision_graph_and_auto_merges_disjoint_fields(self) -> None:
        root = Path(self.temp.name)
        home_a, home_b = root / "branch-a", root / "branch-b"
        base_archive, branch_archive = root / "base.zip", root / "branch-a.zip"
        configure_home(home_a)
        food = service.create_food(
            {
                "name": "燕麦",
                "brand": "合成品牌",
                "basis": "100g",
                "energy_kcal": 380,
                "protein_g": 13,
                "carbs_g": 67,
                "fat_g": 7,
                "fiber_g": 10,
                "sodium_mg": 5,
                "serving_unit": "",
                "category": "staple",
                "menu_priority": "normal",
                "default_portion": "50g",
                "usage_rule": "早餐",
                "source_key": None,
                "source_url": "",
                "package_photo_path": None,
                "notes": "基础备注",
            }
        )
        export_data(base_archive, encrypted=False)

        configure_home(home_b)
        apply_import(base_archive, mode="restore")
        os.environ["MEALCIRCUIT_HOME"] = str(home_a)
        branch_a = service.get_food(food["id"])
        branch_a["name"] = "全谷燕麦"
        service.update_food(food["id"], branch_a)
        export_data(branch_archive, encrypted=False)

        os.environ["MEALCIRCUIT_HOME"] = str(home_b)
        branch_b = service.get_food(food["id"])
        branch_b["notes"] = "设备 B 备注"
        service.update_food(food["id"], branch_b)
        preview = preview_import(branch_archive, mode="merge")
        self.assertTrue(preview["ready"])
        self.assertGreater(preview["revision_count"], preview["entity_count"])
        merged = apply_import(branch_archive, mode="merge")
        self.assertEqual(merged["conflicts"], 0)
        result = service.get_food(food["id"])
        self.assertEqual(result["name"], "全谷燕麦")
        self.assertEqual(result["notes"], "设备 B 备注")
        connection = sqlite3.connect(db_path())
        try:
            head = connection.execute(
                "SELECT revision_id FROM entity_heads WHERE entity_id=?", (food["id"],)
            ).fetchone()[0]
            parents = json.loads(
                connection.execute(
                    "SELECT parent_revision_ids_json FROM domain_revisions WHERE revision_id=?", (head,)
                ).fetchone()[0]
            )
        finally:
            connection.close()
        self.assertEqual(len(parents), 2)

    def test_analysis_result_records_sources_and_becomes_stale_without_overwrite(self) -> None:
        root = Path(self.temp.name) / "provenance"
        configure_home(root)
        complete_standard_onboarding()
        task = service.create_material_task("鸡蛋 2 个")
        result = {
            "summary": "合成分析",
            "combinations": ["鸡蛋"],
            "batch_nutrition": {
                "energy_kcal": [130, 170],
                "protein_g": [11, 15],
                "carbs_g": [0, 3],
                "fat_g": [8, 12],
            },
            "per_serving_nutrition": {
                "energy_kcal": [130, 170],
                "protein_g": [11, 15],
                "carbs_g": [0, 3],
                "fat_g": [8, 12],
            },
            "gaps": [],
            "risks": [],
            "minimal_adjustments": ["配蔬菜"],
        }
        completed = service.complete_task(task["id"], result)
        self.assertEqual(completed["result_json"], result)
        provenance = completed["result_provenance_json"]
        self.assertFalse(provenance["stale"])
        self.assertTrue(
            any(item["entity_kind"] == "task_input" for item in provenance["source_revisions"])
        )
        connection = sqlite3.connect(db_path())
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM entity_heads WHERE entity_kind='analysis_result'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

        changed = dict(SETTINGS)
        changed["meal_environment"] = "改变后的环境"
        (root / "settings.json").write_text(
            json.dumps(changed, ensure_ascii=False), encoding="utf-8"
        )
        self.assertTrue(service.get_task(task["id"])["result_provenance_json"]["stale"])
        self.assertEqual(service.get_task(task["id"])["result_json"], result)

    def test_cross_language_crypto_vector(self) -> None:
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("install cryptography to run cross-language vector")
        protocol = Path(__file__).parents[1] / "protocol" / "fixtures"
        revision = validate_revision(
            json.loads((protocol / "domain-revision.json").read_text(encoding="utf-8"))
        )
        vector = json.loads((protocol / "crypto-v1.json").read_text(encoding="utf-8"))
        cipher = AccountCipher(vector["account_id"], bytes.fromhex(vector["account_data_key_hex"]))
        sealed = cipher._seal(revision, bytes.fromhex(vector["nonce_hex"]))
        self.assertEqual(sealed["remote_id"], vector["remote_id"])
        self.assertEqual(sealed["ciphertext"], vector["ciphertext_base64"])
        self.assertEqual(cipher.open(vector["remote_id"], sealed), revision)

    def test_sync_envelope_rejects_wrong_key_nonce_aad_truncation_and_version(self) -> None:
        if not HAS_CRYPTOGRAPHY:
            self.skipTest("install cryptography to run authenticated-encryption tests")
        fixture = Path(__file__).parents[1] / "protocol" / "fixtures"
        revision = validate_revision(json.loads((fixture / "domain-revision.json").read_text(encoding="utf-8")))
        vector = json.loads((fixture / "crypto-v1.json").read_text(encoding="utf-8"))
        cipher = AccountCipher(vector["account_id"], bytes.fromhex(vector["account_data_key_hex"]))
        sealed = cipher._seal(revision, bytes.fromhex(vector["nonce_hex"]))

        cases = []
        wrong_nonce = dict(sealed)
        nonce = bytearray(base64.b64decode(wrong_nonce["nonce"])); nonce[0] ^= 1
        wrong_nonce["nonce"] = base64.b64encode(nonce).decode("ascii")
        cases.append((cipher, sealed["remote_id"], wrong_nonce))
        wrong_ciphertext = dict(sealed)
        ciphertext = bytearray(base64.b64decode(wrong_ciphertext["ciphertext"])); ciphertext[-1] ^= 1
        wrong_ciphertext["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
        cases.append((cipher, sealed["remote_id"], wrong_ciphertext))
        truncated = dict(sealed)
        truncated["ciphertext"] = base64.b64encode(base64.b64decode(truncated["ciphertext"])[:-1]).decode("ascii")
        cases.append((cipher, sealed["remote_id"], truncated))
        cases.append((cipher, "0" * 64, sealed))
        wrong_version = dict(sealed); wrong_version["key_version"] = 2
        cases.append((cipher, sealed["remote_id"], wrong_version))
        cases.append((AccountCipher(vector["account_id"], bytes([127]) * 32), sealed["remote_id"], sealed))
        for opener, remote_id, envelope in cases:
            with self.assertRaises(ValidationError):
                opener.open(remote_id, envelope)

        blob_id = cipher.blob_id("asset_fixture")
        chunk = cipher.seal_blob_chunk(blob_id, 0, 1, b"photo canary")
        self.assertEqual(cipher.open_blob_chunk(blob_id, 0, 1, chunk), b"photo canary")
        with self.assertRaises(ValidationError):
            cipher.open_blob_chunk(blob_id, 1, 1, chunk)
        with self.assertRaises(ValidationError):
            cipher.open_blob_chunk(blob_id, 0, 1, chunk[:-1])

    def test_cross_language_result_context_and_merge_contract(self) -> None:
        fixture_path = Path(__file__).parents[1] / "protocol" / "fixtures" / "contract-v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["context"]["window_days"], 14)
        self.assertIn("result_schema", fixture["context"]["required_task_keys"])
        self.assertIn("ingredient_carryover_obligations", fixture["context"]["required_daily_keys"])

        self.assertEqual(validate_result("photo", fixture["photo_result"]), fixture["photo_result"])
        self.assertEqual(validate_result("material", fixture["material_result"]), fixture["material_result"])
        daily = fixture["daily"]
        result = validate_daily_review_result(daily["result"], daily["settings"])
        self.assertEqual(result["tomorrow_menu"]["date"], daily["tomorrow"])
        self.assertEqual(
            {item["food_id"] for item in result["priority_food_decisions"]},
            set(daily["priority_food_ids"]),
        )
        service._validate_ingredient_carryover_decisions(result, daily["carryovers"])

        for case in fixture["merge_cases"]:
            merged, conflicts = three_way_merge(case["base"], case["local"], case["remote"])
            self.assertEqual(merged, case["expected"], case["name"])
            self.assertEqual(conflicts, case["conflicts"], case["name"])

        invalid = json.loads(json.dumps(fixture["photo_result"], ensure_ascii=False))
        invalid["unknowns"] = [""]
        with self.assertRaises(ValidationError):
            validate_result("photo", invalid)


if __name__ == "__main__":
    unittest.main()
