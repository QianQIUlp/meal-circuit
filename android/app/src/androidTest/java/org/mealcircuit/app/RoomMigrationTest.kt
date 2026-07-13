package org.mealcircuit.app

import android.content.Context
import androidx.room.Room
import androidx.room.testing.MigrationTestHelper
import androidx.work.BackoffPolicy
import androidx.work.NetworkType
import androidx.sqlite.db.framework.FrameworkSQLiteOpenHelperFactory
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.MealCircuitDatabase
import org.mealcircuit.app.data.SyncConfigurationEntity
import org.mealcircuit.app.data.UnknownEntity
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.portable.ImportMode
import org.mealcircuit.app.portable.PortableData
import org.mealcircuit.app.sync.SecretVault
import org.mealcircuit.app.sync.SyncWorker
import org.mealcircuit.app.sync.SyncAccountManager
import org.mealcircuit.app.sync.SyncApi
import org.mealcircuit.app.sync.SyncEngine
import org.mealcircuit.app.sync.AccountCipher
import java.security.KeyStore
import java.time.Instant

@RunWith(AndroidJUnit4::class)
class RoomMigrationTest {
    private val databaseName = "migration-test"
    private val restartDatabaseName = "process-restart-test"

    @get:Rule
    val helper = MigrationTestHelper(
        InstrumentationRegistry.getInstrumentation(),
        MealCircuitDatabase::class.java,
        emptyList(),
        FrameworkSQLiteOpenHelperFactory(),
    )

    @After
    fun clean() {
        ApplicationProvider.getApplicationContext<Context>().deleteDatabase(databaseName)
        ApplicationProvider.getApplicationContext<Context>().deleteDatabase(restartDatabaseName)
    }

