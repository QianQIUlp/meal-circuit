# MealCircuit Android

The Android client is a native Kotlin application using Jetpack Compose, Room, WorkManager, OkHttp and kotlinx.serialization (`minSdk 26`, `targetSdk 36`). It does not embed Python and does not need a server for local use.

```bash
cd android
./gradlew testDebugUnitTest assembleDebug
./gradlew connectedDebugAndroidTest   # emulator/device
```

Room is the only UI read source. Every local write is immediately committed with an optional outbox operation; WorkManager retries encrypted sync with exponential backoff. The system Photo Picker and `TakePicture` contract handle images. Secrets are wrapped by Android Keystore and excluded from backup/transfer.

Structured revisions always synchronize. Media policy can be `all`, `all_wifi` or `on_demand`; Android checks `ConnectivityManager.isActiveNetworkMetered` before automatic photo transfer, and the on-demand action is explicit. Authentication, incompatible protocol/key versions, failed recovery and user conflicts stop retry loops instead of spinning indefinitely.

The application implements records, daily advice, five status modules with drafts/publication, photo and ingredient tasks, food library, memories, adjustments, profile/settings/doctrine, per-device AI providers, Portable Data, synchronization, media policy, device management, QR pairing, conflicts and safe account-key rotation.

Release signing reads `MEALCIRCUIT_KEYSTORE_PATH`, `MEALCIRCUIT_KEYSTORE_PASSWORD`, `MEALCIRCUIT_KEY_ALIAS` and `MEALCIRCUIT_KEY_PASSWORD`. Unsigned local release builds work without them; official APK/AAB publishing requires the account holder's secrets.

CI also runs a real cross-client acceptance path: Python creates and uploads an offline task, Android restores it and uploads its own offline record, then a fresh Python profile restores that Android record through the same E2EE service.
