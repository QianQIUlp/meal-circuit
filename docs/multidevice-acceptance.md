# Multi-device acceptance matrix

This matrix is the release evidence for the local-first, user-configurable multi-device implementation. It maps the approved scope to source, automated tests and runtime artifacts; it does not treat unexecuted release-signing steps as complete.

| Requirement | Authoritative implementation | Verification evidence |
|---|---|---|
| Domain v1, full UUIDs, immutable revisions, UTC timestamps and legacy IDs | `protocol/domain-v1.schema.json`, `mealcircuit/domain.py`, Android `Domain.kt` | Shared contract fixtures; Python/Android contract tests |
| Explicit SQLite migration, pre-migration backup, rollback, metadata and managed assets | `mealcircuit/db_migrations.py`, `mealcircuit/domain_store.py` | Legacy-schema, failed migration, logical summary and asset tests |
| Versioned settings/profile/doctrine mirrors | `domain_store.py`, `configuration.py` | File-change capture and synced mirror refresh integration tests |
| Portable Data v1 encrypted by default, restore/merge preview, hashes and safe promotion | `mealcircuit/portable.py`, Android `PortableData.kt` | Cross-runtime MCX fixture, restore/merge round trips, interruption recovery, traversal/bomb/tamper tests |
| Transactional outbox, opaque IDs, CAS/idempotency, cursor/ack, snapshot and three-way merge | `mealcircuit/sync.py`, Android `SyncEngine.kt`, `docs/sync-protocol-v1.md` | Two-client offline/concurrency, response-loss, reorder, duplicate-op, conflict and resync tests |
| E2EE entities/assets, recovery, pairing, device revocation and safe key rotation | Python/Android crypto and account managers; server key-envelope/rotation APIs | Fixed cross-language vectors, negative AEAD tests, asset recovery, pairing/revocation and rotation tests |
| Opaque self-hosted service with PostgreSQL 18, Alembic, quotas and local blob boundary | `sync_server/`, `protocol/sync-v1.openapi.json`, `sync_server/compose.yaml` | FastAPI integration suite, Alembic fresh/legacy tests, PostgreSQL 18 CI job and server-plaintext canary scan |
| Desktop local-first UI/CLI, secure storage, optional sync and native packaging | existing Web/Agent workflows plus `secret_store.py`, sync CLI/Web and `packaging/` | Python/Web tests, Windows PyInstaller clean build and packaged smoke test; native CI jobs for all desktop targets |
| Longitudinal Agent draft and compact user-model projection | `agent_workspace.py`, `professional.py`, `agent_user_model` preference and Android optional result rendering | three-stage boundaries, clarification, stale-context, local revision, claim evidence and Portable round-trip tests |
| Native Android offline client and optional background sync | `android/` Compose/Room/WorkManager application | JVM tests, Room migrations, release lint/APK/AAB, emulator instrumentation and real Python↔Android E2EE test |
| User-selected AI providers remain device-local | Python standard-library provider layer and Android `AiClient.kt`/`SecretVault.kt` | Provider payload/validation tests; secret exclusions; DeepSeek photo rejection |
| Release and supply-chain readiness | `uv.lock`, Gradle lock, release workflow, SBOM/checksum/signing gates, license/privacy/threat docs | dependency audit, lock/checksum verifier, release-data scan and native runner workflows |

## Executed local acceptance

- `./test.ps1`: 112 tests pass; 26 optional-dependency tests skip by design.
- Full Python environment: 112 tests run, 111 pass; only the environment-gated PostgreSQL URL test skips locally.
- Android: 11 JVM tests; release lint, unsigned APK/AAB, debug instrumentation and 10 real-service cross-client instrumentation tests pass.
- Real cross-client run proves Python offline write → Android and Android offline write → fresh Python client, then scans the server database/WAL/backup, blob volume and server log for synthetic meal content, password, recovery string and API key with zero matches.
- Windows PyInstaller clean build and packaged `--smoke-test` pass.
- Domain/OpenAPI/lock/dependency/release-data/compile checks, Alembic fresh/legacy upgrade, YAML parsing, `pip-audit` and `git diff --check` pass.

## External release gates

PostgreSQL 18, macOS universal DMG, Linux AppImage and Windows installer builds run on their native GitHub Actions environments. Production Android signing, Play upload, macOS notarization and Windows Authenticode remain intentionally gated on the repository owner's platform accounts and CI secrets. These account-holder actions are not claimed as locally completed.
