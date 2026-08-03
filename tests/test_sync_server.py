from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from sync_server.app import (
        Account,
        AuthSession,
        Blob,
        Change,
        Device,
        DeviceCursor,
        MAX_CHUNK_BYTES,
        Pairing,
        ProcessedOperation,
        RemoteEntity,
        create_app,
        now,
        token_hash,
    )
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

    def _store_recovery(self, key_version: int = 1) -> dict:
        envelope = {
            "version": 1,
            "key_version": key_version,
            "nonce": f"opaque-{key_version}",
            "ciphertext": f"ciphertext-{key_version}",
        }
        response = self.client.put(
            "/v1/key-envelopes/recovery",
            headers=self.headers,
            json={"envelope": envelope},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return envelope

    def _push_entity(self, *, key_version: int, operation_number: int) -> str:
        remote_id = hashlib.sha256(f"opaque entity {operation_number}".encode()).hexdigest()
        operation = {
            "op_id": f"op_00000000-0000-4000-8000-{operation_number:012d}",
            "remote_id": remote_id,
            "base_server_version": 0,
            "key_version": key_version,
            "envelope": {
                "envelope_version": 1,
                "key_version": key_version,
                "nonce": "AA==",
                "ciphertext": "AQ==",
            },
        }
        response = self.client.post(
            "/v1/sync/push",
            headers=self.headers,
            json={"operations": [operation]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["results"][0]["status"], "accepted")
        return remote_id

    def _put_complete_blob(self, *, key_version: int, label: str, byte_count: int = 1) -> str:
        blob_id = hashlib.sha256(label.encode()).hexdigest()
        created = self.client.post(
            "/v1/blobs",
            headers=self.headers,
            json={
                "blob_id": blob_id,
                "byte_count": byte_count,
                "chunk_count": 1,
                "key_version": key_version,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        uploaded = self.client.put(
            f"/v1/blobs/{blob_id}/chunks/0",
            headers=self.headers,
            content=b"x" * (byte_count + 28),
        )
        self.assertEqual(uploaded.status_code, 204, uploaded.text)
        completed = self.client.post(f"/v1/blobs/{blob_id}/complete", headers=self.headers)
        self.assertEqual(completed.status_code, 200, completed.text)
        return blob_id

    def test_registration_auth_recovery_and_refresh_rotation(self) -> None:
        self.assertFalse(self.auth["recovery_configured"])
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
        signed_in = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "synthetic-user",
                "password": "correct horse battery staple",
                "device_name": "second desktop",
            },
        )
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        self.assertTrue(signed_in.json()["recovery_configured"])

        original_refresh = self.auth["refresh_token"]
        refreshed = self.client.post("/v1/sessions/refresh", json={"refresh_token": original_refresh})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertTrue(refreshed.json()["recovery_configured"])
        reused = self.client.post("/v1/sessions/refresh", json={"refresh_token": original_refresh})
        self.assertEqual(reused.status_code, 401)
        with self.app.state.session_factory() as session:
            auth = session.scalar(select(AuthSession).where(AuthSession.previous_refresh_hash == token_hash(original_refresh)))
            self.assertIsNotNone(auth)
            self.assertTrue(auth.revoked)

    def test_recovery_envelope_put_is_idempotent_but_cannot_replace_the_data_key(self) -> None:
        envelope = self._store_recovery()
        repeated = self.client.put(
            "/v1/key-envelopes/recovery",
            headers=self.headers,
            json={"envelope": envelope},
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)

        replacement = dict(envelope)
        replacement["nonce"] = "different-key-material"
        rejected = self.client.put(
            "/v1/key-envelopes/recovery",
            headers=self.headers,
            json={"envelope": replacement},
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(
            rejected.json()["detail"],
            "recovery envelope already configured; use key rotation to replace it",
        )
        self.assertEqual(
            self.client.get("/v1/key-envelopes/recovery", headers=self.headers).json()["envelope"],
            envelope,
        )

    def test_recovery_initialization_is_rejected_after_remote_entity_exists(self) -> None:
        remote_id = hashlib.sha256(b"legacy opaque entity").hexdigest()
        with self.app.state.session_factory() as session:
            session.add(
                RemoteEntity(
                    account_id=self.auth["account_id"],
                    remote_id=remote_id,
                    server_version=1,
                    key_version=1,
                    envelope_json='{"ciphertext":"AQ==","envelope_version":1,"key_version":1,"nonce":"AA=="}',
                    updated_at=now(),
                )
            )
            session.commit()

        response = self.client.put(
            "/v1/key-envelopes/recovery",
            headers=self.headers,
            json={
                "envelope": {
                    "version": 1,
                    "key_version": 1,
                    "nonce": "too-late",
                    "ciphertext": "too-late",
                }
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get("/v1/key-envelopes/recovery", headers=self.headers).status_code, 404)

    def test_recovery_initialization_is_rejected_after_incomplete_blob_exists(self) -> None:
        blob_id = hashlib.sha256(b"incomplete opaque blob").hexdigest()
        with self.app.state.session_factory() as session:
            session.add(
                Blob(
                    account_id=self.auth["account_id"],
                    blob_id=blob_id,
                    byte_count=1,
                    chunk_count=1,
                    key_version=1,
                    complete=False,
                    created_at=now(),
                )
            )
            session.commit()

        response = self.client.put(
            "/v1/key-envelopes/recovery",
            headers=self.headers,
            json={
                "envelope": {
                    "version": 1,
                    "key_version": 1,
                    "nonce": "too-late",
                    "ciphertext": "too-late",
                }
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get("/v1/key-envelopes/recovery", headers=self.headers).status_code, 404)

    def test_encrypted_write_endpoints_require_recovery_configuration(self) -> None:
        remote_id = hashlib.sha256(b"blocked entity before recovery").hexdigest()
        operation = {
            "op_id": "op_00000000-0000-4000-8000-000000000041",
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
        pushed = self.client.post(
            "/v1/sync/push",
            headers=self.headers,
            json={"operations": [operation]},
        )
        self.assertEqual(pushed.status_code, 409, pushed.text)
        self.assertEqual(
            pushed.json()["detail"],
            "recovery envelope must be configured before uploading encrypted data",
        )

        blob_id = hashlib.sha256(b"blocked blob before recovery").hexdigest()
        created = self.client.post(
            "/v1/blobs",
            headers=self.headers,
            json={"blob_id": blob_id, "byte_count": 1, "chunk_count": 1, "key_version": 1},
        )
        self.assertEqual(created.status_code, 409, created.text)

        # Seed a legacy incomplete row so the chunk and completion endpoints are
        # independently covered even though new blob creation is now blocked.
        with self.app.state.session_factory() as session:
            session.add(
                Blob(
                    account_id=self.auth["account_id"],
                    blob_id=blob_id,
                    byte_count=1,
                    chunk_count=1,
                    key_version=1,
                    complete=False,
                    created_at=now(),
                )
            )
            session.commit()
        uploaded = self.client.put(
            f"/v1/blobs/{blob_id}/chunks/0",
            headers=self.headers,
            content=b"x" * 29,
        )
        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        completed = self.client.post(f"/v1/blobs/{blob_id}/complete", headers=self.headers)
        self.assertEqual(completed.status_code, 409, completed.text)

        with self.app.state.session_factory() as session:
            self.assertIsNone(session.get(RemoteEntity, (self.auth["account_id"], remote_id)))

    def test_recovery_configuration_unlocks_encrypted_writes(self) -> None:
        self._store_recovery()
        self._push_entity(key_version=1, operation_number=42)
        self._put_complete_blob(key_version=1, label="blob after recovery")

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
            recovery = client.put(
                "/v1/key-envelopes/recovery",
                headers=headers,
                json={
                    "envelope": {
                        "version": 1,
                        "key_version": 1,
                        "nonce": "opaque",
                        "ciphertext": "opaque",
                    }
                },
            )
            self.assertEqual(recovery.status_code, 200, recovery.text)
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
        self._store_recovery()
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
        self._store_recovery()
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
        oversized = self.client.put(
            f"/v1/blobs/{blob_id}/chunks/0",
            headers=self.headers,
            content=b"x" * (MAX_CHUNK_BYTES + 1),
        )
        self.assertEqual(oversized.status_code, 413, oversized.text)
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
        self._store_recovery()
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
        self._store_recovery()
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
        old_blob_id = self._put_complete_blob(key_version=1, label="old rotated blob", byte_count=4)
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
        blocked_owner_revocation = self.client.delete(
            f"/v1/devices/{self.auth['device_id']}",
            headers=phone_headers,
        )
        self.assertEqual(blocked_owner_revocation.status_code, 409, blocked_owner_revocation.text)
        blocked_owner_logout = self.client.delete(
            "/v1/sessions/current",
            headers=self.headers,
        )
        self.assertEqual(blocked_owner_logout.status_code, 409, blocked_owner_logout.text)
        rotation_after_blocked_deletes = self.client.get(
            "/v1/key-rotations/current",
            headers=self.headers,
        )
        self.assertEqual(rotation_after_blocked_deletes.status_code, 200, rotation_after_blocked_deletes.text)
        self.assertTrue(rotation_after_blocked_deletes.json()["in_progress"])
        self.assertTrue(rotation_after_blocked_deletes.json()["owned_by_current_device"])

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
        self.assertEqual(
            self.client.put(
                f"/v1/blobs/{blob_id}/chunks/0",
                headers=phone_headers,
                content=b"x" * (4 + 28),
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/v1/blobs/{blob_id}/complete",
                headers=phone_headers,
            ).status_code,
            409,
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
        self.assertEqual(self.client.get(f"/v1/blobs/{old_blob_id}/chunks/0", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/blobs/{blob_id}/chunks/0", headers=self.headers).status_code, 200)

    def test_key_rotation_rejects_smaller_entity_inventory(self) -> None:
        self._store_recovery()
        self._push_entity(key_version=1, operation_number=50)
        self._push_entity(key_version=1, operation_number=51)
        begun = self.client.post("/v1/key-rotations", headers=self.headers, json={})
        self.assertEqual(begun.status_code, 201, begun.text)
        self._push_entity(key_version=2, operation_number=52)

        response = self.client.post(
            "/v1/key-rotations/current/commit",
            headers=self.headers,
            json={
                "key_version": 2,
                "recovery_envelope": {
                    "version": 1,
                    "key_version": 2,
                    "nonce": "rotated",
                    "ciphertext": "rotated",
                },
                "entity_count": 1,
                "blob_count": 0,
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        rotation = self.client.get("/v1/key-rotations/current", headers=self.headers).json()
        self.assertTrue(rotation["in_progress"])
        self.assertEqual(rotation["active_key_version"], 1)

    def test_key_rotation_rejects_smaller_blob_inventory(self) -> None:
        self._store_recovery()
        self._put_complete_blob(key_version=1, label="old blob one")
        self._put_complete_blob(key_version=1, label="old blob two")
        begun = self.client.post("/v1/key-rotations", headers=self.headers, json={})
        self.assertEqual(begun.status_code, 201, begun.text)
        self._put_complete_blob(key_version=2, label="only one rotated blob")

        response = self.client.post(
            "/v1/key-rotations/current/commit",
            headers=self.headers,
            json={
                "key_version": 2,
                "recovery_envelope": {
                    "version": 1,
                    "key_version": 2,
                    "nonce": "rotated",
                    "ciphertext": "rotated",
                },
                "entity_count": 0,
                "blob_count": 1,
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        rotation = self.client.get("/v1/key-rotations/current", headers=self.headers).json()
        self.assertTrue(rotation["in_progress"])
        self.assertEqual(rotation["active_key_version"], 1)

    def test_key_rotation_abort_removes_only_staged_epoch(self) -> None:
        self._store_recovery()
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

    def test_key_rotation_lease_renews_for_owner_and_expiry_allows_takeover(self) -> None:
        self._store_recovery()
        phone = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "synthetic-user",
                "password": "correct horse battery staple",
                "device_name": "phone",
            },
        )
        self.assertEqual(phone.status_code, 200, phone.text)
        phone_headers = {"Authorization": f"Bearer {phone.json()['access_token']}"}
        begun = self.client.post("/v1/key-rotations", headers=self.headers, json={})
        self.assertEqual(begun.status_code, 201, begun.text)

        blocked = self.client.post("/v1/key-rotations", headers=phone_headers, json={})
        self.assertEqual(blocked.status_code, 409, blocked.text)
        with self.app.state.session_factory() as session:
            account = session.get(Account, self.auth["account_id"])
            account.rotation_started_at = now() - timedelta(minutes=1)
            previous_heartbeat = account.rotation_started_at
            session.commit()
        owner_status = self.client.get("/v1/key-rotations/current", headers=self.headers)
        self.assertEqual(owner_status.status_code, 200, owner_status.text)
        self.assertTrue(owner_status.json()["owned_by_current_device"])
        with self.app.state.session_factory() as session:
            account = session.get(Account, self.auth["account_id"])
            heartbeat = account.rotation_started_at
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=previous_heartbeat.tzinfo)
            self.assertGreater(heartbeat, previous_heartbeat)

        staged_entity = self._push_entity(key_version=2, operation_number=700)
        staged_blob = self._put_complete_blob(key_version=2, label="expired rotation blob")
        self.assertTrue((self.blobs / self.auth["account_id"] / staged_blob).is_dir())
        with self.app.state.session_factory() as session:
            account = session.get(Account, self.auth["account_id"])
            account.rotation_started_at = now() - timedelta(
                minutes=self.app.state.limits["rotation_lease_minutes"] + 1
            )
            session.commit()

        recovered_login = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "synthetic-user",
                "password": "correct horse battery staple",
                "device_name": "replacement-owner",
            },
        )
        self.assertEqual(recovered_login.status_code, 200, recovered_login.text)
        recovered_headers = {
            "Authorization": f"Bearer {recovered_login.json()['access_token']}"
        }
        takeover = self.client.post("/v1/key-rotations", headers=recovered_headers, json={})
        self.assertEqual(takeover.status_code, 201, takeover.text)
        self.assertTrue(takeover.json()["owned_by_current_device"])
        self.assertEqual(takeover.json()["target_key_version"], 2)
        with self.app.state.session_factory() as session:
            self.assertIsNone(
                session.get(RemoteEntity, (self.auth["account_id"], staged_entity))
            )
            self.assertIsNone(session.get(Blob, (self.auth["account_id"], staged_blob)))
            self.assertEqual(
                session.query(ProcessedOperation)
                .filter(ProcessedOperation.account_id == self.auth["account_id"])
                .count(),
                0,
            )
        self.assertFalse((self.blobs / self.auth["account_id"] / staged_blob).exists())
        old_owner = self.client.get("/v1/key-rotations/current", headers=self.headers)
        self.assertEqual(old_owner.status_code, 200, old_owner.text)
        self.assertFalse(old_owner.json()["owned_by_current_device"])

    def test_full_resync_keeps_a_durable_cursor_and_replays_concurrent_writes(self) -> None:
        self._store_recovery()
        original_ids = [
            self._push_entity(key_version=1, operation_number=number)
            for number in (100, 101, 102)
        ]
        with self.app.state.session_factory() as session:
            session.query(Change).filter(Change.account_id == self.auth["account_id"]).delete()
            session.commit()

        first = self.client.get("/v1/sync/pull?cursor=0&limit=1", headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        first_page = first.json()
        self.assertTrue(first_page["requires_full_resync"])
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["cursor"], 0)
        with self.app.state.session_factory() as session:
            state = session.get(DeviceCursor, (self.auth["account_id"], self.auth["device_id"]))
            self.assertIsNotNone(state)
            self.assertEqual(state.cursor_value, -4)

        restarted = self.client.get("/v1/sync/pull?cursor=0&limit=1", headers=self.headers).json()
        self.assertEqual(restarted["changes"], first_page["changes"])
        self.assertEqual(restarted["cursor"], 0)

        inserted_number = next(
            number
            for number in range(1_000, 10_000)
            if hashlib.sha256(f"opaque entity {number}".encode()).hexdigest()
            < first_page["changes"][0]["remote_id"]
        )
        concurrent_id = self._push_entity(key_version=1, operation_number=inserted_number)

        snapshot_ids = [first_page["changes"][0]["remote_id"]]
        page = first_page
        while page["has_more"]:
            response = self.client.get(
                "/v1/sync/pull",
                headers=self.headers,
                params={
                    "cursor": page["cursor"],
                    "limit": 1,
                    "snapshot_offset": page["snapshot_offset"],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            snapshot_ids.extend(item["remote_id"] for item in page["changes"])
        self.assertEqual(page["cursor"], 3)
        acknowledged = self.client.post(
            "/v1/sync/ack", headers=self.headers, json={"cursor": page["cursor"]}
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)

        incremental = self.client.get("/v1/sync/pull?cursor=3&limit=10", headers=self.headers)
        self.assertEqual(incremental.status_code, 200, incremental.text)
        incremental_ids = [item["remote_id"] for item in incremental.json()["changes"]]
        self.assertIn(concurrent_id, incremental_ids)
        self.assertEqual(incremental.json()["cursor"], 4)
        self.assertTrue(set(original_ids).issubset(set(snapshot_ids) | set(incremental_ids)))

    def test_request_field_and_argon2_work_limits(self) -> None:
        oversized = self.client.post(
            "/v1/sessions",
            content=json.dumps({"padding": "x" * (17 * 1024)}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(oversized.status_code, 413, oversized.text)
        invalid_fields = self.client.post(
            "/v1/sessions",
            json={
                "login_name": "x" * 121,
                "password": "x" * 513,
                "device_name": "x" * 161,
            },
        )
        self.assertEqual(invalid_fields.status_code, 422, invalid_fields.text)

        acquired = 0
        for _ in range(self.app.state.limits["argon2_concurrency"]):
            self.assertTrue(self.app.state.argon2_gate.acquire(blocking=False))
            acquired += 1
        try:
            busy = self.client.post(
                "/v1/sessions",
                json={
                    "login_name": "synthetic-user",
                    "password": "correct horse battery staple",
                    "device_name": "blocked",
                },
            )
        finally:
            for _ in range(acquired):
                self.app.state.argon2_gate.release()
        self.assertEqual(busy.status_code, 429, busy.text)

        root = Path(self.temp.name) / "auth-limited"
        root.mkdir()
        with patch.dict(
            os.environ,
            {
                "MEALCIRCUIT_SYNC_AUTH_RATE_LIMIT": "2",
                "MEALCIRCUIT_SYNC_ARGON2_CONCURRENCY": "1",
            },
        ):
            app = create_app(
                f"sqlite:///{(root / 'auth.db').as_posix()}",
                root / "blobs",
                registration_mode="open",
                create_schema=True,
            )
        with TestClient(app) as client:
            created = client.post(
                "/v1/accounts",
                json={
                    "login_name": "rate-user",
                    "password": "correct horse battery staple",
                    "device_name": "desktop",
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            for _ in range(2):
                rejected = client.post(
                    "/v1/sessions",
                    json={
                        "login_name": "rate-user",
                        "password": "wrong password",
                        "device_name": "attacker",
                    },
                )
                self.assertEqual(rejected.status_code, 401, rejected.text)
            limited = client.post(
                "/v1/sessions",
                json={
                    "login_name": "rate-user",
                    "password": "wrong password",
                    "device_name": "attacker",
                },
            )
            self.assertEqual(limited.status_code, 429, limited.text)
            self.assertTrue(app.state.argon2_gate.acquire(blocking=False))
            try:
                registration_busy = client.post(
                    "/v1/accounts",
                    json={
                        "login_name": "second-user",
                        "password": "another secure password",
                        "device_name": "desktop",
                    },
                )
            finally:
                app.state.argon2_gate.release()
            self.assertEqual(registration_busy.status_code, 429, registration_busy.text)
        app.state.engine.dispose()

    def test_account_row_quotas_and_stale_cleanup(self) -> None:
        root = Path(self.temp.name) / "row-quotas"
        root.mkdir()
        with patch.dict(
            os.environ,
            {
                "MEALCIRCUIT_SYNC_MAX_ENTITIES": "1",
                "MEALCIRCUIT_SYNC_MAX_OPERATIONS": "2",
                "MEALCIRCUIT_SYNC_MAX_DEVICES": "2",
                "MEALCIRCUIT_SYNC_MAX_PAIRINGS": "1",
                "MEALCIRCUIT_SYNC_MAX_INCOMPLETE_BLOBS": "1",
                "MEALCIRCUIT_SYNC_INCOMPLETE_BLOB_HOURS": "1",
            },
        ):
            app = create_app(
                f"sqlite:///{(root / 'quota.db').as_posix()}",
                root / "blobs",
                registration_mode="open",
                create_schema=True,
            )
        with TestClient(app) as client:
            account = client.post(
                "/v1/accounts",
                json={
                    "login_name": "quota-user",
                    "password": "correct horse battery staple",
                    "device_name": "desktop",
                },
            ).json()
            headers = {"Authorization": f"Bearer {account['access_token']}"}
            recovery = client.put(
                "/v1/key-envelopes/recovery",
                headers=headers,
                json={
                    "envelope": {
                        "version": 1,
                        "key_version": 1,
                        "nonce": "quota",
                        "ciphertext": "quota",
                    }
                },
            )
            self.assertEqual(recovery.status_code, 200, recovery.text)

            def operation(number: int, remote_id: str, base: int = 0) -> dict:
                return {
                    "op_id": f"op_00000000-0000-4000-8000-{number:012d}",
                    "remote_id": remote_id,
                    "base_server_version": base,
                    "key_version": 1,
                    "envelope": {"ciphertext": f"opaque-{number}"},
                }

            first_id = hashlib.sha256(b"quota-first").hexdigest()
            second_id = hashlib.sha256(b"quota-second").hexdigest()
            accepted = client.post(
                "/v1/sync/push", headers=headers, json={"operations": [operation(1, first_id)]}
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            entity_limited = client.post(
                "/v1/sync/push", headers=headers, json={"operations": [operation(2, second_id)]}
            )
            self.assertEqual(entity_limited.status_code, 413, entity_limited.text)
            conflict = client.post(
                "/v1/sync/push", headers=headers, json={"operations": [operation(2, first_id)]}
            )
            self.assertEqual(conflict.status_code, 200, conflict.text)
            operation_limited = client.post(
                "/v1/sync/push", headers=headers, json={"operations": [operation(3, first_id)]}
            )
            self.assertEqual(operation_limited.status_code, 413, operation_limited.text)
            with app.state.session_factory() as session:
                old = now() - timedelta(days=91)
                for item in session.query(ProcessedOperation).all():
                    item.created_at = old
                for item in session.query(Change).all():
                    item.created_at = old
                session.commit()
            cleaned_operation = client.post(
                "/v1/sync/push", headers=headers, json={"operations": [operation(3, first_id)]}
            )
            self.assertEqual(cleaned_operation.status_code, 200, cleaned_operation.text)

            phone = client.post(
                "/v1/sessions",
                json={
                    "login_name": "quota-user",
                    "password": "correct horse battery staple",
                    "device_name": "phone",
                },
            )
            self.assertEqual(phone.status_code, 200, phone.text)
            device_limited = client.post(
                "/v1/sessions",
                json={
                    "login_name": "quota-user",
                    "password": "correct horse battery staple",
                    "device_name": "tablet",
                },
            )
            self.assertEqual(device_limited.status_code, 413, device_limited.text)
            with app.state.session_factory() as session:
                stale = session.get(Device, phone.json()["device_id"])
                stale.revoked = True
                stale.last_seen_at = now() - timedelta(days=91)
                session.commit()
            replacement = client.post(
                "/v1/sessions",
                json={
                    "login_name": "quota-user",
                    "password": "correct horse battery staple",
                    "device_name": "replacement",
                },
            )
            self.assertEqual(replacement.status_code, 200, replacement.text)

            claim_one = "first-pairing-token-with-at-least-thirty-two-characters"
            pairing = client.post(
                "/v1/pairings",
                headers=headers,
                json={"claim_token_hash": token_hash(claim_one), "envelope": {"ciphertext": "one"}},
            )
            self.assertEqual(pairing.status_code, 201, pairing.text)
            pairing_limited = client.post(
                "/v1/pairings",
                headers=headers,
                json={
                    "claim_token_hash": token_hash("second-pairing-token-with-at-least-thirty-two-characters"),
                    "envelope": {"ciphertext": "two"},
                },
            )
            self.assertEqual(pairing_limited.status_code, 413, pairing_limited.text)
            with app.state.session_factory() as session:
                stale_pairing = session.get(Pairing, pairing.json()["pairing_id"])
                stale_pairing.expires_at = now() - timedelta(minutes=1)
                session.commit()
            pairing_replacement = client.post(
                "/v1/pairings",
                headers=headers,
                json={
                    "claim_token_hash": token_hash("replacement-pairing-token-with-at-least-thirty-two-characters"),
                    "envelope": {"ciphertext": "replacement"},
                },
            )
            self.assertEqual(pairing_replacement.status_code, 201, pairing_replacement.text)

            first_blob = hashlib.sha256(b"quota-incomplete-one").hexdigest()
            second_blob = hashlib.sha256(b"quota-incomplete-two").hexdigest()
            created_blob = client.post(
                "/v1/blobs",
                headers=headers,
                json={"blob_id": first_blob, "byte_count": 0, "chunk_count": 1, "key_version": 1},
            )
            self.assertEqual(created_blob.status_code, 201, created_blob.text)
            blob_limited = client.post(
                "/v1/blobs",
                headers=headers,
                json={"blob_id": second_blob, "byte_count": 0, "chunk_count": 1, "key_version": 1},
            )
            self.assertEqual(blob_limited.status_code, 413, blob_limited.text)
            with app.state.session_factory() as session:
                stale_blob = session.get(Blob, (account["account_id"], first_blob))
                stale_blob.created_at = now() - timedelta(hours=2)
                session.commit()
            blob_replacement = client.post(
                "/v1/blobs",
                headers=headers,
                json={"blob_id": second_blob, "byte_count": 0, "chunk_count": 1, "key_version": 1},
            )
            self.assertEqual(blob_replacement.status_code, 201, blob_replacement.text)
            uploaded_empty = client.put(
                f"/v1/blobs/{second_blob}/chunks/0", headers=headers, content=b"x" * 28
            )
            self.assertEqual(uploaded_empty.status_code, 204, uploaded_empty.text)
            completed_empty = client.post(f"/v1/blobs/{second_blob}/complete", headers=headers)
            self.assertEqual(completed_empty.status_code, 200, completed_empty.text)
            third_blob = hashlib.sha256(b"quota-complete-zero-bypass").hexdigest()
            completed_blob_limited = client.post(
                "/v1/blobs",
                headers=headers,
                json={"blob_id": third_blob, "byte_count": 0, "chunk_count": 1, "key_version": 1},
            )
            self.assertEqual(completed_blob_limited.status_code, 413, completed_blob_limited.text)
        app.state.engine.dispose()

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
                {
                    key: capabilities[key]
                    for key in (
                        "max_batch",
                        "max_pull",
                        "max_entity_bytes",
                        "max_blob_bytes",
                        "max_pull_response_bytes",
                    )
                },
                {
                    "max_batch": 1,
                    "max_pull": 7,
                    "max_entity_bytes": 2048,
                    "max_blob_bytes": 4096,
                    "max_pull_response_bytes": 32 * 1024 * 1024,
                },
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

    def test_account_blob_byte_quota_is_enforced(self) -> None:
        root = Path(self.temp.name) / "byte-quota"
        root.mkdir()
        with patch.dict(
            os.environ,
            {"MEALCIRCUIT_SYNC_QUOTA_BYTES": "256"},
        ):
            app = create_app(
                f"sqlite:///{(root / 'quota.db').as_posix()}",
                root / "blobs",
                registration_mode="open",
                create_schema=True,
            )
        try:
            with TestClient(app) as client:
                account = client.post(
                    "/v1/accounts",
                    json={
                        "login_name": "byte-quota-user",
                        "password": "correct horse battery staple",
                        "device_name": "desktop",
                    },
                ).json()
                headers = {"Authorization": f"Bearer {account['access_token']}"}
                recovery = client.put(
                    "/v1/key-envelopes/recovery",
                    headers=headers,
                    json={
                        "envelope": {
                            "version": 1,
                            "key_version": 1,
                            "nonce": "quota",
                            "ciphertext": "quota",
                        }
                    },
                )
                self.assertEqual(recovery.status_code, 200, recovery.text)

                def create_blob(blob_id: str, byte_count: int) -> int:
                    return client.post(
                        "/v1/blobs",
                        headers=headers,
                        json={
                            "blob_id": blob_id,
                            "byte_count": byte_count,
                            "chunk_count": 1,
                            "key_version": 1,
                        },
                    ).status_code

                within_quota = hashlib.sha256(b"within-quota").hexdigest()
                self.assertEqual(create_blob(within_quota, 100), 201)
                over_quota = hashlib.sha256(b"over-quota").hexdigest()
                self.assertEqual(create_blob(over_quota, 200), 413)
                self.assertEqual(create_blob(over_quota, 200), 413)
        finally:
            app.state.engine.dispose()

    def test_pull_pages_are_bounded_by_advertised_response_bytes(self) -> None:
        root = Path(self.temp.name) / "bounded-pull"
        root.mkdir()
        with patch.dict(
            os.environ,
            {
                "MEALCIRCUIT_SYNC_MAX_PULL": "7",
                "MEALCIRCUIT_SYNC_MAX_ENTITY_BYTES": "2048",
                "MEALCIRCUIT_SYNC_MAX_PULL_RESPONSE_BYTES": "8192",
            },
        ):
            app = create_app(
                f"sqlite:///{(root / 'bounded.db').as_posix()}",
                root / "blobs",
                registration_mode="open",
                create_schema=True,
            )
        with TestClient(app) as client:
            capabilities = client.get("/v1/capabilities").json()
            self.assertEqual(capabilities["max_pull"], 1)
            self.assertEqual(capabilities["max_pull_response_bytes"], 8192)
            account = client.post(
                "/v1/accounts",
                json={
                    "login_name": "bounded-pull",
                    "password": "bounded pull secure password",
                    "device_name": "test",
                },
            ).json()
            headers = {"Authorization": f"Bearer {account['access_token']}"}
            recovery = client.put(
                "/v1/key-envelopes/recovery",
                headers=headers,
                json={
                    "envelope": {
                        "version": 1,
                        "key_version": 1,
                        "nonce": "bounded",
                        "ciphertext": "bounded",
                    }
                },
            )
            self.assertEqual(recovery.status_code, 200, recovery.text)
            operations = [
                {
                    "op_id": f"op_00000000-0000-4000-8000-{index:012d}",
                    "remote_id": hashlib.sha256(f"bounded-{index}".encode()).hexdigest(),
                    "base_server_version": 0,
                    "key_version": 1,
                    "envelope": {
                        "envelope_version": 1,
                        "key_version": 1,
                        "nonce": "AA==",
                        "ciphertext": "x" * 1500,
                    },
                }
                for index in range(3)
            ]
            pushed = client.post(
                "/v1/sync/push",
                headers=headers,
                json={"operations": operations},
            )
            self.assertEqual(pushed.status_code, 200, pushed.text)

            cursor = 0
            seen: list[str] = []
            for _ in range(3):
                page = client.get(
                    f"/v1/sync/pull?cursor={cursor}&limit=1",
                    headers=headers,
                )
                self.assertEqual(page.status_code, 200, page.text)
                self.assertLessEqual(len(page.content), 8192)
                body = page.json()
                self.assertEqual(len(body["changes"]), 1)
                seen.append(body["changes"][0]["remote_id"])
                cursor = body["cursor"]
            self.assertEqual(seen, [item["remote_id"] for item in operations])
            self.assertFalse(body["has_more"])
        app.state.engine.dispose()


if __name__ == "__main__":
    unittest.main()
