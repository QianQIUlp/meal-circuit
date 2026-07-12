# Portable Data v1

Portable Data is the backup, migration and client-interchange format. It exports domain revisions rather than internal SQLite or Room tables.

Desktop commands:

```text
python -m mealcircuit.agent_cli export-data --output backup.mcx
python -m mealcircuit.agent_cli import-data backup.mcx --preview --mode restore
python -m mealcircuit.agent_cli import-data backup.mcx --mode merge --apply
```

The Android Settings screen exposes the same format through the system document picker.

## Contents and encryption

The logical archive contains a versioned manifest, JSONL revision graph, entity heads, versioned configuration and managed assets. Every file has a SHA-256 entry. It excludes API keys, Account Data Keys, recovery keys, access/refresh tokens and device wrapping keys.

The default `.mcx` container uses a random 256-bit export key, AES-256-GCM authenticated chunks and a checksummed `MC1-…` recovery string. Encryption covers archive metadata and payload. A plaintext ZIP requires both `--plain --i-understand-plaintext-risk` and should be treated as exposed health data.

## Import modes

- `restore` requires a fresh empty `MEALCIRCUIT_HOME`. Desktop validates the decrypted archive first, copies the current profile into a sibling staging directory on the same volume, performs every database/config/asset write and logical round-trip there, then promotes the complete directory with a journaled rename. A killed process either restores the previous directory or finalizes an already durable promotion before the next default-database open. Android performs the same logical validation inside its Room transaction and removes unreferenced partial asset files at startup.
- `merge` previews first and uses the same parent-aware merge rules as online sync. Conflicting siblings remain available in the conflict center.

Import rejects unsupported format/schema versions, duplicate IDs, missing references, parent cycles, invalid heads, hash mismatch, authenticated-encryption failure, path traversal, absolute archive paths, unsafe compression ratios, oversized entries and partial writes. Handled failure discards staging immediately; process interruption is recovered from the persistent sibling transaction journal before incomplete data becomes visible.

Backups are useful only if the recovery string is stored separately and restore is rehearsed. See [`backup-restore.md`](backup-restore.md).
