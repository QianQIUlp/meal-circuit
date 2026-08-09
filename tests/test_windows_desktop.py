from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mealcircuit import desktop
from mealcircuit import server as server_module


class WindowsDesktopStartupFailureTest(unittest.TestCase):
    def test_windows_package_uses_cloudflare_pages_brand_icon(self):
        root = Path(__file__).resolve().parents[1]
        site_icon = (root / "site" / "favicon.svg").read_text(encoding="utf-8")
        app_icon = (root / "mealcircuit" / "static" / "favicon.svg").read_text(encoding="utf-8")
        self.assertEqual(site_icon, app_icon)

        ico = (root / "packaging" / "windows" / "MealCircuit.ico").read_bytes()
        reserved, image_type, image_count = struct.unpack("<HHH", ico[:6])
        self.assertEqual(0, reserved)
        self.assertEqual(1, image_type)
        self.assertGreaterEqual(image_count, 8)

        spec = (root / "packaging" / "mealcircuit.spec").read_text(encoding="utf-8")
        installer = (root / "packaging" / "windows" / "MealCircuit.iss").read_text(encoding="utf-8")
        self.assertIn('windows_icon = str(root / "packaging" / "windows" / "MealCircuit.ico")', spec)
        self.assertIn("icon=windows_icon", spec)
        self.assertIn('(str(root / "pyproject.toml"), "."),', spec)
        self.assertIn("SetupIconFile=MealCircuit.ico", installer)

    def test_http_access_log_tolerates_windowed_executable_without_stderr(self):
        handler = object.__new__(server_module.Handler)
        with patch.object(server_module.sys, "stderr", None):
            handler.log_message("GET %s", "/setup")

    def test_embedded_server_errors_are_written_to_startup_log(self):
        server = object.__new__(desktop._DesktopHTTPServer)
        with (
            patch.object(desktop.traceback, "format_exc", return_value="request traceback"),
            patch.object(desktop, "_write_startup_error_log") as write_log,
        ):
            server.handle_error(None, ("127.0.0.1", 12345))

        write_log.assert_called_once_with(
            "Embedded loopback request failed:\nrequest traceback"
        )

    def test_loopback_probe_ignores_environment_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"MealCircuit")

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                },
            ):
                desktop._probe_loopback_server(server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_startup_failure_is_logged_but_not_exposed_in_notification(self):
        private_detail = "private-user-path-and-database-detail"
        with tempfile.TemporaryDirectory() as temp_name:
            log_directory = Path(temp_name) / "logs"
            notifications: list[tuple[Path | None, bool]] = []
            with (
                patch.object(desktop, "_run", side_effect=RuntimeError(private_detail)),
                patch.object(desktop, "_startup_log_directories", return_value=(log_directory,)),
                patch.object(
                    desktop,
                    "_show_startup_error",
                    side_effect=lambda path, use_dialog: notifications.append((path, use_dialog)),
                ),
                self.assertRaises(SystemExit) as raised,
            ):
                desktop.main()

            self.assertEqual(1, raised.exception.code)
            self.assertEqual(1, len(notifications))
            log_path, use_dialog = notifications[0]
            self.assertTrue(use_dialog)
            assert log_path is not None
            self.assertTrue(log_path.is_file())
            self.assertIn(private_detail, log_path.read_text(encoding="utf-8"))

    def test_notification_text_contains_no_exception_detail(self):
        private_detail = "do-not-show-this-private-detail"
        messages: list[str] = []
        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(desktop, "_show_windows_error", side_effect=messages.append),
        ):
            desktop._show_startup_error(Path("C:/safe/MealCircuit-startup.log"))

        self.assertEqual(1, len(messages))
        self.assertNotIn(private_detail, messages[0])
        self.assertIn("MealCircuit-startup.log", messages[0])

    def test_smoke_test_failure_does_not_open_a_blocking_dialog(self):
        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(desktop, "_show_windows_error") as show_windows_error,
            patch.object(desktop.sys, "stderr"),
        ):
            desktop._show_startup_error(
                Path("C:/safe/MealCircuit-startup.log"),
                use_dialog=False,
            )

        show_windows_error.assert_not_called()

    def test_log_writer_falls_back_when_local_app_data_is_unwritable(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            unavailable = root / "not-a-directory"
            unavailable.write_text("occupied", encoding="utf-8")
            fallback = root / "fallback"
            with patch.object(
                desktop,
                "_startup_log_directories",
                return_value=(unavailable, fallback),
            ):
                log_path = desktop._write_startup_error_log("traceback detail")

            self.assertIsNotNone(log_path)
            assert log_path is not None
            self.assertEqual(fallback, log_path.parent)
            self.assertEqual("traceback detail", log_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex test")
    def test_same_home_allows_only_one_desktop_instance(self):
        with tempfile.TemporaryDirectory() as temp_name:
            environment = os.environ.copy()
            environment["MEALCIRCUIT_HOME"] = str(Path(temp_name) / "home")
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "from mealcircuit.desktop import _single_instance\n"
                        "with _single_instance():\n"
                        "    print('ready', flush=True)\n"
                        "    input()\n"
                    ),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual("ready", holder.stdout.readline().strip())
                contender = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from mealcircuit.desktop import "
                            "DesktopAlreadyRunningError, _single_instance\n"
                            "try:\n"
                            "    with _single_instance():\n"
                            "        raise SystemExit(3)\n"
                            "except DesktopAlreadyRunningError:\n"
                            "    print('already-running')\n"
                        ),
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(0, contender.returncode, contender.stderr)
                self.assertEqual("already-running", contender.stdout.strip())
            finally:
                if holder.poll() is None:
                    assert holder.stdin is not None
                    holder.stdin.write("\n")
                    holder.stdin.flush()
                    holder.wait(timeout=10)
                for stream in (holder.stdin, holder.stdout, holder.stderr):
                    if stream is not None:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
