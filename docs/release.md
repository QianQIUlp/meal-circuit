# Release process

The `release-builds` workflow maintains two targets: Windows x64 and Android. Its exact public manifest has six assets: `MealCircuit-<version>-windows-x64-portable.zip`, `MealCircuit-<version>-windows-x64-setup.exe`, `app-release.apk`, `app-release.aab`, `SHA256SUMS.txt`, and the lock-derived `MealCircuit-v<version>.cdx.json`. The Python Web UI and CLI are the source implementation of the Windows desktop client; source or wheel availability does not declare another supported operating-system target.

Required release checks are Python 3.11/3.13, PostgreSQL 18 integration, Android unit/build/lint, emulator instrumentation, Alembic upgrade, OpenAPI freshness, dependency audit, installable wheel/sdist verification, license inventory and release-data scan. A release tag starts the same full test workflow, and the publishing job waits for that exact commit's successful run instead of treating platform builds as a substitute.

The emulator job includes a real Python ↔ Android E2EE round trip against the migrated reference service. Platform packaging is not inferred from source tests: the Windows job executes the frozen application's packaged smoke test, and Android runs JVM, release build/lint and emulator instrumentation checks.

Python resolution is frozen in `uv.lock`, Android's resolved graph is frozen in `android/app/gradle.lockfile`, and the Gradle distribution is SHA-256 verified. Changing any of these requires intentionally regenerating the lock/checksum and rerunning the supply-chain job.

Repository secrets are not needed for local or pull-request artifacts, but a release tag fails closed unless all of these publishing credentials are configured:

- Android keystore, alias and passwords
- Windows Authenticode PFX and password

Never upload secrets to a Portable Data archive or repository. A tag-triggered workflow publishes only after the same-tag quality matrix and both supported platform jobs finish. Android signing and Windows Authenticode signing failures are release blockers.

Every Windows desktop bundle and Android package includes the MealCircuit license, privacy/security/disclaimer documents and applicable third-party license or notice text. Packaging verification fails if these files are missing. The release SBOM is generated from the canonical Python and Android lock files, must contain dependency relationships, and must not expose runner paths.

Local verification can prove only what ran on the current host. Before creating a release tag, confirm that private vulnerability reporting and both required signing credentials are configured. The tagged workflow publishes only after the same-tag Python/PostgreSQL/Android quality matrix, Windows and Android package jobs, signature checks, and checksum/SBOM generation succeed. Afterward, inspect both same-tag runs and the exact six-asset public manifest before recommending the release; a failed or cancelled run must never be advertised as usable.

The product site deploys automatically from `main`, and its download links name a concrete release. Avoid a public 404 window: create and verify that tagged GitHub release before merging the matching site-version links into the production branch (or temporarily keep the production site on the previous supported version). Only advertise the new version after both the release assets and the production-site links are reachable.