    @Test
    fun migration1To2PreservesDomainTablesAndAddsVersionMetadata() {
        helper.createDatabase(databaseName, 1).apply {
            execSQL(
                "INSERT INTO materialized_records(entityId,entityKind,payloadJson,deleted,sortKey,updatedAt) " +
                    "VALUES('record_fixture','daily_record','{}',0,'2026-07-10','2026-07-10T00:00:00Z')"
            )
            close()
        }
        helper.runMigrationsAndValidate(databaseName, 2, true, MealCircuitDatabase.MIGRATION_1_2).use { db ->
            db.query("SELECT value FROM app_metadata WHERE `key`='schema_version'").use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals("2", cursor.getString(0))
            }
            db.query("SELECT COUNT(*) FROM materialized_records WHERE entityId='record_fixture'").use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals(1, cursor.getInt(0))
            }
        }
    }

    @Test
    fun localWriteAndOutboxAreCommittedTogether() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            repository.putSyncConfiguration(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://sync.invalid",
                    accountId = "account_test",
                    updatedAt = Instant.now().toString(),
                )
            )
            val recordId = DomainRevision.id("record")
            val revision = repository.save(
                EntityKind.DAILY_RECORD,
                buildJsonObject {
                    put("id", recordId); put("record_date", "2026-07-10")
                    put("raw_input", "offline"); put("created_at", Instant.now().toString())
                },
                recordId,
            )
            assertEquals(revision.revisionId, repository.pending().single().revisionId)
            assertEquals("offline", repository.record(revision.entityId)?.payloadJson?.let {
                repository.json.parseToJsonElement(it).jsonObject.getValue("raw_input").jsonPrimitive.content
            })
        } finally {
            database.close()
        }
    }

    @Test
    fun unknownSchemaEnvelopeIsRetainedWithoutMaterialization() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            repository.putUnknown(
                UnknownEntity(
                    remoteId = "a".repeat(64),
                    serverVersion = 7,
                    keyVersion = 2,
                    encryptedEnvelope = "{\"ciphertext\":\"opaque-future-schema\"}",
                    updatedAt = Instant.now().toString(),
                )
            )
            assertEquals(1, database.dao().unknownCount())
            assertEquals(null, repository.record("record_future"))
        } finally {
            database.close()
        }
    }

    @Test
    fun processRestartPreservesLocalRecordAndPendingOutbox() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        var database = Room.databaseBuilder(context, MealCircuitDatabase::class.java, restartDatabaseName).build()
        val recordId = DomainRevision.id("record")
        try {
            var repository = DomainRepository(database, "device_restart")
            repository.putSyncConfiguration(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://sync.invalid",
                    accountId = "account_test",
                    updatedAt = Instant.now().toString(),
                )
            )
            val revision = repository.save(
                EntityKind.DAILY_RECORD,
                buildJsonObject {
                    put("id", recordId); put("record_date", "2026-07-10")
                    put("raw_input", "survives restart"); put("created_at", Instant.now().toString())
                },
                recordId,
            )
            database.close()

            database = Room.databaseBuilder(context, MealCircuitDatabase::class.java, restartDatabaseName).build()
            repository = DomainRepository(database, "device_restart")
            assertEquals(revision.revisionId, repository.pending().single().revisionId)
            assertEquals("survives restart", repository.record(recordId)?.payloadJson?.let {
                repository.json.parseToJsonElement(it).jsonObject.getValue("raw_input").jsonPrimitive.content
            })
        } finally {
            database.close()
        }
    }

    @Test
    fun pythonGeneratedEncryptedPortableFixtureImportsOnAndroid() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_android_fixture")
            val metadata = context.assets.open("fixtures/portable-v1-meta.json").bufferedReader().use {
                repository.json.parseToJsonElement(it.readText()).jsonObject
            }
            val preview = context.assets.open("fixtures/portable-v1.mcx").use { input ->
                PortableData(context, repository).import(
                    input,
                    metadata.getValue("recovery_key").jsonPrimitive.content,
                    ImportMode.RESTORE,
                )
            }
            assertEquals(1, preview.entities)
            val entityId = metadata.getValue("entity_id").jsonPrimitive.content
            val payload = repository.record(entityId)?.payloadJson?.let {
                repository.json.parseToJsonElement(it).jsonObject
            }
            assertEquals(
                "合成测试燕麦",
                payload?.getValue("food")?.jsonObject?.getValue("name")?.jsonPrimitive?.content,
            )
        } finally {
            database.close()
        }
    }

    @Test
    fun keystoreWrappedSecretCanBeDeletedWithoutRoomPersistence() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val vault = SecretVault(context)
        val secret = ByteArray(32) { it.toByte() }
        vault.put("instrumentation.secret", secret)
        assertTrue(secret.contentEquals(vault.get("instrumentation.secret")))
        vault.delete("instrumentation.secret")
        assertEquals(null, vault.get("instrumentation.secret"))
    }

    @Test
    fun keystoreWrappedSecretIsBoundToItsLogicalName() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val vault = SecretVault(context)
        val source = "instrumentation.bound.source"
        val target = "instrumentation.bound.target"
        vault.put(source, ByteArray(32) { 42 })
        val preferences = context.getSharedPreferences("wrapped_secrets", Context.MODE_PRIVATE)
        val encoded = requireNotNull(preferences.getString(source, null))
        assertTrue(preferences.edit().putString(target, encoded).commit())
        assertEquals(null, vault.get(target))
        vault.delete(source)
        vault.delete(target)
    }

    @Test
    fun keystoreLossMakesWrappedSecretsUnavailableInsteadOfReturningGarbage() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val vault = SecretVault(context)
        val name = "instrumentation.keystore.loss"
        vault.put(name, ByteArray(32) { 7 })
        KeyStore.getInstance("AndroidKeyStore").apply {
            load(null)
            deleteEntry("MealCircuit.wrap.v1")
        }
        assertEquals(null, vault.get(name))
        vault.delete(name)
    }

    @Test
    fun syncWorkWaitsForNetworkAndUsesExponentialBackoff() {
        val request = SyncWorker.buildRequest()
        assertEquals(NetworkType.CONNECTED, request.workSpec.constraints.requiredNetworkType)
        assertEquals(BackoffPolicy.EXPONENTIAL, request.workSpec.backoffPolicy)
        assertEquals(30_000L, request.workSpec.backoffDelayDuration)
    }

    @Test
    fun pythonDesktopAndAndroidExchangeOfflineRevisionsThroughRealServer() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        val serverUrl = arguments.getString("syncServerUrl")
        assumeTrue("cross-client server not configured", !serverUrl.isNullOrBlank())
        val login = requireNotNull(arguments.getString("syncLogin"))
        val password = requireNotNull(arguments.getString("syncPassword"))
        val recovery = requireNotNull(arguments.getString("syncRecovery"))
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        val vault = SecretVault(context)
        try {
            val repository = DomainRepository(database, "device_android_cross_client")
            val accounts = SyncAccountManager(repository, vault)
            accounts.login(serverUrl!!, login, password, "android-emulator", recovery)
            val configuration = requireNotNull(repository.syncConfiguration())
            val key = requireNotNull(vault.get("sync.account_data_key"))
            val engine = SyncEngine(
                repository,
                SyncApi(requireNotNull(configuration.serverUrl), vault),
                AccountCipher(requireNotNull(configuration.accountId), key, configuration.keyVersion),
                context,
            )
            val pulled = engine.run()
            assertTrue(pulled.applied > 0)
            assertTrue(repository.records(EntityKind.TASK_INPUT).any { record ->
                repository.json.parseToJsonElement(record.payloadJson).jsonObject
                    .getValue("original_input").jsonPrimitive.content == "python-offline-canary"
            })

            val recordId = DomainRevision.id("record")
            repository.save(
                EntityKind.DAILY_RECORD,
                buildJsonObject {
                    put("id", recordId)
                    put("record_date", "2026-07-12")
                    put("raw_input", "android-offline-canary")
                    put("created_at", Instant.now().toString())
                },
                recordId,
            )
            val pushed = engine.run()
            assertTrue(pushed.accepted > 0)
            accounts.unlink()
        } finally {
            database.close()
        }
    }
}
