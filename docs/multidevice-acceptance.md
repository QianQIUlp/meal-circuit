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
| Longitudinal Agent draft and compact user-model projection | `agent_workspace.py`, `agent_intelligence.py`, `professional.py`, goal/user/meal projections and Android result rendering | mandatory stage receipts, clarification, stale-context, local revision, deterministic intent coverage, claim evidence and Portable round-trip tests |
| Native Android offline client and optional background sync | `android/` Compose/Room/WorkManager application | JVM tests, Room migrations, release lint/APK/AAB, emulator instrumentation and real Python↔Android E2EE test |
| User-selected AI providers remain device-local | Python standard-library provider layer and device-local secret storage | Provider payload/validation tests; secret exclusions; DeepSeek photo rejection; Android daily generation is not a publication path |
| Release and supply-chain readiness | `uv.lock`, Gradle lock, release workflow, SBOM/checksum/signing gates, license/privacy/threat docs | dependency audit, lock/checksum verifier, release-data scan and native runner workflows |

## Verification boundary

This matrix defines the required multi-device coverage; it is not a claim about the latest branch run. Exact test counts and platform results become stale as the product changes, so current verification must come from the latest GitHub Actions run and the newest entry at the top of `DEVELOPMENT.md`. Historical counts must not be used to infer that a current change has passed.

## External release gates

PostgreSQL 18, macOS universal DMG, Linux AppImage and Windows installer builds run on their native GitHub Actions environments. Production Android signing and Play upload remain gated on the repository owner's platform account and CI secrets; when those Android secrets are absent, a tagged release omits Android assets rather than publishing unsigned ones. macOS notarization and Windows Authenticode remain optional trust enhancements gated on their respective platform credentials. These account-holder actions are not claimed as locally completed.
