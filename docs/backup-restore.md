# Backup and restore drill

## Local client backup

1. Export an encrypted `.mcx` from desktop or Android.
2. Record the displayed recovery string in a separate password manager/offline location.
3. Copy the `.mcx` to the backup target; never put a live SQLite/WAL directory in bidirectional file sync.
4. On a clean test profile, run import preview and then `restore` apply.
5. Export the restored profile again and compare the normalized domain summary and asset SHA-256 values. Automated round-trip tests perform the same invariant.

Desktop example:

```powershell
python -m mealcircuit.agent_cli export-data --output mealcircuit-backup.mcx
$env:MEALCIRCUIT_HOME = "$env:TEMP\mealcircuit-restore-drill"
python -m mealcircuit.agent_cli init
python -m mealcircuit.agent_cli import-data mealcircuit-backup.mcx --preview --mode restore
python -m mealcircuit.agent_cli import-data mealcircuit-backup.mcx --mode restore --apply
```

## Self-hosted sync backup

Back up PostgreSQL and the blob volume from the same consistency window. Both contain ciphertext, but both are required. Preserve server environment/config and Caddy certificates separately. Test restore on an isolated hostname, run Alembic to the recorded application version, and connect a disposable client with a recovery string.

The sync server is not the only backup: a malicious deletion or operator error can synchronize deletion. Keep periodic immutable `.mcx` exports.

## Failure expectations

- A missing/wrong `.mcx` recovery string must fail before current data changes.
- A missing account recovery string with no authorized device means remote data is unrecoverable by design.
- A restored sync log may force full snapshot resync; this is expected and must not roll back local writes.
- Before upgrading, preserve the pre-migration SQLite snapshot and server database/blob backup until logical counts, hashes and real client smoke tests pass.
- Python import uses sibling staging/rollback directories beside `MEALCIRCUIT_HOME`; they can temporarily contain plaintext copies of the local profile. Keep the parent directory on a trusted, access-controlled disk and let the next startup complete automatic recovery before manually deleting anything.
