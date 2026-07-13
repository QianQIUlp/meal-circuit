from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from sync_server.app import AuthSession, DeviceCursor, ProcessedOperation, create_app, token_hash
except ImportError:  # The base desktop install intentionally has no server dependencies.
    TestClient = None


@unittest.skipIf(TestClient is None, "install the server extra to run sync service tests")
class SyncServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "sync.db"
        self.blobs = root / "blobs"
        self.app = create_app(
            f"sqlite:///{self.database.as_posix()}",
            self.blobs,
            registration_mode="first-user",
            create_schema=True,
        )
        self.client = TestClient(self.app)
        created = self.client.post(
            "/v1/accounts",
            json={
                "login_name": "synthetic-user",
                "password": "correct horse battery staple",
                "device_name": "desktop",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.auth = created.json()
        self.headers = {"Authorization": f"Bearer {self.auth['access_token']}"}

    def tearDown(self) -> None:
        self.client.close()
        self.app.state.engine.dispose()
        self.temp.cleanup()

    def test_registration_auth_recovery_and_refresh_rotation(self) -> None:
        second = self.client.post(
            "/v1/accounts",
            json={"login_name": "second", "password": "another secure password", "device_name": "phone"},
        )
        self.assertEqual(second.status_code, 403)

        envelope = {"version": 1, "key_version": 1, "nonce": "opaque", "ciphertext": "still-opaque"}
        stored = self.client.put("/v1/key-envelopes/recovery", headers=self.headers, json={"envelope": envelope})
        self.assertEqual(stored.status_code, 200)
        fetched = self.client.get("/v1/key-envelopes/recovery", headers=self.headers)
        self.assertEqual(fetched.json()["envelope"], envelope)

        original_refresh = self.auth["refresh_token"]
        refreshed = self.client.post("/v1/sessions/refresh", json={"refresh_token": original_refresh})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        reused = self.client.post("/v1/sessions/refresh", json={"refresh_token": original_refresh})
        self.assertEqual(reused.status_code, 401)
        with self.app.state.session_factory() as session:
            auth = session.scalar(select(AuthSession).where(AuthSession.previous_refresh_hash == token_hash(original_refresh)))
            self.assertIsNotNone(auth)
            self.assertTrue(auth.revoked)

    def test_first_user_registration_stays_closed_after_account_deletion(self) -> None:
        deleted = self.client.request(
            "DELETE",
            "/v1/account",
            headers=self.headers,
            json={"password": "correct horse battery staple"},
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        reopened = self.client.post(
            "/v1/accounts",
            json={"login_name": "replacement", "password": "another secure password", "device_name": "phone"},
        )
        self.assertEqual(reopened.status_code, 403)

    def test_blob_routes_use_injectable_storage_boundary(self) -> None:
        class MemoryBlobStorage:
            def __init__(self):
                self.chunks: dict[tuple[str, str, int], bytes] = {}
                self.deleted_accounts: list[str] = []

            def put_chunk(self, account_id, blob_id, index, data):
                self.chunks[(account_id, blob_id, index)] = data

            def chunk_size(self, account_id, blob_id, index):
                value = self.chunks.get((account_id, blob_id, index))
                return len(value) if value is not None else None

            def read_chunk(self, account_id, blob_id, index):
                return self.chunks.get((account_id, blob_id, index))

            def delete_blob(self, account_id, blob_id):
                for key in list(self.chunks):
                    if key[:2] == (account_id, blob_id):
                        del self.chunks[key]

            def delete_account(self, account_id):
                self.deleted_accounts.append(account_id)
                for key in list(self.chunks):
                    if key[0] == account_id:
                        del self.chunks[key]

        memory = MemoryBlobStorage()
        app = create_app(
            f"sqlite:///{(Path(self.temp.name) / 'memory-storage.db').as_posix()}",
            registration_mode="open",
            create_schema=True,
            blob_storage=memory,
        )
        client = TestClient(app)
        try:
            account = client.post(
                "/v1/accounts",
                json={
                    "login_name": "storage-user",
                    "password": "correct horse battery staple",
                    "device_name": "desktop",
                },
            ).json()
            headers = {"Authorization": f"Bearer {account['access_token']}"}
            blob_id = hashlib.sha256(b"storage boundary").hexdigest()
            created = client.post(
                "/v1/blobs",
                headers=headers,
                json={"blob_id": blob_id, "byte_count": 1, "chunk_count": 1, "key_version": 1},
            )
            self.assertEqual(created.status_code, 201, created.text)
            encrypted_chunk = b"x" * 29  # one plaintext byte plus nonce/tag overhead
            self.assertEqual(
                client.put(f"/v1/blobs/{blob_id}/chunks/0", headers=headers, content=encrypted_chunk).status_code,
                204,
            )
            self.assertEqual(
                client.post(f"/v1/blobs/{blob_id}/complete", headers=headers).status_code,
                200,
            )
            self.assertEqual(
                client.get(f"/v1/blobs/{blob_id}/chunks/0", headers=headers).content,
                encrypted_chunk,
            )
            self.assertEqual(
                client.request(
                    "DELETE",
                    "/v1/account",
                    headers=headers,
                    json={"password": "correct horse battery staple"},
                ).status_code,
                204,
            )
            self.assertEqual(memory.deleted_accounts, [account["account_id"]])
            self.assertEqual(memory.chunks, {})
        finally:
            client.close()
            app.state.engine.dispose()

    def test_push_is_idempotent_conflicts_pull_and_ack(self) -> None:
        remote_id = hashlib.sha256(b"opaque entity").hexdigest()
        envelope = {"envelope_version": 1, "key_version": 1, "nonce": "AA==", "ciphertext": "AQ=="}
        operation = {
            "op_id": "op_00000000-0000-4000-8000-000000000001",
            "remote_id": remote_id,
            "base_server_version": 0,
            "key_version": 1,
            "envelope": envelope,
        }
        first = self.client.post("/v1/sync/push", headers=self.headers, json={"operations": [operation]})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["results"][0]["status"], "accepted")
        duplicate = self.client.post("/v1/sync/push", headers=self.headers, json={"operations": [operation]})
        self.assertEqual(duplicate.json(), first.json())

        stale = {**operation, "op_id": "op_00000000-0000-4000-8000-000000000002"}
        conflict = self.client.post("/v1/sync/push", headers=self.headers, json={"operations": [stale]})
        self.assertEqual(conflict.json()["results"][0]["status"], "conflict")
        self.assertEqual(conflict.json()["results"][0]["envelope"], envelope)

        pulled = self.client.get("/v1/sync/pull?cursor=0", headers=self.headers)
        self.assertEqual(pulled.status_code, 200)
        self.assertEqual(pulled.json()["cursor"], 1)
        self.assertEqual(len(pulled.json()["changes"]), 1)
        acknowledged = self.client.post("/v1/sync/ack", headers=self.headers, json={"cursor": 1})
        self.assertEqual(acknowledged.json(), {"cursor": 1})
        snapshot = self.client.get("/v1/sync/pull?cursor=0", headers=self.headers)
        self.assertTrue(snapshot.json()["requires_full_resync"])
        self.assertEqual(snapshot.json()["changes"][0]["envelope"], envelope)
        self.assertEqual(snapshot.json()["cursor"], 1)
        with self.app.state.session_factory() as session:
            self.assertEqual(session.query(ProcessedOperation).count(), 2)
            cursor = session.get(DeviceCursor, (self.auth["account_id"], self.auth["device_id"]))
            self.assertEqual(cursor.cursor_value, 1)

    def test_pairing_blob_and_device_revocation(self) -> None:
        claim_token = "pairing-secret-with-more-than-thirty-two-characters"
        created = self.client.post(
            "/v1/pairings",
            headers=self.headers,
            json={
                "claim_token_hash": token_hash(claim_token),
                "envelope": {"ciphertext": "device-wrapped-account-key"},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        phone = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "synthetic-user",
                "password": "correct horse battery staple",
                "device_name": "android",
            },
        ).json()
        phone_headers = {"Authorization": f"Bearer {phone['access_token']}"}
        claimed = self.client.post(
            f"/v1/pairings/{created.json()['pairing_id']}/claim",
            headers=phone_headers,
            json={"claim_token": claim_token},
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        claimed_again = self.client.post(
            f"/v1/pairings/{created.json()['pairing_id']}/claim",
            headers=phone_headers,
            json={"claim_token": claim_token},
        )
        self.assertEqual(claimed_again.status_code, 410)

        blob_id = hashlib.sha256(b"opaque blob").hexdigest()
        blob = self.client.post(
            "/v1/blobs",
            headers=self.headers,
            json={"blob_id": blob_id, "byte_count": 17, "chunk_count": 1, "key_version": 1},
        )
        self.assertEqual(blob.status_code, 201, blob.text)
        self.assertEqual(
            self.client.get(f"/v1/blobs/{blob_id}/chunks/0", headers=self.headers).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/v1/blobs/{blob_id}/complete", headers=self.headers).status_code,
            409,
        )
        chunk = b"x" * (17 + 28)
        uploaded = self.client.put(f"/v1/blobs/{blob_id}/chunks/0", headers=self.headers, content=chunk)
        self.assertEqual(uploaded.status_code, 204, uploaded.text)
        completed = self.client.post(f"/v1/blobs/{blob_id}/complete", headers=self.headers)
        self.assertEqual(completed.status_code, 200, completed.text)
        downloaded = self.client.get(f"/v1/blobs/{blob_id}/chunks/0", headers=self.headers)
        self.assertEqual(downloaded.content, chunk)
        self.assertEqual(
            self.client.put(f"/v1/blobs/{blob_id}/chunks/0", headers=self.headers, content=chunk).status_code,
            409,
        )

        revoked = self.client.delete(f"/v1/devices/{phone['device_id']}", headers=self.headers)
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(self.client.get("/v1/devices", headers=phone_headers).status_code, 401)

    def test_server_persistence_contains_no_domain_plaintext_canary(self) -> None:
        # Only ciphertext-like bytes are ever submitted; the domain canary remains client-side.
        domain_canary = "SYNTHETIC-PRIVATE-MEAL-CANARY"
        ciphertext = hashlib.sha256(domain_canary.encode()).hexdigest()
        remote_id = hashlib.sha256(b"opaque canary id").hexdigest()
        response = self.client.post(
            "/v1/sync/push",
            headers=self.headers,
            json={
                "operations": [
                    {
                        "op_id": "op_00000000-0000-4000-8000-000000000003",
                        "remote_id": remote_id,
                        "base_server_version": 0,
                        "key_version": 1,
                        "envelope": {"ciphertext": ciphertext},
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        persisted = self.database.read_bytes()
        self.assertNotIn(domain_canary.encode(), persisted)
        for path in self.blobs.rglob("*"):
            if path.is_file():
                self.assertNotIn(domain_canary.encode(), path.read_bytes())

    def test_key_rotation_replaces_remote_epoch_and_revokes_other_devices(self) -> None:
        old_id = hashlib.sha256(b"old opaque entity").hexdigest()
        old_operation = {
            "op_id": "op_00000000-0000-4000-8000-000000000010",
            "remote_id": old_id,
            "base_server_version": 0,
            "key_version": 1,
            "envelope": {"envelope_version": 1, "key_version": 1, "nonce": "AA==", "ciphertext": "AQ=="},
        }
        self.assertEqual(
            self.client.post("/v1/sync/push", headers=self.headers, json={"operations": [old_operation]}).status_code,
            200,
        )
        phone = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "synthetic-user",
                "password": "correct horse battery staple",
                "device_name": "phone",
            },
        ).json()
        phone_headers = {"Authorization": f"Bearer {phone['access_token']}"}

        begun = self.client.post("/v1/key-rotations", headers=self.headers, json={})
        self.assertEqual(begun.status_code, 201, begun.text)
        self.assertEqual(begun.json()["target_key_version"], 2)
        self.assertEqual(self.client.get("/v1/sync/pull?cursor=0", headers=phone_headers).status_code, 409)

        new_id = hashlib.sha256(b"new opaque entity").hexdigest()
        new_operation = {
            "op_id": "op_00000000-0000-4000-8000-000000000011",
            "remote_id": new_id,
            "base_server_version": 0,
            "key_version": 2,
            "envelope": {"envelope_version": 1, "key_version": 2, "nonce": "Ag==", "ciphertext": "Aw=="},
        }
        staged = self.client.post("/v1/sync/push", headers=self.headers, json={"operations": [new_operation]})
        self.assertEqual(staged.status_code, 200, staged.text)
        self.assertEqual(staged.json()["results"][0]["status"], "accepted")
        rejected_old = self.client.post(
            "/v1/sync/push",
            headers=self.headers,
            json={"operations": [{**old_operation, "op_id": "op_00000000-0000-4000-8000-000000000012"}]},
        )
        self.assertEqual(rejected_old.status_code, 409)

        blob_id = hashlib.sha256(b"rotated blob").hexdigest()
        self.assertEqual(
            self.client.post(
                "/v1/blobs",
                headers=self.headers,
                json={"blob_id": blob_id, "byte_count": 4, "chunk_count": 1, "key_version": 2},
            ).status_code,
            201,
        )
        self.assertEqual(self.client.put(f"/v1/blobs/{blob_id}/chunks/0", headers=self.headers, content=b"x" * (4 + 28)).status_code, 204)
        self.assertEqual(self.client.post(f"/v1/blobs/{blob_id}/complete", headers=self.headers).status_code, 200)
        recovery = {"version": 1, "key_version": 2, "nonce": "opaque2", "ciphertext": "cipher2"}
        commit_body = {"key_version": 2, "recovery_envelope": recovery, "entity_count": 1, "blob_count": 1}
        committed = self.client.post("/v1/key-rotations/current/commit", headers=self.headers, json=commit_body)
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(committed.json()["active_key_version"], 2)
        self.assertEqual(committed.json()["revoked_devices"], 1)
        repeated = self.client.post("/v1/key-rotations/current/commit", headers=self.headers, json=commit_body)
        self.assertTrue(repeated.json()["already_committed"])
        self.assertEqual(self.client.get("/v1/devices", headers=phone_headers).status_code, 401)
        snapshot = self.client.get("/v1/sync/pull?cursor=0", headers=self.headers).json()
        self.assertTrue(snapshot["requires_full_resync"])
        self.assertEqual([item["remote_id"] for item in snapshot["changes"]], [new_id])
        self.assertEqual(
            self.client.get("/v1/key-envelopes/recovery", headers=self.headers).json()["envelope"], recovery
        )

    def test_key_rotation_abort_removes_only_staged_epoch(self) -> None:
        begun = self.client.post("/v1/key-rotations", headers=self.headers, json={})
        self.assertEqual(begun.status_code, 201)
        staged_id = hashlib.sha256(b"aborted epoch").hexdigest()
        operation = {
            "op_id": "op_00000000-0000-4000-8000-000000000020",
            "remote_id": staged_id,
            "base_server_version": 0,
            "key_version": 2,
            "envelope": {"envelope_version": 1, "key_version": 2, "nonce": "AA==", "ciphertext": "AQ=="},
        }
        self.assertEqual(
            self.client.post("/v1/sync/push", headers=self.headers, json={"operations": [operation]}).status_code,
            200,
        )
        self.assertEqual(self.client.delete("/v1/key-rotations/current", headers=self.headers).status_code, 204)
        status_value = self.client.get("/v1/key-rotations/current", headers=self.headers).json()
        self.assertFalse(status_value["in_progress"])
        self.assertEqual(status_value["active_key_version"], 1)
        self.assertEqual(self.client.get("/v1/sync/pull?cursor=0", headers=self.headers).json()["changes"], [])

    def test_capabilities_report_configured_limits_and_enforce_batch(self) -> None:
        root = Path(self.temp.name) / "limited"
        root.mkdir()
        with patch.dict(
            os.environ,
            {
                "MEALCIRCUIT_SYNC_MAX_BATCH": "1",
                "MEALCIRCUIT_SYNC_MAX_PULL": "7",
                "MEALCIRCUIT_SYNC_MAX_ENTITY_BYTES": "2048",
                "MEALCIRCUIT_SYNC_MAX_BLOB_BYTES": "4096",
            },
        ):
            app = create_app(
                f"sqlite:///{(root / 'limited.db').as_posix()}",
                root / "blobs",
                registration_mode="open",
                create_schema=True,
            )
        with TestClient(app) as client:
            capabilities = client.get("/v1/capabilities").json()
            self.assertEqual(
                {key: capabilities[key] for key in ("max_batch", "max_pull", "max_entity_bytes", "max_blob_bytes")},
                {"max_batch": 1, "max_pull": 7, "max_entity_bytes": 2048, "max_blob_bytes": 4096},
            )
            account = client.post(
                "/v1/accounts",
                json={"login_name": "limited", "password": "limited secure password", "device_name": "test"},
            ).json()
            headers = {"Authorization": f"Bearer {account['access_token']}"}
            operation = {
                "op_id": "op_00000000-0000-4000-8000-000000000030",
                "remote_id": hashlib.sha256(b"limited").hexdigest(),
                "base_server_version": 0,
                "key_version": 1,
                "envelope": {"ciphertext": "opaque"},
            }
            response = client.post(
                "/v1/sync/push", headers=headers, json={"operations": [operation, {**operation, "op_id": "op_00000000-0000-4000-8000-000000000031"}]}
            )
            self.assertEqual(response.status_code, 413)
        app.state.engine.dispose()


if __name__ == "__main__":
    unittest.main()
