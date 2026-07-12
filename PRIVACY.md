# Privacy

MealCircuit has no telemetry and does not require registration. Desktop data stays under `MEALCIRCUIT_HOME`; Android data stays in the application-private Room/files directories. Removing source code or uninstalling one desktop copy does not automatically remove external private data.

Optional synchronization sends opaque IDs, authenticated ciphertext, account/device identifiers, tokens and network metadata to the user-configured server. The server cannot read entity kinds, entity IDs, filenames, photos, meal records, profile or doctrine, but it can observe account/device existence, IP addresses, times, ciphertext sizes and traffic patterns.

Optional AI generation is configured independently on each device. The chosen provider receives the specific context/image submitted for that generation. Review that provider's retention and training policy before sending health information. API keys are not synchronized or exported.

Encrypted `.mcx` exports contain health data and photos and require their separate recovery string. Plain ZIP export is intentionally explicit and unprotected. Sync tokens, device keys, Account Data Keys, recovery strings and API keys are excluded from both formats.

Never attach real private data to an issue, pull request, crash report or test fixture. Use `doctor` to locate desktop storage, the in-app export/delete controls for user data portability, and [`docs/backup-restore.md`](docs/backup-restore.md) for a verified recovery process.
