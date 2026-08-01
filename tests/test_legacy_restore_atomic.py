from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mealcircuit import portability
from mealcircuit import portable as portable_module
from mealcircuit.configuration import initialize_private_home
from mealcircuit.db import connect, init_db
from mealcircuit.storage import (
    app_home,
    background_data_operation,
    profile_path,
    resolve_data_path,
    upload_root,
)
from mealcircuit.validation import ValidationError


class LegacyRestoreAtomicityTest(unittest.TestCase):
    ENVIRONMENT_KEYS = (
        "MEALCIRCUIT_HOME",
        "MEALCIRCUIT_DB",
        "DIETOS_DB",
        "MEALCIRCUIT_DOCTRINE",
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.previous_environment = {
            key: os.environ.get(key) for key in self.ENVIRONMENT_KEYS
        }
        os.environ["MEALCIRCUIT_HOME"] = str(self.home)
        for key in self.ENVIRONMENT_KEYS[1:]:
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

    def test_commit_failure_restores_original_home_without_mixed_files(self) -> None:
        profile_path().write_text("bundle profile", encoding="utf-8")
        upload_root().mkdir(parents=True, exist_ok=True)
        media = upload_root() / "state.bin"
        media.write_bytes(b"bundle media")
        bundle = self.root / "bundle.zip"
        portability.export_bundle(bundle)

        profile_path().write_text("current profile", encoding="utf-8")
        media.write_bytes(b"current media")
        real_replace = os.replace

        def reject_staging_promotion(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path.name.startswith(".mealcircuit-import-staging-")
                and destination_path.resolve() == self.home.resolve()
            ):
                raise PermissionError("synthetic Windows directory lock")
            return real_replace(source, destination)

        with (
            patch.object(
                portable_module.os,
                "replace",
                side_effect=reject_staging_promotion,
            ),
            self.assertRaisesRegex(PermissionError, "synthetic Windows directory lock"),
        ):
            portability.restore_bundle(bundle, confirm=True)

        self.assertEqual("current profile", profile_path().read_text(encoding="utf-8"))
        self.assertEqual(b"current media", media.read_bytes())
        self.assertFalse(
            list(self.root.glob(".mealcircuit-import-staging-*")),
            "failed restore left a staging home behind",
        )
        self.assertFalse(
            list(self.root.glob(".mealcircuit-import-rollback-*")),
            "failed restore left a rollback journal behind",
        )
        self.assertEqual("ok", portability.preview_import(bundle)["database_integrity"])

    def test_background_path_resolution_cannot_observe_staging_home(self) -> None:
        update_entered = threading.Event()
        release_update = threading.Event()
        observer_completed = threading.Event()
        observed: list[Path] = []
        failures: list[BaseException] = []

        def update() -> None:
            try:
                with portable_module.atomic_home_update():
                    update_entered.set()
                    if not release_update.wait(5):
                        raise AssertionError("test did not release atomic update")
            except BaseException as exc:
                failures.append(exc)

        def observe() -> None:
            observed.append(app_home())
            observer_completed.set()

        update_thread = threading.Thread(target=update)
        observer_thread = threading.Thread(target=observe)
        update_thread.start()
        self.assertTrue(update_entered.wait(2))
        observer_thread.start()
        self.assertFalse(
            observer_completed.wait(0.25),
            "background path lookup observed the import staging environment",
        )
        release_update.set()
        update_thread.join(timeout=5)
        observer_thread.join(timeout=5)

        self.assertFalse(update_thread.is_alive())
        self.assertFalse(observer_thread.is_alive())
        self.assertEqual([], failures)
        self.assertEqual([self.home.resolve()], observed)

    def test_active_background_operation_rejects_restore_without_freezing_paths(self) -> None:
        worker_entered = threading.Event()
        release_worker = threading.Event()

        def worker() -> None:
            with background_data_operation():
                worker_entered.set()
                release_worker.wait(5)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(worker_entered.wait(2))
        try:
            self.assertEqual(self.home.resolve(), app_home())
            with self.assertRaisesRegex(ValidationError, "后台智能生成仍在运行"):
                with portable_module.atomic_home_update():
                    self.fail("restore must not start while a background operation is active")
        finally:
            release_worker.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(list(self.root.glob(".mealcircuit-import-rollback-*")))

    def test_restore_rejects_background_operation_before_creating_backup(self) -> None:
        profile_path().write_text("bundle profile", encoding="utf-8")
        bundle = self.root / "background-bundle.zip"
        portability.export_bundle(bundle)
        profile_path().write_text("current profile", encoding="utf-8")
        backup_root = self.home / "backups"
        before = set(backup_root.glob("pre-restore-*.zip"))
        worker_entered = threading.Event()
        release_worker = threading.Event()

        def worker() -> None:
            with background_data_operation():
                worker_entered.set()
                release_worker.wait(5)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(worker_entered.wait(2))
        try:
            with self.assertRaisesRegex(ValidationError, "后台智能生成仍在运行"):
                portability.restore_bundle(bundle, confirm=True)
        finally:
            release_worker.set()
            thread.join(timeout=5)

        self.assertEqual("current profile", profile_path().read_text(encoding="utf-8"))
        self.assertEqual(before, set(backup_root.glob("pre-restore-*.zip")))

    def test_second_process_cannot_recover_an_active_import_journal(self) -> None:
        child_environment = os.environ.copy()
        child_environment["MEALCIRCUIT_HOME"] = str(self.home)
        for key in self.ENVIRONMENT_KEYS[1:]:
            child_environment.pop(key, None)

        with portable_module.atomic_home_update():
            profile_path().write_text("committed profile", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from mealcircuit.db import init_db; init_db()",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=child_environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("DataDirectoryBusyError", completed.stderr)

        self.assertEqual("committed profile", profile_path().read_text(encoding="utf-8"))
        self.assertFalse(list(self.root.glob(".mealcircuit-import-rollback-*")))

    def test_preinitialized_second_process_writes_only_after_promotion(self) -> None:
        child_environment = os.environ.copy()
        child_environment["MEALCIRCUIT_HOME"] = str(self.home)
        for key in self.ENVIRONMENT_KEYS[1:]:
            child_environment.pop(key, None)
        child_code = (
            "import sys\n"
            "from mealcircuit.db import connect, init_db\n"
            "init_db()\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "with connect() as connection:\n"
            "    connection.execute("
            "\"INSERT INTO app_metadata(key,value) VALUES('cross_process_gate','written') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value\""
            ")\n"
            "print('written', flush=True)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        completed = threading.Event()
        output: list[str] = []

        def read_completion() -> None:
            assert child.stdout is not None
            output.append(child.stdout.readline().strip())
            completed.set()

        try:
            assert child.stdout is not None
            self.assertEqual("ready", child.stdout.readline().strip())
            with portable_module.atomic_home_update():
                assert child.stdin is not None
                child.stdin.write("write\n")
                child.stdin.flush()
                reader = threading.Thread(target=read_completion)
                reader.start()
                self.assertFalse(
                    completed.wait(0.3),
                    "second process wrote to the original home during promotion",
                )
            self.assertTrue(completed.wait(20))
            reader.join(timeout=5)
            self.assertEqual(["written"], output)
            self.assertEqual(0, child.wait(timeout=10))
            with connect() as connection:
                stored = connection.execute(
                    "SELECT value FROM app_metadata WHERE key='cross_process_gate'"
                ).fetchone()
            self.assertIsNotNone(stored)
            self.assertEqual("written", stored[0])
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def test_second_process_photo_file_and_database_commit_after_promotion(self) -> None:
        bundle = self.root / "photo-race-bundle.zip"
        portability.export_bundle(bundle)
        child_environment = os.environ.copy()
        child_environment["MEALCIRCUIT_HOME"] = str(self.home)
        for key in self.ENVIRONMENT_KEYS[1:]:
            child_environment.pop(key, None)
        child_code = (
            "import io, sys\n"
            "from mealcircuit import service\n"
            "from mealcircuit.db import init_db\n"
            "init_db()\n"
            "real_read_upload = service._read_upload\n"
            "def pause_after_read(stream):\n"
            "    value = real_read_upload(stream)\n"
            "    print('past-init-and-read', flush=True)\n"
            "    sys.stdin.readline()\n"
            "    return value\n"
            "service._read_upload = pause_after_read\n"
            "task = service.create_photo_task("
            "io.BytesIO(b'\\x89PNG\\r\\n\\x1a\\nsynthetic'), "
            "'cross process photo'"
            ")\n"
            "print(task['id'], flush=True)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        completed = threading.Event()
        output: list[str] = []

        def read_completion() -> None:
            assert child.stdout is not None
            output.append(child.stdout.readline().strip())
            completed.set()

        try:
            assert child.stdout is not None
            self.assertEqual("past-init-and-read", child.stdout.readline().strip())
            real_apply = portability._apply_restore_files
            reader: threading.Thread | None = None

            def apply_while_photo_write_waits(files: dict[str, bytes]) -> None:
                nonlocal reader
                assert child.stdin is not None
                child.stdin.write("write\n")
                child.stdin.flush()
                reader = threading.Thread(target=read_completion)
                reader.start()
                self.assertFalse(
                    completed.wait(0.3),
                    "photo bytes were written into the original home during promotion",
                )
                self.assertFalse(list((self.home / "uploads").iterdir()))
                real_apply(files)

            with patch.object(
                portability,
                "_apply_restore_files",
                side_effect=apply_while_photo_write_waits,
            ):
                portability.restore_bundle(bundle, confirm=True)
            self.assertTrue(completed.wait(20))
            assert reader is not None
            reader.join(timeout=5)
            self.assertEqual(0, child.wait(timeout=10))
            self.assertEqual(1, len(output))
            with connect() as connection:
                row = connection.execute(
                    "SELECT image_path FROM tasks WHERE id=?",
                    (output[0],),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(resolve_data_path(row[0]).is_file())
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def test_double_failure_preserves_journal_for_next_startup_recovery(self) -> None:
        marker = self.home / "original.marker"
        marker.write_text("original", encoding="utf-8")
        real_replace = os.replace

        def reject_promotion_and_rollback(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path.name.startswith(".mealcircuit-import-staging-")
                and destination_path.resolve() == self.home.resolve()
            ):
                raise PermissionError("synthetic promotion failure")
            if (
                source_path.name == "previous-home"
                and destination_path.resolve() == self.home.resolve()
            ):
                raise PermissionError("synthetic rollback failure")
            return real_replace(source, destination)

        with (
            patch.object(
                portable_module.os,
                "replace",
                side_effect=reject_promotion_and_rollback,
            ),
            self.assertRaisesRegex(RuntimeError, "自动回滚也失败"),
        ):
            with portable_module.atomic_home_update():
                profile_path().write_text("staged profile", encoding="utf-8")

        journals = list(self.root.glob(".mealcircuit-import-rollback-*"))
        self.assertEqual(1, len(journals))
        self.assertTrue((journals[0] / "previous-home" / marker.name).is_file())
        self.assertFalse(self.home.exists())

        self.assertTrue(portable_module.recover_interrupted_import())
        self.assertEqual("original", marker.read_text(encoding="utf-8"))
        self.assertFalse(list(self.root.glob(".mealcircuit-import-rollback-*")))


if __name__ == "__main__":
    unittest.main()
