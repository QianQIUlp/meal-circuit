# Third-party dependency inventory

This is the human-readable direct-dependency inventory for MealCircuit 0.3.0. The release workflow also emits a CycloneDX SBOM containing resolved transitive packages and versions. The upstream license text distributed with each binary remains authoritative.

| Component | Use | Upstream license |
|---|---|---|
| cryptography | Desktop E2EE | Apache-2.0 OR BSD-3-Clause |
| keyring | Desktop operating-system credential store | MIT |
| pywebview | Desktop native window | BSD-3-Clause |
| pyinstaller | Desktop packaging | GPL-2.0-or-later with PyInstaller bootloader exception |
| fastapi | Sync HTTP service | MIT |
| pydantic | Sync request validation | MIT |
| sqlalchemy | PostgreSQL persistence | MIT |
| alembic | Server schema migrations | MIT |
| psycopg / psycopg-binary | PostgreSQL driver | LGPL-3.0-only |
| uvicorn | ASGI server | BSD-3-Clause |
| argon2-cffi | Password hashing | MIT |
| httpx | Server integration tests | BSD-3-Clause |
| Kotlin and kotlinx.serialization/coroutines | Android implementation | Apache-2.0 |
| AndroidX Core, Activity, Lifecycle, Navigation, Compose, Room and WorkManager | Android application framework | Apache-2.0 |
| OkHttp | Android HTTPS client | Apache-2.0 |
| ZXing Android Embedded | Android QR scanning | Apache-2.0 |
| tzdata | IANA timezone database on Windows | Apache-2.0 |
| setuptools | Python build backend | MIT |
| uv | Cross-platform Python dependency lock/install tooling | Apache-2.0 OR MIT |
| jsonschema | Protocol contract validation in development/CI | MIT |
| pip-audit | Python dependency vulnerability audit in development/CI | Apache-2.0 |
| Lucide icons | Desktop Web UI icons | ISC; bundled notice is in `mealcircuit/static/icons/LUCIDE_LICENSE.txt` |

No third-party dependency changes MealCircuit's MIT project license. LGPL components are dynamically used as unmodified libraries; PyInstaller's exception permits distribution of generated executables under the application license.
