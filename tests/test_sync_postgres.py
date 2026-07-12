from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from sync_server.app import create_app
except ImportError:
    TestClient = None


POSTGRES_URL = os.environ.get("MEALCIRCUIT_SYNC_POSTGRES_TEST_URL")


@unittest.skipUnless(TestClient is not None and POSTGRES_URL, "PostgreSQL integration URL not configured")
class SyncPostgresTest(unittest.TestCase):
    def test_migrated_postgres_accepts_idempotent_cas_and_account_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(POSTGRES_URL, Path(temp) / "blobs", registration_mode="open")
            with TestClient(app) as client:
                login = f"ci-{uuid.uuid4().hex}"
                password = "postgres integration password"
                account = client.post(
                    "/v1/accounts",
                    json={"login_name": login, "password": password, "device_name": "ci"},
                )
                self.assertEqual(account.status_code, 201, account.text)
                headers = {"Authorization": f"Bearer {account.json()['access_token']}"}
                remote_id = hashlib.sha256(login.encode()).hexdigest()
                operation = {
                    "op_id": f"op_{uuid.uuid4()}",
                    "remote_id": remote_id,
                    "base_server_version": 0,
                    "key_version": 1,
                    "envelope": {
                        "envelope_version": 1,
                        "key_version": 1,
                        "nonce": "AA==",
                        "ciphertext": "AQ==",
                    },
                }
                first = client.post("/v1/sync/push", headers=headers, json={"operations": [operation]})
                duplicate = client.post("/v1/sync/push", headers=headers, json={"operations": [operation]})
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(first.json(), duplicate.json())
                self.assertEqual(client.get("/v1/sync/pull?cursor=0", headers=headers).status_code, 200)
                deleted = client.request("DELETE", "/v1/account", headers=headers, json={"password": password})
                self.assertEqual(deleted.status_code, 204, deleted.text)
            app.state.engine.dispose()


if __name__ == "__main__":
    unittest.main()
