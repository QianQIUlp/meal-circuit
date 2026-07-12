# Release process

The `release-builds` workflow builds each desktop platform natively because PyInstaller cannot cross-compile. It produces Windows x64 portable ZIP and Inno Setup installer, a verified macOS universal2 DMG, Linux x86_64 AppImage, Android release APK/AAB, SHA-256 checksums and a CycloneDX SBOM.

Required release checks are Python 3.11/3.13, PostgreSQL 18 integration, Android unit/build/lint, emulator instrumentation, Alembic upgrade, OpenAPI freshness, dependency audit, license inventory and release-data scan.

The emulator job includes a real Python ↔ Android E2EE round trip against the migrated reference service. Platform packaging is not inferred from a host-only build: Windows, macOS universal2 and Linux AppImage jobs each execute their packaged smoke test on the native runner.

Python resolution is frozen in `uv.lock`, Android's resolved graph is frozen in `android/app/gradle.lockfile`, and the Gradle distribution plus downloaded AppImage builder are SHA-256 verified. Changing any of these requires intentionally regenerating the lock/checksum and rerunning the supply-chain job.

Repository secrets are optional for local artifacts but required for official trust/publishing:

- Android keystore, alias and passwords
- Apple Developer signing certificate, identity, Apple ID app password and team ID
- Windows Authenticode PFX and password

Never upload secrets to a Portable Data archive or repository. A tag-triggered workflow publishes only after all platform jobs finish. Signing/notarization failures are release blockers; unsigned artifacts must be clearly labeled and must not replace stable signed downloads.

Local verification can prove the current host's package only. Before publishing, inspect the GitHub Actions run and require the PostgreSQL 18, Android emulator, all three desktop package jobs, signature/notarization steps and checksum/SBOM release job to be green.
