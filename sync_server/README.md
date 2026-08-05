# MealCircuit Sync self-hosting

MealCircuit Sync is an opaque E2EE relay. It does not run MealCircuit business logic, decrypt user data, call AI providers or recover a lost Account Data Key.

## Quick start

```bash
cd sync_server
export POSTGRES_PASSWORD='replace-with-a-long-url-safe-random-password'
docker compose up -d db sync
curl http://127.0.0.1:8080/healthz
```

For remote clients, configure `SYNC_HOSTNAME` and `CADDY_EMAIL`, then run `docker compose --profile https up -d`. Clients reject plain HTTP except localhost debug builds. Keep the sync port loopback-only when Caddy is used.

`REGISTRATION_MODE` defaults to `first-user`: after the first account, public registration closes. `open` and `closed` are explicit alternatives.

The Compose volume is mounted at `/var/lib/postgresql`, the version-aware volume root required by the official PostgreSQL 18 image. Do not change it back to the pre-18 `/var/lib/postgresql/data` path without an explicit migration plan.

## Configuration

| Variable | Default |
|---|---:|
| `MEALCIRCUIT_SYNC_DATABASE_URL` | Compose PostgreSQL URL |
| `MEALCIRCUIT_SYNC_BLOB_ROOT` | `/var/lib/mealcircuit-sync/blobs` |
| `MEALCIRCUIT_SYNC_REGISTRATION_MODE` | `first-user` |
| `MEALCIRCUIT_SYNC_MAX_BATCH` | 100 operations |
| `MEALCIRCUIT_SYNC_MAX_PULL` | 500 changes |
| `MEALCIRCUIT_SYNC_MAX_ENTITY_BYTES` | 1 MiB ciphertext |
| `MEALCIRCUIT_SYNC_MAX_PULL_RESPONSE_BYTES` | 32 MiB JSON response budget |
| `MEALCIRCUIT_SYNC_MAX_REQUEST_BYTES` | 32 MiB sync-push request budget |
| `MEALCIRCUIT_SYNC_MAX_BLOB_BYTES` | 10 MiB plaintext metadata limit |
| `MEALCIRCUIT_SYNC_QUOTA_BYTES` | 10 GiB per account |
| `MEALCIRCUIT_SYNC_MAX_ENTITIES` | 100,000 opaque entities and 100,000 blobs per account/key epoch |
| `MEALCIRCUIT_SYNC_MAX_OPERATIONS` | 500,000 retained idempotency records per account |
| `MEALCIRCUIT_SYNC_MAX_DEVICES` | 32 per account |
| `MEALCIRCUIT_SYNC_MAX_PAIRINGS` | 32 active pairings per account |
| `MEALCIRCUIT_SYNC_MAX_INCOMPLETE_BLOBS` | 128 per account |
| `MEALCIRCUIT_SYNC_INCOMPLETE_BLOB_HOURS` | 24 hours before incomplete-blob cleanup |
| `MEALCIRCUIT_SYNC_AUTH_RATE_LIMIT` | 20 login/registration attempts per source and window |
| `MEALCIRCUIT_SYNC_AUTH_RATE_WINDOW_SECONDS` | 60 seconds |
| `MEALCIRCUIT_SYNC_ARGON2_CONCURRENCY` | 2 concurrent password hash/verify jobs per process |
| `MEALCIRCUIT_SYNC_ROTATION_LEASE_MINUTES` | 60-minute renewable key-rotation lease |

The request and pull-response byte budgets can lower the effective batch or page count; `/v1/capabilities` reports those effective limits. Authentication rate limiting is per service process and source address, so multi-worker or multi-instance deployments should also enforce a shared limit at the trusted reverse proxy.

Key rotation ownership is a renewable lease rather than a permanent account lock. Target-key pushes and blob uploads, plus owner status checks, renew it. After the configured idle period, the next rotation-sensitive request removes the abandoned target epoch and allows another authenticated device to start a fresh rotation; an unexpired non-owner still receives `409`.

Run `alembic -c sync_server/alembic.ini upgrade head` before the service; the container does this automatically. The API contract is `protocol/sync-v1.openapi.json` and `/v1/capabilities` reports effective limits.

## Administration and logs

`mealcircuit-sync-admin` can create/disable accounts, reset authentication and report opaque usage. It cannot view or restore content. The application does not log request bodies, Authorization values, recovery envelopes or encrypted entity/blob bodies. Put proxy access logs under the same privacy policy because IP/timing/size metadata remains sensitive.

Back up PostgreSQL and the blob volume together. See `docs/backup-restore.md` and `docs/threat-model.md`.

The HTTP layer depends on the small `BlobStorage` protocol in `blob_storage.py`; the shipped `LocalBlobStorage` performs atomic encrypted-chunk writes on the mounted volume. This is the extension boundary for a future S3-compatible adapter. Version 1 intentionally ships and supports only the local-volume implementation.
