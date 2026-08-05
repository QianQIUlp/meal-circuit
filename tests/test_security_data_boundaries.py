from __future__ import annotations

import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mealcircuit import migration, portable, service, storage
from mealcircuit.configuration import initialize_private_home
from mealcircuit.db import connect, init_db
from mealcircuit.domain import make_revision, new_id
from mealcircuit.domain_store import capture_task_input
from mealcircuit.storage import (
    copy_tree_without_reparse_points,
    create_secure_directory,
    ensure_secure_app_home,
    ensure_secure_directory,
    iter_regular_tree_files,
    resolve_managed_media_path,
)
from mealcircuit.validation import ValidationError


class DataSecurityBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.previous_environment = {
            key: os.environ.get(key)
            for key in (
                "MEALCIRCUIT_HOME",
                "MEALCIRCUIT_DB",
                "DIETOS_DB",
                "MEALCIRCUIT_DOCTRINE",
            )
        }
        os.environ["MEALCIRCUIT_HOME"] = str(self.home)
        for key in ("MEALCIRCUIT_DB", "DIETOS_DB", "MEALCIRCUIT_DOCTRINE"):
            os.environ.pop(key, None)
        initialize_private_home()
        init_db()

    def tearDown(self) -> None:
        for key, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _create_junction(self, link: Path, target: Path) -> None:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"Windows junction unavailable: {result.stderr or result.stdout}")

    def _current_user_sid(self) -> str:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"whoami SID lookup unavailable: {result.stderr or result.stdout}")
        rows = list(csv.reader(result.stdout.splitlines()))
        if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-"):
            self.skipTest(f"whoami returned no SID: {result.stdout}")
        return rows[0][1]

    def _icacls(self, path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["icacls.exe", str(path), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"icacls unavailable: {result.stderr or result.stdout}")
        return result

    def _acl_contains_sid(self, path: Path, sid: str) -> bool:
        result = self._icacls(path, "/findsid", f"*{sid}", "/C")
        return str(path).casefold() in result.stdout.casefold()

    def test_windows_dacl_state_rejects_absent_null_and_inheriting_dacls(self) -> None:
        for present, address in ((False, 1), (True, 0)):
            with self.subTest(present=present, address=address), self.assertRaisesRegex(
                ValidationError,
                "缺少安全 DACL",
            ):
                storage._require_safe_windows_dacl(
                    self.root,
                    present=present,
                    address=address,
                    control=0x1000,
                    require_protected=True,
                )
        with self.assertRaisesRegex(ValidationError, "仍允许从父目录继承"):
            storage._require_safe_windows_dacl(
                self.root,
                present=True,
                address=1,
                control=0,
                require_protected=True,
            )

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    def test_secure_directory_chain_is_private_and_rolls_back_on_failure(self) -> None:
        shared = self.root / "shared-secure-chain"
        shared.mkdir()
        everyone_sid = "S-1-1-0"
        self._icacls(shared, "/grant", f"*{everyone_sid}:(OI)(CI)(RX)")
        try:
            target = shared / "first" / "second" / "private"
            create_secure_directory(target)
            self.assertTrue(self._acl_contains_sid(shared, everyone_sid))
            for directory in (shared / "first", shared / "first" / "second", target):
                with self.subTest(directory=directory):
                    self.assertFalse(self._acl_contains_sid(directory, everyone_sid))
            with self.assertRaisesRegex(ValidationError, "已存在"):
                create_secure_directory(target)

            failing = shared / "rollback-first" / "rollback-second" / "private"
            original = storage._create_windows_secure_directory
            calls = 0

            def fail_second(directory: Path, dacl_source: Path | None) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected secure-directory failure")
                original(directory, dacl_source)

            with (
                patch("mealcircuit.storage._create_windows_secure_directory", side_effect=fail_second),
                self.assertRaisesRegex(ValidationError, "无法安全创建目录"),
            ):
                create_secure_directory(failing)
            self.assertFalse((shared / "rollback-first").exists())
        finally:
            self._icacls(shared, "/remove:g", f"*{everyone_sid}")

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    def test_tree_copy_preserves_source_root_dacl_under_shared_parent(self) -> None:
        shared = self.root / "shared-copy-acl"
        shared.mkdir()
        everyone_sid = "S-1-1-0"
        current_sid = self._current_user_sid()
        self._icacls(shared, "/grant", f"*{everyone_sid}:(OI)(CI)(RX)")
        try:
            source = shared / "source"
            source.mkdir()
            (source / "secret.txt").write_text("private", encoding="utf-8")
            self._icacls(source, "/grant:r", f"*{current_sid}:(OI)(CI)(F)")
            self._icacls(source, "/inheritance:r")
            self.assertTrue(self._acl_contains_sid(shared, everyone_sid))
            self.assertFalse(self._acl_contains_sid(source, everyone_sid))

            target = shared / "target"
            copy_tree_without_reparse_points(source, target)
            self.assertEqual("private", (target / "secret.txt").read_text(encoding="utf-8"))
            self.assertFalse(self._acl_contains_sid(target, everyone_sid))
            self.assertNotIn("(I)", self._icacls(target).stdout)
        finally:
            self._icacls(shared, "/remove:g", f"*{everyone_sid}")

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    def test_first_app_home_creation_is_private_under_shared_parent(self) -> None:
        shared = self.root / "shared-first-home"
        shared.mkdir()
        everyone_sid = "S-1-1-0"
        self._icacls(shared, "/grant", f"*{everyone_sid}:(OI)(CI)(RX)")
        new_home = shared / "missing-parent" / "MealCircuit"
        previous_home = os.environ.get("MEALCIRCUIT_HOME")
        try:
            os.environ["MEALCIRCUIT_HOME"] = str(new_home)
            created = ensure_secure_app_home()
            self.assertEqual(new_home, created)
            self.assertFalse(self._acl_contains_sid(shared / "missing-parent", everyone_sid))
            self.assertFalse(self._acl_contains_sid(new_home, everyone_sid))
        finally:
            if previous_home is None:
                os.environ.pop("MEALCIRCUIT_HOME", None)
            else:
                os.environ["MEALCIRCUIT_HOME"] = previous_home
            self._icacls(shared, "/remove:g", f"*{everyone_sid}")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_existing_secure_directory_rejects_junction_ancestor(self) -> None:
        outside = self.root / "secure-directory-outside"
        leaf = outside / "ordinary-leaf"
        leaf.mkdir(parents=True)
        junction = self.root / "secure-directory-junction"
        self._create_junction(junction, outside)
        previous_home = os.environ.get("MEALCIRCUIT_HOME")
        previous_db = os.environ.get("MEALCIRCUIT_DB")
        try:
            with self.assertRaisesRegex(ValidationError, "重解析点"):
                ensure_secure_directory(junction / leaf.name)
            os.environ["MEALCIRCUIT_HOME"] = str(junction / "MealCircuit")
            with self.assertRaisesRegex(ValidationError, "重解析点"):
                ensure_secure_app_home()
            os.environ["MEALCIRCUIT_HOME"] = str(self.home)
            os.environ["MEALCIRCUIT_DB"] = str(junction / leaf.name / "unsafe.db")
            with self.assertRaisesRegex(ValidationError, "重解析点"):
                with connect():
                    pass
        finally:
            if previous_home is None:
                os.environ.pop("MEALCIRCUIT_HOME", None)
            else:
                os.environ["MEALCIRCUIT_HOME"] = previous_home
            if previous_db is None:
                os.environ.pop("MEALCIRCUIT_DB", None)
            else:
                os.environ["MEALCIRCUIT_DB"] = previous_db
            junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows named mutex behavior")
    def test_windows_data_mutex_blocks_same_home_but_not_different_home(self) -> None:
        home_a = self.root / "mutex-home-a"
        home_b = self.root / "mutex-home-b"
        probe = (
            "import sys\n"
            "from pathlib import Path\n"
            "from mealcircuit.storage import DataDirectoryBusyError, process_data_lock\n"
            "try:\n"
            "    with process_data_lock(Path(sys.argv[1]), wait=False):\n"
            "        print('ACQUIRED')\n"
            "except DataDirectoryBusyError:\n"
            "    print('BUSY')\n"
            "    raise SystemExit(3)\n"
        )

        def run_probe(home: Path) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-c", probe, str(home)],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True,
                text=True,
                check=False,
            )

        with storage.process_data_lock(home_a, wait=False):
            same_home = run_probe(home_a)
            different_home = run_probe(home_b)
        released_home = run_probe(home_a)

        self.assertEqual(3, same_home.returncode, same_home.stderr or same_home.stdout)
        self.assertIn("BUSY", same_home.stdout)
        self.assertEqual(0, different_home.returncode, different_home.stderr)
        self.assertIn("ACQUIRED", different_home.stdout)
        self.assertEqual(0, released_home.returncode, released_home.stderr)
        self.assertIn("ACQUIRED", released_home.stdout)
        self.assertEqual(
            storage._windows_data_mutex_name(Path(str(home_a).upper())),
            storage._windows_data_mutex_name(Path(str(home_a).lower())),
        )
        self.assertNotEqual(
            storage._windows_data_mutex_name(home_a),
            storage._windows_data_mutex_name(home_b),
        )
        self.assertFalse(list(self.root.glob(".mealcircuit-import-lock-*")))

    def test_new_home_promotion_race_never_deletes_unknown_home(self) -> None:
        new_home = self.root / "promotion-race" / "MealCircuit"
        previous = {
            key: os.environ.get(key)
            for key in (
                "MEALCIRCUIT_HOME",
                "MEALCIRCUIT_DB",
                "DIETOS_DB",
                "MEALCIRCUIT_DOCTRINE",
            )
        }
        os.environ["MEALCIRCUIT_HOME"] = str(new_home)
        for key in ("MEALCIRCUIT_DB", "DIETOS_DB", "MEALCIRCUIT_DOCTRINE"):
            os.environ.pop(key, None)
        real_replace = portable.os.replace

        def competing_home(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path.name.startswith(".mealcircuit-import-staging-")
                and destination_path == new_home
            ):
                new_home.mkdir(parents=True)
                (new_home / "other-process.txt").write_text("keep", encoding="utf-8")
                raise PermissionError("synthetic competing home")
            return real_replace(source, destination)

        try:
            with (
                patch.object(portable.os, "replace", side_effect=competing_home),
                self.assertRaisesRegex(portable.ImportRollbackError, "自动回滚也失败"),
            ):
                with portable.atomic_home_update():
                    (portable.app_home() / "staged.txt").write_text("staged", encoding="utf-8")

            self.assertEqual(
                "keep",
                (new_home / "other-process.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse((new_home / "staged.txt").exists())
            self.assertEqual(
                1,
                len(list(new_home.parent.glob(".mealcircuit-import-rollback-*"))),
            )
            self.assertEqual(
                1,
                len(list(new_home.parent.glob(".mealcircuit-import-staging-*"))),
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_invalid_import_journal_is_preserved_for_manual_recovery(self) -> None:
        new_home = self.root / "invalid-journal" / "MealCircuit"
        previous_home = os.environ.get("MEALCIRCUIT_HOME")
        os.environ["MEALCIRCUIT_HOME"] = str(new_home)
        journal = new_home.parent / (
            f".mealcircuit-import-rollback-{storage.data_home_identity(new_home)}"
        )
        create_secure_directory(journal)
        evidence = journal / "evidence.txt"
        evidence.write_text("preserve", encoding="utf-8")
        try:
            with self.assertRaisesRegex(RuntimeError, "已保留现场"):
                portable.recover_interrupted_import()
            self.assertEqual("preserve", evidence.read_text(encoding="utf-8"))
            self.assertTrue(journal.is_dir())
        finally:
            if previous_home is None:
                os.environ.pop("MEALCIRCUIT_HOME", None)
            else:
                os.environ["MEALCIRCUIT_HOME"] = previous_home

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    def test_recovery_rejects_unprotected_precreated_journal_before_manifest(self) -> None:
        new_home = self.root / "spoofed-journal" / "MealCircuit"
        new_home.parent.mkdir()
        previous_home = os.environ.get("MEALCIRCUIT_HOME")
        os.environ["MEALCIRCUIT_HOME"] = str(new_home)
        journal = new_home.parent / (
            f".mealcircuit-import-rollback-{storage.data_home_identity(new_home)}"
        )
        journal.mkdir()
        manifest = journal / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        try:
            with self.assertRaisesRegex(RuntimeError, "当前用户与 DACL 验证"):
                portable.recover_interrupted_import()
            self.assertEqual("{}", manifest.read_text(encoding="utf-8"))
            self.assertTrue(journal.is_dir())
        finally:
            if previous_home is None:
                os.environ.pop("MEALCIRCUIT_HOME", None)
            else:
                os.environ["MEALCIRCUIT_HOME"] = previous_home

    def test_successful_new_home_keeps_internal_owner_marker_without_breaking_data(self) -> None:
        new_home = self.root / "owned-new-home" / "MealCircuit"
        previous_home = os.environ.get("MEALCIRCUIT_HOME")
        os.environ["MEALCIRCUIT_HOME"] = str(new_home)
        try:
            with portable.atomic_home_update():
                (portable.app_home() / "business.txt").write_text("ok", encoding="utf-8")
            self.assertEqual("ok", (new_home / "business.txt").read_text(encoding="utf-8"))
            owner_marker = new_home / portable._HOME_OWNER_MARKER
            self.assertRegex(owner_marker.read_text(encoding="ascii").strip(), r"^[0-9a-f]{32}$")
            self.assertFalse(list(new_home.parent.glob(".mealcircuit-import-rollback-*")))
            self.assertFalse(list(new_home.parent.glob(".mealcircuit-import-staging-*")))
            self.assertFalse(portable.recover_interrupted_import())
        finally:
            if previous_home is None:
                os.environ.pop("MEALCIRCUIT_HOME", None)
            else:
                os.environ["MEALCIRCUIT_HOME"] = previous_home

    def test_unc_media_path_is_rejected_before_filesystem_resolution(self) -> None:
        with (
            patch.object(Path, "resolve", side_effect=AssertionError("must not resolve UNC")),
            self.assertRaisesRegex(ValidationError, "网络共享或设备命名空间"),
        ):
            resolve_managed_media_path(r"\\attacker.invalid\share\photo.jpg")

    def test_tree_walk_permission_errors_fail_closed_without_partial_copy(self) -> None:
        blocked = self.home / "blocked"
        blocked.mkdir()
        (blocked / "secret.txt").write_text("secret", encoding="utf-8")

        def denied_walk(*args, **kwargs):
            error = PermissionError(13, "access denied", str(blocked))
            kwargs["onerror"](error)
            yield from ()

        with (
            patch("mealcircuit.storage.os.walk", side_effect=denied_walk),
            self.assertRaisesRegex(ValidationError, "无法完整读取目录树"),
        ):
            list(iter_regular_tree_files(self.home))

        target = self.root / "partial-copy"
        with (
            patch("mealcircuit.storage.os.walk", side_effect=denied_walk),
            self.assertRaisesRegex(ValidationError, "无法完整读取目录树"),
        ):
            copy_tree_without_reparse_points(self.home, target)
        self.assertFalse(target.exists())

    def test_existing_external_file_is_display_only_and_never_hashed(self) -> None:
        external = self.root / "outside-private.jpg"
        external.write_bytes(b"private")
        with connect() as connection:
            connection.execute(
                """INSERT INTO tasks(
                       id,type,status,original_input,image_path,created_at,result_version,input_version
                   ) VALUES(?,?,?,?,?,?,0,1)""",
                (
                    "task_external_boundary",
                    "photo",
                    "pending",
                    "",
                    str(external),
                    "2026-07-31T00:00:00Z",
                ),
            )
            revision = capture_task_input(connection, "task_external_boundary")
            managed_count = int(
                connection.execute("SELECT COUNT(*) FROM managed_assets").fetchone()[0]
            )
        self.assertIsNotNone(revision)
        self.assertEqual(str(external), revision.payload["external_reference"])
        self.assertIs(revision.payload["unresolved"], True)
        self.assertEqual(0, managed_count)
        with connect() as connection:
            mapping, sources = portable._collect_assets(connection)
        self.assertEqual(
            {"external_reference": str(external), "unresolved": True},
            mapping[str(external)],
        )
        self.assertEqual({}, sources)

    def test_imported_external_references_are_never_materialized_as_paths(self) -> None:
        task_id = new_id("task")
        task_revision = make_revision(
            "task",
            {
                "task": {
                    "id": task_id,
                    "type": "photo",
                    "status": "pending",
                    "created_at": "2026-07-31T00:00:00Z",
                    "external_reference": r"\\attacker.invalid\share\photo.jpg",
                    "unresolved": True,
                }
            },
            entity_id=task_id,
            author_device_id=new_id("device"),
        )
        with connect() as connection:
            portable._apply_revision(connection, task_revision, {})
            stored = connection.execute(
                "SELECT image_path FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        self.assertIsNone(stored[0])

        food_id = new_id("food")
        food_revision = make_revision(
            "food_item",
            {
                "food": {
                    "id": food_id,
                    "name": "Synthetic",
                    "basis": "100g",
                    "created_at": "2026-07-31T00:00:00Z",
                    "updated_at": "2026-07-31T00:00:00Z",
                    "package_photo_external_reference": "C:/private/photo.jpg",
                    "package_photo_unresolved": True,
                },
                "history": [],
            },
            entity_id=food_id,
            author_device_id=new_id("device"),
        )
        with connect() as connection:
            portable._apply_revision(connection, food_revision, {})
            stored = connection.execute(
                "SELECT package_photo_path FROM food_items WHERE id=?",
                (food_id,),
            ).fetchone()
        self.assertIsNone(stored[0])

    def test_deep_revision_graph_validation_is_iterative(self) -> None:
        revisions = {
            f"rev_{index}": SimpleNamespace(
                parent_revision_ids=() if index == 0 else (f"rev_{index - 1}",)
            )
            for index in range(10_000)
        }
        portable._validate_revision_graph(revisions)

        cycle = {
            "rev_a": SimpleNamespace(parent_revision_ids=("rev_b",)),
            "rev_b": SimpleNamespace(parent_revision_ids=("rev_a",)),
        }
        with self.assertRaisesRegex(ValidationError, "包含循环"):
            portable._validate_revision_graph(cycle)

    def test_legacy_media_path_rejects_parent_traversal(self) -> None:
        unsafe = (
            r"data\uploads\..\private.jpg",
            r"..\private.jpg",
            r"\\attacker.invalid\share\private.jpg",
            r"\\?\C:\private.jpg",
            r"C:\private.jpg",
            r"C:private.jpg",
            r"\private.jpg",
            r"data\uploads\NUL.txt",
            "data\\uploads\\COM¹.txt",
            "data\\uploads\\LPT³.png",
        )
        for value in unsafe:
            with self.subTest(value=value), self.assertRaisesRegex(
                ValidationError,
                "不安全的媒体路径",
            ):
                migration._normal_path(value, "uploads")
        self.assertEqual(
            "uploads/photo.jpg",
            migration._normal_path(r"C:\legacy\data\uploads\photo.jpg", "uploads"),
        )
        self.assertEqual(
            "uploads/photo.jpg",
            migration._normal_path("photo.jpg", "uploads"),
        )

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_managed_media_rejects_junction_root_and_superscript_devices(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.jpg").write_bytes(b"private")
        uploads = self.home / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        uploads.rmdir()
        self._create_junction(uploads, outside)
        try:
            with self.assertRaisesRegex(ValidationError, "重解析点|逃逸"):
                resolve_managed_media_path("uploads/private.jpg")
        finally:
            uploads.rmdir()
            uploads.mkdir()

        for name in (
            "COM¹.txt",
            "COM².jpg",
            "LPT³.png",
            "bad?.jpg",
            "bad<name.jpg",
            "nul\0name.jpg",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValidationError,
                "不安全文件名",
            ):
                resolve_managed_media_path(f"uploads/{name}", require_file=False)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_atomic_home_staging_never_follows_junctions(self) -> None:
        outside = self.root / "atomic-outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        junction = self.home / "linked-outside"
        self._create_junction(junction, outside)
        try:
            with self.assertRaisesRegex(ValidationError, "重解析点"):
                with portable.atomic_home_update():
                    pass
            self.assertFalse(
                list(self.root.glob(".mealcircuit-import-staging-*"))
            )
        finally:
            junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_legacy_data_ancestor_junction_is_rejected_for_media_and_database(self) -> None:
        legacy = self.root / "legacy-junction"
        legacy.mkdir()
        outside = self.root / "legacy-outside"
        (outside / "uploads").mkdir(parents=True)
        (outside / "uploads" / "private.jpg").write_bytes(b"private")
        sqlite3.connect(outside / "dietos.db").close()
        data_junction = legacy / "data"
        self._create_junction(data_junction, outside)
        try:
            with self.assertRaisesRegex(ValidationError, "重解析点"):
                migration.migration_preview(legacy)
            with self.assertRaisesRegex(ValidationError, "重解析点"):
                migration.apply_migration(legacy)
        finally:
            data_junction.rmdir()

    def test_migration_manifest_contains_only_promoted_home_paths(self) -> None:
        source = self.root / "legacy-manifest"
        source_upload = source / "data" / "uploads" / "new.jpg"
        source_upload.parent.mkdir(parents=True)
        source_upload.write_bytes(b"new")

        result = migration.apply_migration(source)
        manifest_path = Path(result["manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(str(self.home.resolve()), manifest["target_home"])
        self.assertTrue(manifest["files"])
        for item in manifest["files"]:
            self.assertTrue(str(item["target"]).startswith(str(self.home.resolve())))
            self.assertNotIn(".mealcircuit-import-staging-", item["target"])
        self.assertNotIn(".mealcircuit-import-staging-", manifest_path.read_text(encoding="utf-8"))

    def test_task_context_drops_unmanaged_image_without_resolving_it(self) -> None:
        task_id = "task_unc_context"
        with connect() as connection:
            connection.execute(
                """INSERT INTO tasks(
                       id,type,status,original_input,image_path,created_at,result_version,input_version
                   ) VALUES(?,?,?,?,?,?,0,1)""",
                (
                    task_id,
                    "photo",
                    "pending",
                    "",
                    r"\\attacker.invalid\share\photo.jpg",
                    "2026-07-31T00:00:00Z",
                ),
            )
        with (
            patch(
                "mealcircuit.personalization.require_generation",
                return_value={"policy_version": 1, "fact_only": False},
            ),
            patch(
                "mealcircuit.personalization.active_personalization",
                return_value={
                    "safety": {},
                    "profile": None,
                    "goals": [],
                    "strategy": None,
                    "targets": [],
                },
            ),
            patch(
                "mealcircuit.adaptive.active_adaptations",
                return_value={"confirmed_rules": []},
            ),
            patch("mealcircuit.adaptive.list_inventory", return_value=[]),
            patch("mealcircuit.adaptive.task_evidence_links", return_value=[]),
        ):
            context = service.task_context(task_id)
        self.assertIsNone(context["task"]["image_path"])
        self.assertIs(context["task"]["image_unresolved"], True)

    def test_legacy_migration_rolls_back_files_when_database_step_fails(self) -> None:
        marker = self.home / "original.marker"
        marker.write_text("original", encoding="utf-8")
        source = self.root / "legacy"
        source_upload = source / "data" / "uploads" / "new.jpg"
        source_upload.parent.mkdir(parents=True)
        source_upload.write_bytes(b"new")
        source_database = source / "data" / "dietos.db"
        source_connection = sqlite3.connect(source_database)
        source_connection.close()

        with self.assertRaises(sqlite3.Error):
            migration.apply_migration(source)

        self.assertEqual("original", marker.read_text(encoding="utf-8"))
        self.assertFalse((self.home / "uploads" / "new.jpg").exists())
        self.assertFalse(list(self.root.glob(".mealcircuit-import-staging-*")))
        self.assertFalse(list(self.root.glob(".mealcircuit-import-rollback-*")))


if __name__ == "__main__":
    unittest.main()
