# Privacy

MealCircuit has no telemetry and does not require registration. Android data
stays in the application's private Room database and files directories.

Optional synchronization sends opaque IDs, authenticated ciphertext,
account/device identifiers, tokens and network metadata to the
user-configured server. The server cannot read entity kinds, entity IDs,
filenames, photos, meal records, profile or doctrine, but it can observe
account/device existence, IP addresses, times, ciphertext sizes and traffic
patterns.

The Android application does not call an external AI provider. If synchronized
data is later used for optional generation on another MealCircuit client, that
client sends only the context or image explicitly submitted to its configured
provider. Review that provider's retention and training policy before sending
health information. API keys are not synchronized or exported.

Encrypted .mcx exports contain health data and photos and require their
separate recovery string. A plain ZIP package explicitly created on another
client is unprotected. Sync tokens, device keys, Account Data Keys, recovery
strings and API keys are excluded from both formats.

Never attach real private data to an issue, pull request, crash report or test
fixture.
