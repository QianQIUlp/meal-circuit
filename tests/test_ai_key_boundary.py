from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mealcircuit import agent_cli, ai, secret_store
from mealcircuit.validation import ValidationError


AI_ENVIRONMENT_KEYS = (
    "MEALCIRCUIT_AI_PROVIDER",
    "MEALCIRCUIT_AI_MODEL",
    "MEALCIRCUIT_OPENAI_API_KEY",
    "MEALCIRCUIT_ANTHROPIC_API_KEY",
    "MEALCIRCUIT_DEEPSEEK_API_KEY",
    "MEALCIRCUIT_AI_TIMEOUT_SECONDS",
    "MEALCIRCUIT_AI_MAX_OUTPUT_TOKENS",
    "MEALCIRCUIT_AI_CASE_MODEL",
    "MEALCIRCUIT_AI_PLAN_MODEL",
    "MEALCIRCUIT_AI_REVIEW_MODEL",
)


class FakeKeyringError(Exception):
    pass


class FakeKeyring:
    class Backend:
        priority = 1

    def __init__(self, values: dict[str, str] | None = None, fail_delete: set[str] | None = None):
        self.values = dict(values or {})
        self.fail_delete = set(fail_delete or ())
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []

    def get_keyring(self):
        return self.Backend()

    def get_password(self, service: str, name: str):
        self.get_calls.append(name)
        return self.values.get(name)

    def set_password(self, service: str, name: str, value: str):
        self.set_calls.append((name, value))
        self.values[name] = value

    def delete_password(self, service: str, name: str):
        self.delete_calls.append(name)
        if name in self.fail_delete:
            raise FakeKeyringError(f"cannot delete {name}")
        self.values.pop(name, None)


class AIKeyProcessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._environment = {key: os.environ.get(key) for key in AI_ENVIRONMENT_KEYS}
        for key in AI_ENVIRONMENT_KEYS:
            os.environ.pop(key, None)
        with secret_store._LOCK:
            self._secret_session = dict(secret_store._SESSION)
            secret_store._SESSION.clear()

    def tearDown(self):
        for key, value in self._environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        with secret_store._LOCK:
            secret_store._SESSION.clear()
            secret_store._SESSION.update(self._secret_session)

    def fake_keyring(self, fake: FakeKeyring):
        return mock.patch.object(
            secret_store,
            "_keyring",
            return_value=(fake, FakeKeyringError),
        )

    def test_persisted_ai_credentials_are_not_loaded_as_runtime_configuration(self):
        fake = FakeKeyring({
            "ai.provider": "openai",
            "ai.model": "persisted-model",
            "ai.key.openai": "persisted-api-key",
        })

        with self.fake_keyring(fake):
            status = ai.ai_status()
            with self.assertRaisesRegex(ValidationError, "MEALCIRCUIT_AI_PROVIDER"):
                ai.load_config()

        self.assertIsNone(status["provider"])
        self.assertIsNone(status["model"])
        self.assertFalse(status["key_configured"])
        self.assertEqual([], fake.get_calls)

    def test_legacy_secure_configuration_api_and_cli_fail_closed_without_writing(self):
        fake = FakeKeyring()

        with self.fake_keyring(fake):
            with self.assertRaisesRegex(ValidationError, "当前进程"):
                ai.store_secure_config("openai", "gpt-test", "should-not-persist")

            stderr = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "mealcircuit",
                        "ai-configure-secure",
                        "--provider",
                        "openai",
                        "--model",
                        "gpt-test",
                    ],
                ),
                mock.patch.object(
                    agent_cli.getpass,
                    "getpass",
                    side_effect=AssertionError("disabled command must not request an API key"),
                ),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                agent_cli.main()

        self.assertEqual(2, raised.exception.code)
        self.assertIn("当前进程", stderr.getvalue())
        self.assertEqual([], fake.set_calls)

    def test_legacy_cleanup_reports_any_persistent_deletion_failure(self):
        fake = FakeKeyring(
            {
                "ai.provider": "openai",
                "ai.model": "persisted-model",
                "ai.key.openai": "persisted-api-key",
            },
            fail_delete={"ai.key.openai"},
        )

        with self.fake_keyring(fake):
            with self.assertRaisesRegex(ValidationError, r"ai\.key\.openai"):
                ai.clear_legacy_credentials()

        self.assertIn("ai.key.openai", fake.values)
        self.assertTrue(
            {
                "ai.provider",
                "ai.model",
                "ai.key.openai",
                "ai.key.anthropic",
                "ai.key.deepseek",
            }.issubset(fake.get_calls),
        )

    def test_legacy_cleanup_command_is_explicit_and_deletes_all_known_names(self):
        fake = FakeKeyring({
            "ai.provider": "anthropic",
            "ai.model": "persisted-model",
            "ai.key.anthropic": "persisted-api-key",
        })

        stdout = io.StringIO()
        with self.fake_keyring(fake):
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["mealcircuit", "ai-clear-legacy-credentials"],
                ),
                contextlib.redirect_stdout(stdout),
            ):
                agent_cli.main()
        result = json.loads(stdout.getvalue())

        self.assertTrue(result["cleared"])
        self.assertTrue(result["legacy_credentials"])
        self.assertEqual("anthropic", result["previous_provider"])
        self.assertEqual({}, fake.values)

    def test_configure_runtime_remains_process_local_and_overrides_legacy_values(self):
        fake = FakeKeyring({
            "ai.provider": "anthropic",
            "ai.model": "persisted-model",
            "ai.key.anthropic": "persisted-api-key",
        })

        with self.fake_keyring(fake):
            status = ai.configure_runtime(
                "openai",
                "runtime-model",
                "runtime-api-key",
                timeout_seconds=45,
                max_output_tokens=512,
                case_model="runtime-case-model",
            )
            config = ai.load_config()

        self.assertEqual("openai", status["provider"])
        self.assertEqual("runtime-model", status["model"])
        self.assertTrue(status["key_configured"])
        self.assertEqual("process", status["secure_storage"])
        self.assertEqual("openai", config.provider)
        self.assertEqual("runtime-model", config.model)
        self.assertEqual("runtime-api-key", config.api_key)
        self.assertEqual(45, config.timeout_seconds)
        self.assertEqual(512, config.max_output_tokens)
        self.assertEqual([], fake.get_calls)

    def test_ai_image_encoding_only_reads_bounded_verified_managed_media(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            home = root / "home"
            uploads = home / "uploads"
            uploads.mkdir(parents=True)
            valid = uploads / "valid.png"
            valid_data = b"\x89PNG\r\n\x1a\nmanaged"
            valid.write_bytes(valid_data)
            spoof = uploads / "spoof.png"
            spoof.write_bytes(b"GIF89aspoofed")
            oversized = uploads / "oversized.png"
            oversized.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
            outside = root / "private.png"
            outside.write_bytes(valid_data)

            with mock.patch.dict(os.environ, {"MEALCIRCUIT_HOME": str(home)}):
                data_url = ai._image_data_url(str(valid))
                block = ai._anthropic_image_block(str(valid))
                self.assertTrue(data_url.startswith("data:image/png;base64,"))
                self.assertEqual("image/png", block["source"]["media_type"])

                with self.assertRaisesRegex(ValidationError, "受管媒体目录"):
                    ai._image_data_url(str(outside))
                with self.assertRaisesRegex(ValidationError, "内容与扩展名一致"):
                    ai._image_data_url(str(spoof))
                with mock.patch.object(ai, "MAX_IMAGE_BYTES", 16):
                    with self.assertRaisesRegex(ValidationError, "超过 10MB"):
                        ai._image_data_url(str(oversized))

    def test_http_provider_rejects_redirects_and_bounds_response_bodies(self):
        class ProviderHandler(http.server.BaseHTTPRequestHandler):
            target_hits = 0

            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                type(self).target_hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/large":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(ai.MAX_PROVIDER_RESPONSE_BYTES + 1))
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                error_body = b"x" * (ai.MAX_PROVIDER_ERROR_BYTES + 1)
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(error_body)))
                self.end_headers()
                self.wfile.write(error_body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaisesRegex(ValidationError, "重定向"):
                ai._post_json(f"{base}/redirect", {}, {}, 5)
            self.assertEqual(0, ProviderHandler.target_hits)

            with self.assertRaisesRegex(ValidationError, "成功响应.*超过"):
                ai._post_json(f"{base}/large", {}, {}, 5)
            with self.assertRaisesRegex(ValidationError, "HTTP 400.*安全上限"):
                ai._post_json(f"{base}/error", {}, {}, 5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
