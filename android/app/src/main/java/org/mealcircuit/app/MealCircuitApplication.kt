package org.mealcircuit.app

import android.app.Application
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.MealCircuitDatabase
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.sync.AccountCipher
import org.mealcircuit.app.sync.SecretVault
import org.mealcircuit.app.sync.SyncApi
import org.mealcircuit.app.sync.SyncEngine
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking

class MealCircuitApplication : Application() {
    lateinit var repository: DomainRepository
        private set
    lateinit var vault: SecretVault
        private set

    override fun onCreate() {
        super.onCreate()
        val preferences = getSharedPreferences("installation", MODE_PRIVATE)
        fun installationId(key: String, prefix: String): String = preferences.getString(key, null)
            ?: DomainRevision.id(prefix).also {
                check(preferences.edit().putString(key, it).commit()) {
                    "Unable to persist installation identity"
                }
            }
        val deviceId = preferences.getString("device_id", null)
            ?: DomainRevision.id("device").also {
                check(preferences.edit().putString("device_id", it).commit()) {
                    "Unable to persist installation device ID"
                }
            }
        repository = DomainRepository(MealCircuitDatabase.open(this), deviceId)
        runBlocking(Dispatchers.IO) {
            repository.ensureMetadata(installationId("instance_id", "instance"))
            repository.cleanupOrphanedAssetFiles(filesDir)
        }
        vault = SecretVault(this)
    }

    suspend fun syncEngineOrNull(): SyncEngine? {
        val configuration = repository.syncConfiguration()?.takeIf { it.enabled } ?: return null
        val accountId = configuration.accountId ?: return null
        val serverUrl = configuration.serverUrl ?: return null
        val key = vault.get("sync.account_data_key") ?: return null
        return SyncEngine(
            repository,
            SyncApi(serverUrl, vault),
            AccountCipher(accountId, key, configuration.keyVersion),
            this,
        )
    }
}
