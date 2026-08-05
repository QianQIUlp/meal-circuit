from __future__ import annotations

import io
import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from unittest.mock import patch
from pathlib import Path

from mealcircuit import agent_intelligence, agent_workspace, personalization, service
from mealcircuit.configuration import configured_today, configuration_status, initialize_private_home
from mealcircuit.contracts import load_contract, validate_transition
from mealcircuit.crypto import format_recovery_key, parse_recovery_key, random_key
from mealcircuit.db import connect, init_db
from mealcircuit.db_migrations import CURRENT_SCHEMA_VERSION
from mealcircuit import db_migrations
from mealcircuit.domain import DomainRevision, make_revision, three_way_merge, validate_revision
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

    def test_portable_revision_graph_handles_twenty_thousand_deep_chain_iteratively(self) -> None:
        depth = 20_000
        revisions = {
            f"rev_deep_{index}": DomainRevision(
                entity_id="preferences_deep",
                entity_kind="preferences",
                revision_id=f"rev_deep_{index}",
                parent_revision_ids=() if index == 0 else (f"rev_deep_{index - 1}",),
                created_at="2026-08-01T00:00:00Z",
                author_device_id="device_deep",
                deleted=False,
                payload={"kind": "settings", "content": "{}"},
            )
            for index in range(depth)
        }

        portable_module._validate_revision_graph(revisions)

        first = revisions["rev_deep_0"]
        cyclic = dict(revisions)
        cyclic[first.revision_id] = DomainRevision(
            entity_id=first.entity_id,
            entity_kind=first.entity_kind,
            revision_id=first.revision_id,
            parent_revision_ids=(f"rev_deep_{depth - 1}",),
            created_at=first.created_at,
            author_device_id=first.author_device_id,
            deleted=first.deleted,
            payload=first.payload,
        )
        with self.assertRaisesRegex(ValidationError, "循环"):
            portable_module._validate_revision_graph(cyclic)

    def test_portable_revision_parent_must_belong_to_same_entity_and_kind(self) -> None:
        parent = DomainRevision(
            entity_id="preferences_parent",
            entity_kind="preferences",
            revision_id="rev_parent_identity",
            parent_revision_ids=(),
            created_at="2026-08-01T00:00:00Z",
            author_device_id="device_graph",
            deleted=False,
            payload={"kind": "settings", "content": "{}"},
        )
        for child in (
            DomainRevision(
                entity_id="preferences_child",
                entity_kind=parent.entity_kind,
                revision_id="rev_child_entity",
                parent_revision_ids=(parent.revision_id,),
                created_at=parent.created_at,
                author_device_id=parent.author_device_id,
                deleted=False,
                payload=parent.payload,
            ),
            DomainRevision(
                entity_id=parent.entity_id,
                entity_kind="memory",
                revision_id="rev_child_kind",
                parent_revision_ids=(parent.revision_id,),
                created_at=parent.created_at,
                author_device_id=parent.author_device_id,
                deleted=False,
                payload={"content": "different kind"},
            ),
        ):
            with self.subTest(revision_id=child.revision_id):
                with self.assertRaisesRegex(ValidationError, "同一实体和类型"):
                    portable_module._validate_revision_graph(
                        {parent.revision_id: parent, child.revision_id: child}
                    )

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
        with zipfile.ZipFile(archive) as portable:
            manifest = json.loads(portable.read("manifest.json"))
            self.assertEqual(len(manifest["assets"]), 1)
            descriptor = manifest["assets"][0]
            self.assertEqual(
                set(descriptor), {"id", "sha256", "path", "bytes", "media_type"}
            )
            asset_revisions = [
                json.loads(line)
                for line in portable.read("entities/asset.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
            head = next(
                item
                for item in asset_revisions
                if item["revision_id"] == manifest["entity_heads"][descriptor["id"]]
            )
            self.assertEqual(descriptor["id"], head["entity_id"])
            self.assertEqual(descriptor["sha256"], head["payload"]["sha256"])
            self.assertEqual(descriptor["bytes"], head["payload"]["byte_count"])
            self.assertEqual(descriptor["media_type"], head["payload"]["media_type"])
            self.assertEqual(descriptor["path"], head["payload"]["archive_path"])
            self.assertEqual(
                descriptor["path"],
                f"assets/{descriptor['sha256']}{head['payload']['extension']}",
            )

        configure_home(target)
        preview = preview_import(archive, mode="restore")
        self.assertTrue(preview["ready"])
        imported = apply_import(archive, mode="restore")
        self.assertEqual(imported["round_trip"], "ok")
        restored = service.get_task(task["id"])
        self.assertEqual(restored["original_input"], "合成照片")
        self.assertTrue(resolve_data_path(restored["image_path"]).is_file())

    def test_historical_v1_asset_descriptor_without_id_or_media_type_is_supported(self) -> None:
        root = Path(self.temp.name)
        source, target = root / "legacy-asset-source", root / "legacy-asset-target"
        archive, legacy_archive = root / "current.zip", root / "legacy-v1.zip"
        configure_home(source)
        task = service.create_photo_task(
            io.BytesIO(b"\xff\xd8\xfflegacy-v1-asset"), "历史资产"
        )
        export_data(archive, encrypted=False)

        with zipfile.ZipFile(archive) as input_zip, zipfile.ZipFile(
            legacy_archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as output_zip:
            for info in input_zip.infolist():
                data = input_zip.read(info.filename)
                if info.filename == "manifest.json":
                    manifest = json.loads(data)
                    manifest["assets"] = [
                        {
                            "path": descriptor["path"],
                            "bytes": descriptor["bytes"],
                            "sha256": descriptor["sha256"],
                        }
                        for descriptor in manifest["assets"]
                    ]
                    data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                output_zip.writestr(info, data)

        configure_home(target)
        preview = preview_import(legacy_archive, mode="restore")
        self.assertEqual(preview["asset_count"], 1)
        imported = apply_import(legacy_archive, mode="restore")
        self.assertEqual(imported["round_trip"], "ok")
        restored = service.get_task(task["id"])
        self.assertTrue(resolve_data_path(restored["image_path"]).is_file())

    def test_apply_import_uses_one_private_snapshot_when_source_changes(self) -> None:
        root = Path(self.temp.name)
        source, target, archive = (
            root / "source-snapshot",
            root / "target-snapshot",
            root / "snapshot.zip",
        )
        configure_home(source)
        task = service.create_photo_task(
            io.BytesIO(b"\xff\xd8\xffstable-snapshot"),
            "快照导入",
        )
        export_data(archive, encrypted=False)

        configure_home(target)
        temporary_root = portable_module._portable_temp_root()
        stale = temporary_root / "mealcircuit-source-stale.portable"
        recent = temporary_root / "mealcircuit-source-recent.portable"
        stale.write_bytes(b"stale")
        recent.write_bytes(b"recent")
        os.utime(stale, (1, 1))
        real_preview = portable_module.preview_import
        preview_sources: list[Path] = []

        def preview_after_replacement(path, *args, **kwargs):
            preview_sources.append(Path(path).resolve())
            if len(preview_sources) == 1:
                archive.write_bytes(b"source replaced after snapshot")
            return real_preview(path, *args, **kwargs)

        with patch.object(
            portable_module,
            "preview_import",
            side_effect=preview_after_replacement,
        ):
            imported = apply_import(archive, mode="restore")

        self.assertEqual(imported["round_trip"], "ok")
        self.assertTrue(preview_sources)
        self.assertTrue(all(path != archive.resolve() for path in preview_sources))
        self.assertTrue(all(path.parent == temporary_root for path in preview_sources))
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(all(not path.exists() for path in preview_sources))
        restored = service.get_task(task["id"])
        self.assertEqual(restored["original_input"], "快照导入")
        self.assertTrue(resolve_data_path(restored["image_path"]).is_file())

    def test_export_rejects_dangling_asset_reference_before_writing_backup(self) -> None:
        root = Path(self.temp.name)
        source, archive = root / "source-dangling", root / "dangling.zip"
        configure_home(source)
        service.create_photo_task(io.BytesIO(b"\xff\xd8\xffdangling-source"), "悬空引用")
        missing_id = f"asset_{'f' * 64}"
        with connect() as connection:
            head = connection.execute(
                """SELECT r.revision_id,r.payload_json
                   FROM entity_heads h JOIN domain_revisions r ON r.revision_id=h.revision_id
                   WHERE h.entity_kind='task_input'"""
            ).fetchone()
            self.assertIsNotNone(head)
            payload = json.loads(head["payload_json"])
            payload["asset_id"] = missing_id
            connection.execute(
                "UPDATE domain_revisions SET payload_json=? WHERE revision_id=?",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    head["revision_id"],
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(ValidationError, "缺失资产"):
            export_data(archive, encrypted=False)
        self.assertFalse(archive.exists())

        tombstone = make_revision(
            "asset",
            {
                "sha256": "1" * 64,
                "media_type": "image/jpeg",
                "extension": ".jpg",
                "byte_count": 4,
            },
            entity_id="asset_" + "1" * 64,
            author_device_id="device_portable_tombstone",
            deleted=True,
        )
        referencing = make_revision(
            "task_input",
            {
                "task_id": "task_019f4a15-5fd1-7582-ae7e-5d45b235d399",
                "task_type": "photo",
                "input_version": 1,
                "original_input": "",
                "asset_id": tombstone.entity_id,
                "input_history": [],
            },
            entity_id="task_input_019f4a15-5fd1-7582-ae7e-5d45b235d398",
            author_device_id="device_portable_tombstone",
        )
        portable_module._require_asset_references(
            [referencing, tombstone],
            {tombstone.entity_id},
        )

    def test_portable_asset_manifest_is_strictly_bound_to_asset_head(self) -> None:
        root = Path(self.temp.name)
        source, target, archive = root / "source-contract", root / "target-contract", root / "base.zip"
        configure_home(source)
        service.create_photo_task(io.BytesIO(b"\xff\xd8\xffasset-contract"), "资产契约")
        export_data(archive, encrypted=False)

        with zipfile.ZipFile(archive) as portable:
            base_entries = {
                info.filename: portable.read(info.filename)
                for info in portable.infolist()
                if not info.is_dir()
            }
        base_manifest = json.loads(base_entries["manifest.json"])
        base_descriptor = base_manifest["assets"][0]

        def write_variant(name: str, mutate) -> Path:
            entries = dict(base_entries)
            manifest = json.loads(json.dumps(base_manifest))
            mutate(manifest, entries)
            entries["manifest.json"] = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            path = root / f"asset-contract-{name}.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for entry_name, data in entries.items():
                    output.writestr(entry_name, data)
            return path

        descriptor_variants = {
            "missing-field": {key: value for key, value in base_descriptor.items() if key != "media_type"},
            "extra-field": {**base_descriptor, "unexpected": True},
            "id-type": {**base_descriptor, "id": 1},
            "sha-type": {**base_descriptor, "sha256": 1},
            "path-type": {**base_descriptor, "path": 1},
            "bytes-string": {**base_descriptor, "bytes": str(base_descriptor["bytes"])},
            "bytes-bool": {**base_descriptor, "bytes": True},
            "bytes-float": {**base_descriptor, "bytes": float(base_descriptor["bytes"])},
            "media-type-type": {**base_descriptor, "media_type": 1},
            "id-mismatch": {**base_descriptor, "id": "asset_unknown"},
            "sha-mismatch": {**base_descriptor, "sha256": "0" * 64},
            "path-mismatch": {**base_descriptor, "path": "assets/unrelated.bin"},
            "bytes-mismatch": {**base_descriptor, "bytes": base_descriptor["bytes"] + 1},
            "media-type-mismatch": {**base_descriptor, "media_type": "application/octet-stream"},
        }
        variants: dict[str, list[dict]] = {
            name: [descriptor] for name, descriptor in descriptor_variants.items()
        }
        variants["missing-mapping"] = []
        variants["duplicate-mapping"] = [dict(base_descriptor), dict(base_descriptor)]
        variants["extra-mapping"] = [
            {**base_descriptor, "id": "asset_unknown"},
            dict(base_descriptor),
        ]

        configure_home(target)
        for name, descriptors in variants.items():
            with self.subTest(name=name):
                tampered = write_variant(
                    name,
                    lambda manifest, _entries, value=descriptors: manifest.__setitem__("assets", value),
                )
                with self.assertRaises(ValidationError):
                    preview_import(tampered, mode="restore")

        def corrupt_asset_head(manifest: dict, entries: dict[str, bytes]) -> None:
            path = "entities/asset.jsonl"
            revisions = [
                json.loads(line)
                for line in entries[path].decode("utf-8").splitlines()
                if line.strip()
            ]
            head_id = manifest["entity_heads"][base_descriptor["id"]]
            for revision in revisions:
                if revision["revision_id"] == head_id:
                    revision["payload"]["archive_path"] = "assets/wrong.bin"
            raw = (
                "\n".join(
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for item in revisions
                )
                + "\n"
            ).encode("utf-8")
            entries[path] = raw
            manifest["content"][path]["sha256"] = portable_module._sha256_bytes(raw)

        with self.assertRaises(ValidationError):
            preview_import(write_variant("head-archive-path", corrupt_asset_head), mode="restore")

        def add_unlisted_asset(_manifest: dict, entries: dict[str, bytes]) -> None:
            entries["assets/unlisted.bin"] = b"unlisted"

        with self.assertRaisesRegex(ValidationError, "精确对应"):
            preview_import(write_variant("unlisted-file", add_unlisted_asset), mode="restore")

    def test_agent_user_model_projection_round_trips_and_hydrates_locally(self) -> None:
        root = Path(self.temp.name)
        source, target, archive = root / "source-agent", root / "target-agent", root / "agent.zip"
        configure_home(source)
        init_db()
        original = agent_workspace.upsert_claim(
            claim_type="stable_preference", statement="晚餐更喜欢一锅完成",
            scope={"meal": "晚餐"}, effect={"complexity": "优先一锅菜"},
            evidence_type="user_correction", evidence_id="portable-proof", explicit=True,
        )
        goal_contract = agent_intelligence.refresh_goal_contract_sync(
            personalization.active_personalization()
        )
        episode = agent_intelligence.refresh_meal_episode("2026-07-14", "lunch")
        export_data(archive, encrypted=False)

        configure_home(target)
        apply_import(archive, mode="restore")
        restored = {item["id"]: item for item in agent_workspace.list_claims()}
        self.assertIn(original["id"], restored)
        self.assertEqual("active", restored[original["id"]]["status"])
        self.assertEqual({"complexity": "优先一锅菜"}, restored[original["id"]]["effect_json"])
        with connect() as conn:
            projections = {
                row["kind"]: json.loads(row["content"])
                for row in conn.execute(
                    "SELECT kind,content FROM config_documents WHERE kind IN ('goal_contract','meal_episode_projection')"
                ).fetchall()
            }
            restored_episode = conn.execute(
                "SELECT event_date,meal_slot FROM meal_episode_projections WHERE id=?", (episode["id"],)
            ).fetchone()
        self.assertEqual(goal_contract["contract_id"], projections["goal_contract"]["contract_id"])
        self.assertEqual("2026-07-14", restored_episode["event_date"])
        self.assertEqual("lunch", restored_episode["meal_slot"])
        self.assertEqual(1, len(projections["meal_episode_projection"]["episodes"]))

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

    def test_asset_metadata_rejects_windows_path_escape_and_invalid_sizes(self) -> None:
        digest = hashlib.sha256(b"asset").hexdigest()
        valid = make_revision(
            "asset",
            {
                "sha256": digest,
                "media_type": "image/png",
                "extension": ".png",
                "byte_count": 5,
                "archive_path": f"assets/{digest}.png",
            },
            author_device_id="device_test",
        )
        self.assertEqual(".png", valid.payload["extension"])
        for change in (
            {"sha256": "not-a-digest"},
            {"extension": r"\..\..\..\escaped.cmd"},
            {"extension": ".png:stream"},
            {"byte_count": -1},
            {"byte_count": "5"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                make_revision(
                    "asset",
                    {**valid.payload, **change},
                    author_device_id="device_test",
                )

        root = Path(self.temp.name)
        configure_home(root / "target")
        escaped = root / "escaped.bin"
        archive_path = f"assets/{digest}.bin"
        unsafe = replace(
            valid,
            payload={
                **valid.payload,
                "archive_path": archive_path,
                "extension": r"\..\..\..\escaped.bin",
            },
        )
        archive_file = root / "unsafe-asset.zip"
        with zipfile.ZipFile(archive_file, "w") as writer:
            writer.writestr(archive_path, b"asset")
        with zipfile.ZipFile(archive_file) as reader:
            manifest = {
                "entity_heads": {unsafe.entity_id: unsafe.revision_id},
                "assets": [
                    {
                        "id": unsafe.entity_id,
                        "sha256": digest,
                        "path": archive_path,
                        "bytes": 5,
                        "media_type": "image/png",
                    }
                ],
            }
            with self.assertRaises(ValidationError):
                portable_module._asset_paths(reader, manifest, [unsafe])
        self.assertFalse(escaped.exists())

    def test_merge_records_entity_kind_collision_without_overwriting_head(self) -> None:
        root = Path(self.temp.name)
        source, target, archive = root / "kind-source", root / "kind-target", root / "kind.zip"
        configure_home(source)
        task = service.create_material_task("同 ID 不同类型")
        export_data(archive, encrypted=False)

        configure_home(target)
        init_db()
        local = DomainRevision(
            entity_id=task["id"],
            entity_kind="preferences",
            revision_id="rev_local_kind_collision",
            parent_revision_ids=(),
            created_at="2026-08-01T00:00:00Z",
            author_device_id="device_local_kind",
            deleted=False,
            payload={"kind": "settings", "content": "{}"},
        )
        with connect() as connection:
            portable_module._store_revision_only(connection, local)
            connection.execute(
                """INSERT INTO entity_heads(entity_id,entity_kind,revision_id,conflicted,updated_at)
                   VALUES(?,?,?,0,?)""",
                (local.entity_id, local.entity_kind, local.revision_id, local.created_at),
            )
            connection.commit()

        preview = preview_import(archive, mode="merge")
        self.assertIn(
            {"kind": "task", "id": task["id"]},
            preview["conflicts"],
        )
        self.assertNotIn(
            {"kind": "task", "id": task["id"]},
            preview["new"],
        )
        result = apply_import(archive, mode="merge")
        self.assertEqual(result["conflicts"], 1)
        with connect() as connection:
            head = connection.execute(
                "SELECT entity_kind,revision_id,conflicted FROM entity_heads WHERE entity_id=?",
                (task["id"],),
            ).fetchone()
            conflict = connection.execute(
                "SELECT conflicting_paths_json FROM sync_conflicts WHERE entity_id=?",
                (task["id"],),
            ).fetchone()
        self.assertEqual(tuple(head), (local.entity_kind, local.revision_id, 1))
        self.assertEqual(json.loads(conflict["conflicting_paths_json"]), ["$entity_kind"])

    def test_merge_rejects_existing_revision_id_with_different_content(self) -> None:
        root = Path(self.temp.name)
        source, target, archive = (
            root / "revision-source",
            root / "revision-target",
            root / "revision.zip",
        )
        configure_home(source)
        service.create_material_task("revision 冲突")
        export_data(archive, encrypted=False)
        with zipfile.ZipFile(archive) as portable:
            entity_path = "entities/task_input.jsonl"
            remote_value = json.loads(
                portable.read(entity_path).decode("utf-8").splitlines()[0]
            )

        configure_home(target)
        init_db()
        local_value = json.loads(json.dumps(remote_value))
        local_value["payload"]["original_input"] = "本机不同内容"
        local = validate_revision(local_value)
        with connect() as connection:
            portable_module._store_revision_only(connection, local)
            connection.commit()

        with self.assertRaisesRegex(ValidationError, "已有不同内容冲突"):
            preview_import(archive, mode="merge")

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
