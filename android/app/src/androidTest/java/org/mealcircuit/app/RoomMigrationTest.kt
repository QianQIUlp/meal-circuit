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
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonArray
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
import org.mealcircuit.app.data.EntityHeadEntity
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.data.MealCircuitDatabase
import org.mealcircuit.app.data.SyncConflictEntity
import org.mealcircuit.app.data.SyncConfigurationEntity
import org.mealcircuit.app.data.SyncShadowEntity
import org.mealcircuit.app.data.UnknownEntity
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.portable.ImportMode
import org.mealcircuit.app.portable.PortableData
import org.mealcircuit.app.sync.SecretVault
import org.mealcircuit.app.sync.KeyRotationManager
import org.mealcircuit.app.sync.SyncWorker
import org.mealcircuit.app.sync.SyncAccountManager
import org.mealcircuit.app.sync.SyncApi
import org.mealcircuit.app.sync.SyncEngine
import org.mealcircuit.app.sync.AccountCipher
import org.mealcircuit.app.sync.PendingRegistration
import org.mealcircuit.app.sync.RecoveryMaterial
import java.io.File
import java.security.KeyStore
import java.security.MessageDigest
import java.time.Instant
import kotlinx.serialization.encodeToString

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
    fun migration2To3AddsUnknownReprocessAttemptsWithDefaultZero() {
        helper.createDatabase(databaseName, 2).apply {
            execSQL(
                "INSERT INTO sync_unknown_entities(remoteId,serverVersion,keyVersion,encryptedEnvelope,updatedAt) " +
                    "VALUES('a','1','1','{}','2026-08-01T00:00:00Z')"
            )
            close()
        }
        helper.runMigrationsAndValidate(databaseName, 3, true, MealCircuitDatabase.MIGRATION_2_3).use { db ->
            db.query("SELECT reprocessAttempts FROM sync_unknown_entities WHERE remoteId='a'").use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals(0, cursor.getInt(0))
            }
        }
    }

    @Test
    fun crossKindConflictKeepLocalKeepsLocalKindAndRequeuesRevision() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_kind_test")
            repository.putSyncConfiguration(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://sync.invalid",
                    accountId = "account_test",
                    updatedAt = Instant.now().toString(),
                )
            )
            val entityId = DomainRevision.id("record")
            val timestamp = Instant.now().toString()
            val local = repository.save(
                EntityKind.DAILY_RECORD,
                buildJsonObject {
                    put("id", entityId); put("record_date", "2026-07-10")
                    put("raw_input", "local"); put("created_at", timestamp)
                },
                entityId,
            )
            val remote = DomainRevision.create(
                kind = EntityKind.FOOD_ITEM,
                entityId = entityId,
                deviceId = "remote_device",
                payload = buildJsonObject {
                    put("food", buildJsonObject {
                        put("id", entityId); put("name", "remote"); put("basis", "100g")
                        put("created_at", timestamp); put("updated_at", timestamp)
                    })
                    put("history", buildJsonArray {})
                },
            )
            val conflictId = DomainRevision.id("conflict")
            repository.commitSyncConflict(
                SyncConflictEntity(
                    id = conflictId, entityId = entityId, entityKind = "food_item",
                    baseRevisionJson = "",
                    localRevisionJson = repository.json.encodeToString(local),
                    remoteRevisionJson = repository.json.encodeToString(remote),
                    conflictingPathsJson = "[\"entity_kind\"]", status = "unresolved",
                    createdAt = timestamp, resolvedAt = null,
                ),
                entityId,
            )
            assertTrue(repository.heads().single().conflicted)
            repository.commitKindConflictResolutionKeepingLocal(conflictId, local)
            val head = repository.heads().single()
            assertTrue(!head.conflicted)
            assertEquals("daily_record", head.entityKind)
            assertEquals(local.revisionId, head.revisionId)
            assertEquals("resolved", repository.conflict(conflictId)?.status)
            assertTrue(repository.pending().any { it.revisionId == local.revisionId })
        } finally {
            database.close()
        }
    }

    @Test
    fun assetConflictResolutionCommitsFileAndRowAtomicallyAndMissingFileStaysUnresolved() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_asset_test")
            repository.putSyncConfiguration(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://sync.invalid",
                    accountId = "account_asset_test",
                    updatedAt = Instant.now().toString(),
                )
            )
            val filesDir = context.filesDir
            val bytes = "asset conflict fixture".toByteArray()
            val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
                .joinToString("") { "%02x".format(it) }
            val entityId = "asset_$digest"
            val timestamp = Instant.now().toString()
            val assetPayload = buildJsonObject {
                put("sha256", digest); put("media_type", "image/jpeg")
                put("extension", ".jpg"); put("byte_count", bytes.size)
            }
            val local = repository.save(EntityKind.ASSET, assetPayload, entityId)
            val target = filesDir.resolve("assets/$digest.jpg")
            target.parentFile?.mkdirs()
            target.writeBytes(bytes)
            val validAsset = ManagedAssetEntity(
                id = entityId, sha256 = digest, mediaType = "image/jpeg", extension = ".jpg",
                byteCount = bytes.size.toLong(), relativePath = "assets/$digest.jpg",
                unresolved = false, createdAt = timestamp,
            )
            repository.putAsset(validAsset)
            val remote = DomainRevision.create(
                kind = EntityKind.ASSET, entityId = entityId, deviceId = "remote_device",
                payload = assetPayload,
            )
            val conflictId = DomainRevision.id("conflict")
            repository.commitSyncConflict(
                SyncConflictEntity(
                    id = conflictId, entityId = entityId, entityKind = "asset",
                    baseRevisionJson = "",
                    localRevisionJson = repository.json.encodeToString(local),
                    remoteRevisionJson = repository.json.encodeToString(remote),
                    conflictingPathsJson = "[\"payload\"]", status = "unresolved",
                    createdAt = timestamp, resolvedAt = null,
                ),
                entityId,
            )
            repository.commitConflictResolution(
                conflictId,
                local,
                managedAsset = validAsset,
            )
            val committed = requireNotNull(repository.asset(entityId))
            assertEquals("assets/$digest.jpg", committed.relativePath)
            assertTrue(!committed.unresolved)
            assertEquals("resolved", repository.conflict(conflictId)?.status)
            assertTrue(repository.pending().any { it.revisionId == local.revisionId })

            val missingEntityId = DomainRevision.id("asset")
            val missingPayload = buildJsonObject {
                put("sha256", "0".repeat(64)); put("media_type", "image/png")
                put("extension", ".png"); put("byte_count", 1)
            }
            val missingLocal = repository.save(EntityKind.ASSET, missingPayload, missingEntityId)
            val missingRemote = DomainRevision.create(
                kind = EntityKind.ASSET, entityId = missingEntityId, deviceId = "remote_device",
                payload = missingPayload,
            )
            val missingConflict = DomainRevision.id("conflict")
            repository.commitSyncConflict(
                SyncConflictEntity(
                    id = missingConflict, entityId = missingEntityId, entityKind = "asset",
                    baseRevisionJson = "",
                    localRevisionJson = repository.json.encodeToString(missingLocal),
                    remoteRevisionJson = repository.json.encodeToString(missingRemote),
                    conflictingPathsJson = "[\"payload\"]", status = "unresolved",
                    createdAt = timestamp, resolvedAt = null,
                ),
                missingEntityId,
            )
            repository.commitConflictResolution(
                missingConflict,
                missingLocal,
                managedAsset = ManagedAssetEntity(
                    id = missingEntityId, sha256 = "0".repeat(64), mediaType = "image/png",
                    extension = ".png", byteCount = 1, relativePath = null,
                    unresolved = true, createdAt = timestamp,
                ),
            )
            val unresolved = requireNotNull(repository.asset(missingEntityId))
            assertTrue(unresolved.unresolved)
            assertEquals(null, unresolved.relativePath)
        } finally {
            database.close()
        }
    }

    @Test
    fun failedRegistrationConfirmationClearsSecretsAndPendingRegistration() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        val vault = SecretVault(context)
        val tokenKeys = listOf(
            "sync.access_token", "sync.refresh_token", "sync.account_data_key", "sync.pending_registration"
        )
        try {
            vault.deleteAll(tokenKeys)
            val repository = DomainRepository(database, "device_rollback_test")
            val accounts = SyncAccountManager(repository, vault)
            val pending = PendingRegistration(
                serverUrl = "https://127.0.0.1:1",
                accountId = "account_rollback",
                deviceId = "device_rollback",
                deviceName = "回滚测试设备",
                material = RecoveryMaterial(
                    accountDataKey = ByteArray(32),
                    recoveryKey = "AAAA-BBBB-CCCC-DDDD",
                    envelopeNonce = "bm9uY2U=",
                    envelopeCiphertext = "Y2lwaGVydGV4dA==",
                    keyVersion = 1,
                ),
            )
            vault.putAll(
                mapOf(
                    "sync.access_token" to "synthetic-rollback-token".toByteArray(),
                    "sync.refresh_token" to "synthetic-rollback-refresh".toByteArray(),
                    "sync.pending_registration" to repository.json.encodeToString(pending).toByteArray(),
                )
            )
            val failure = runCatching { accounts.confirmRegistration(pending, "AAAA-BBBB-CCCC-DDDD") }
            assertTrue(failure.isFailure)
            assertEquals(null, vault.get("sync.access_token"))
            assertEquals(null, vault.get("sync.refresh_token"))
            assertEquals(null, vault.get("sync.account_data_key"))
            assertEquals(null, vault.get("sync.pending_registration"))
            assertEquals(null, accounts.pendingRegistration())
            assertTrue(repository.syncConfiguration()?.enabled != true)
        } finally {
            vault.deleteAll(tokenKeys)
            database.close()
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
    fun aiGenerationSnapshotRejectsCommitWhenAnySourceHeadChanged() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_snapshot_test")
            val timestamp = Instant.now().toString()
            val taskId = DomainRevision.id("task")
            val inputId = DomainRevision.id("input")
            val foodId = DomainRevision.id("food")
            repository.save(
                EntityKind.TASK,
                buildJsonObject {
                    put("task", buildJsonObject {
                        put("id", taskId); put("type", "material"); put("status", "pending")
                        put("created_at", timestamp); put("result_version", 0)
                    })
                },
                taskId,
            )
            repository.save(
                EntityKind.TASK_INPUT,
                buildJsonObject {
                    put("task_id", taskId); put("task_type", "material")
                    put("input_version", 1); put("original_input", "analyze meal")
                    put("input_history", buildJsonArray {})
                },
                inputId,
            )
            fun foodPayload(name: String) = buildJsonObject {
                put("food", buildJsonObject {
                    put("id", foodId); put("name", name); put("brand", "")
                    put("basis", "100g"); put("energy_kcal", 120); put("protein_g", 25)
                    put("carbs_g", 0); put("fat_g", 2); put("category", "other")
                    put("menu_priority", "normal"); put("notes", "")
                    put("created_at", timestamp); put("updated_at", timestamp)
                })
                put("history", buildJsonArray {})
            }
            repository.save(EntityKind.FOOD_ITEM, foodPayload("chicken"), foodId)

            data class Snapshot(val inputId: String, val taskId: String, val heads: List<EntityHeadEntity>)
            suspend fun capture(): Snapshot = repository.mutateTransaction {
                val input = records(EntityKind.TASK_INPUT).single()
                val task = requireNotNull(
                    record(Json.parseToJsonElement(input.payloadJson).jsonObject.getValue("task_id").jsonPrimitive.content)
                )
                val foods = records(EntityKind.FOOD_ITEM)
                val ids = setOf(input.entityId, task.entityId) + foods.map { it.entityId }
                Snapshot(input.entityId, task.entityId, heads().filter { it.entityId in ids })
            }
            suspend fun commit(snapshot: Snapshot) = repository.mutateTransaction {
                for (headRow in snapshot.heads) {
                    require(head(headRow.entityId)?.revisionId == headRow.revisionId) { "stale-source" }
                }
            }

            val captured = capture()
            repository.save(EntityKind.FOOD_ITEM, foodPayload("chicken breast"), foodId)
            val stale = runCatching { commit(captured) }
            assertTrue(stale.isFailure)
            assertEquals("stale-source", stale.exceptionOrNull()?.message)
            assertEquals(1, repository.records(EntityKind.FOOD_ITEM).size)

            val recaptured = capture()
            val clean = runCatching { commit(recaptured) }
            assertTrue(clean.isSuccess)
        } finally {
            database.close()
        }
    }

    @Test
    fun mediaPolicyUpdateWaitsForSyncAndPreservesAdvancedCursor() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            repository.putSyncConfiguration(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://sync.invalid",
                    accountId = "account_test",
                    cursor = 1,
                    updatedAt = Instant.now().toString(),
                )
            )
            val entered = CompletableDeferred<Unit>()
            val release = CompletableDeferred<Unit>()
            val sync = launch {
                repository.withMutationGate {
                    val current = requireNotNull(repository.syncConfiguration())
                    entered.complete(Unit)
                    release.await()
                    repository.putSyncConfiguration(current.copy(cursor = 99))
                }
            }
            entered.await()
            val policy = async { repository.updateMediaPolicy("on_demand") }
            yield()
            release.complete(Unit)
            sync.join()
            policy.await()

            val updated = requireNotNull(repository.syncConfiguration())
            assertEquals(99, updated.cursor)
            assertEquals("on_demand", updated.mediaPolicy)
        } finally {
            database.close()
        }
    }

    @Test
    fun mutationsWaitForApplicationInitialization() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        val initialization = CompletableDeferred<Unit>()
        try {
            val repository = DomainRepository(
                database,
                "device_ready_test",
                initializationGate = initialization,
            )
            val entityId = DomainRevision.id("record")
            val save = async {
                repository.save(
                    EntityKind.DAILY_RECORD,
                    buildJsonObject {
                        put("id", entityId)
                        put("record_date", "2026-08-04")
                        put("raw_input", "等待初始化")
                        put("created_at", Instant.now().toString())
                    },
                    entityId,
                )
            }
            yield()
            assertTrue(!save.isCompleted)
            initialization.complete(Unit)
            assertEquals(entityId, save.await().entityId)
        } finally {
            database.close()
        }
    }

    @Test
    fun abortWithoutLocalRotationStagingDoesNotRequireNetwork() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        val vault = SecretVault(context)
        val stagingKeys = listOf(
            "sync.rotation.account_data_key",
            "sync.rotation.recovery_key",
            "sync.rotation.material",
            "sync.rotation.key_version",
        )
        try {
            vault.deleteAll(stagingKeys)
            val repository = DomainRepository(database, "device_test")
            repository.putSyncConfiguration(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://offline.invalid",
                    accountId = "account_test",
                    updatedAt = Instant.now().toString(),
                )
            )
            KeyRotationManager(context, repository, vault).abort()
            assertTrue(requireNotNull(repository.syncConfiguration()).enabled)
        } finally {
            vault.deleteAll(stagingKeys)
            database.close()
        }
    }

    @Test
    fun conflictedHeadRejectsOrdinarySaveAndStaysConflictedWhenMaterialized() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            val recordId = DomainRevision.id("record")
            val original = repository.save(
                EntityKind.DAILY_RECORD,
                dailyRecordPayload(recordId, "original"),
                recordId,
            )
            repository.markHeadConflict(recordId, true)

            val saveResult = runCatching {
                repository.save(
                    EntityKind.DAILY_RECORD,
                    dailyRecordPayload(recordId, "must not overwrite conflict"),
                    recordId,
                )
            }
            assertTrue(saveResult.isFailure)
            assertEquals(original.revisionId, repository.heads().single().revisionId)
            assertTrue(repository.heads().single().conflicted)

            val remote = DomainRevision.create(
                kind = EntityKind.DAILY_RECORD,
                entityId = recordId,
                parents = listOf(original.revisionId),
                deviceId = "device_remote",
                payload = dailyRecordPayload(recordId, "remote materialization"),
            )
            repository.storeRevision(remote, materialize = true)
            assertEquals(remote.revisionId, repository.heads().single().revisionId)
            assertTrue(repository.heads().single().conflicted)
        } finally {
            database.close()
        }
    }

    @Test
    fun duplicateRevisionIdIsIdempotentOnlyForIdenticalContent() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            val recordId = DomainRevision.id("record")
            val revision = DomainRevision.create(
                kind = EntityKind.DAILY_RECORD,
                entityId = recordId,
                deviceId = "device_test",
                payload = dailyRecordPayload(recordId, "canonical"),
            )
            repository.commitRevision(revision, queue = false)
            repository.storeRevision(revision, materialize = true)

            val collision = revision.copy(payload = dailyRecordPayload(recordId, "collision"))
            assertTrue(runCatching { repository.storeRevision(collision, materialize = true) }.isFailure)
            assertEquals(revision, repository.revision(revision.revisionId))
            assertEquals("canonical", repository.record(recordId)?.payloadJson?.let {
                repository.json.parseToJsonElement(it).jsonObject.getValue("raw_input").jsonPrimitive.content
            })
        } finally {
            database.close()
        }
    }

    @Test
    fun mutationGateIsReentrantAndSerializesMutations() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            withTimeout(5_000) {
                repository.withMutationGate {
                    repository.withMutationGate { Unit }
                }
            }

            val entered = CompletableDeferred<Unit>()
            val release = CompletableDeferred<Unit>()
            val secondStarted = CompletableDeferred<Unit>()
            val events = mutableListOf<String>()
            val first = launch {
                repository.withMutationGate {
                    events += "first-start"
                    entered.complete(Unit)
                    release.await()
                    events += "first-end"
                }
            }
            entered.await()
            val second = launch {
                secondStarted.complete(Unit)
                repository.withMutationGate { events += "second" }
            }
            secondStarted.await()
            yield()
            assertEquals(listOf("first-start"), events)
            release.complete(Unit)
            first.join()
            second.join()
            assertEquals(listOf("first-start", "first-end", "second"), events)
        } finally {
            database.close()
        }
    }

    @Test
    fun mutationTransactionCommitsTogetherWithoutReenteringTheGate() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            val rolledBackId = DomainRevision.id("record")
            var failureCleanupRan = false
            val failed = runCatching {
                withTimeout(5_000) {
                    repository.mutateTransaction(
                        onFailure = {
                            failureCleanupRan = true
                            error("synthetic cleanup")
                        },
                    ) {
                        save(
                            EntityKind.DAILY_RECORD,
                            dailyRecordPayload(rolledBackId, "must roll back"),
                            rolledBackId,
                        )
                        error("synthetic rollback")
                    }
                }
            }
            assertTrue(failed.isFailure)
            assertTrue(failureCleanupRan)
            assertEquals("synthetic rollback", failed.exceptionOrNull()?.message)
            assertEquals("synthetic cleanup", failed.exceptionOrNull()?.suppressed?.single()?.message)
            assertEquals(null, repository.record(rolledBackId))

            val firstId = DomainRevision.id("record")
            val secondId = DomainRevision.id("record")
            withTimeout(5_000) {
                repository.mutateTransaction {
                    save(EntityKind.DAILY_RECORD, dailyRecordPayload(firstId, "first"), firstId)
                    save(EntityKind.DAILY_RECORD, dailyRecordPayload(secondId, "second"), secondId)
                }
            }
            assertEquals("first", repository.record(firstId)?.payloadJson?.let {
                repository.json.parseToJsonElement(it).jsonObject.getValue("raw_input").jsonPrimitive.content
            })
            assertEquals("second", repository.record(secondId)?.payloadJson?.let {
                repository.json.parseToJsonElement(it).jsonObject.getValue("raw_input").jsonPrimitive.content
            })
        } finally {
            database.close()
        }
    }

    @Test
    fun accountChangeClearsSyncStateAndPreservesDomainDataAndAssets() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        try {
            val repository = DomainRepository(database, "device_test")
            val recordId = DomainRevision.id("record")
            repository.save(EntityKind.DAILY_RECORD, dailyRecordPayload(recordId, "kept"), recordId)
            repository.putAsset(
                ManagedAssetEntity(
                    id = "asset_kept",
                    sha256 = "a".repeat(64),
                    mediaType = "image/jpeg",
                    extension = "jpg",
                    byteCount = 12,
                    relativePath = "assets/kept.jpg",
                    unresolved = false,
                    createdAt = Instant.now().toString(),
                )
            )
            repository.putSyncConfiguration(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://old.invalid",
                    accountId = "account_old",
                    keyVersion = 1,
                    cursor = 91,
                    updatedAt = Instant.now().toString(),
                )
            )
            val secondId = DomainRevision.id("record")
            repository.save(EntityKind.DAILY_RECORD, dailyRecordPayload(secondId, "queued"), secondId)
            val oldOpId = repository.pending().single().opId
            seedAccountScopedState(repository, recordId, "configure")

            repository.configureSync(
                SyncConfigurationEntity(
                    enabled = true,
                    serverUrl = "https://new.invalid",
                    accountId = "account_new",
                    remoteDeviceId = "device_remote",
                    keyVersion = 2,
                    cursor = 123,
                    updatedAt = Instant.now().toString(),
                )
            )

            val configured = requireNotNull(repository.syncConfiguration())
            assertEquals(0, configured.cursor)
            assertEquals(null, repository.outbox(oldOpId))
            assertEquals(repository.heads().size, repository.pending().size)
            assertTrue(repository.pending().all { it.baseServerVersion == 0L && it.keyVersion == 2 })
            assertEquals(null, repository.shadowForEntity(recordId))
            assertEquals(Triple(2, 0, 0), repository.rotationReadiness())
            assertTrue(repository.heads().none { it.conflicted })

            seedAccountScopedState(repository, recordId, "unlink")
            val accountManager = SyncAccountManager(repository, SecretVault(context))
            val blocked = runCatching { accountManager.unlink() }.exceptionOrNull()
            assertTrue(blocked is IllegalArgumentException)
            assertTrue(requireNotNull(repository.syncConfiguration()).enabled)
            assertEquals("unresolved", repository.conflict("conflict_unlink")?.status)
            assertTrue(repository.shadowForEntity(recordId) != null)
            assertTrue(repository.unknownEntities().isNotEmpty())
            assertTrue(repository.heads().first { it.entityId == recordId }.conflicted)

            repository.resolveConflict("conflict_unlink", Instant.now().toString())
            repository.markHeadConflict(recordId, false)
            accountManager.unlink()

            val disabled = requireNotNull(repository.syncConfiguration())
            assertTrue(!disabled.enabled)
            assertEquals(null, disabled.serverUrl)
            assertEquals(null, disabled.accountId)
            assertEquals(0, disabled.cursor)
            assertTrue(repository.pending().isEmpty())
            assertEquals(null, repository.shadowForEntity(recordId))
            assertEquals(null, repository.conflict("conflict_unlink"))
            assertTrue(repository.unknownEntities().isEmpty())
            assertTrue(repository.heads().none { it.conflicted })
            assertEquals("kept", repository.record(recordId)?.payloadJson?.let {
                repository.json.parseToJsonElement(it).jsonObject.getValue("raw_input").jsonPrimitive.content
            })
            assertEquals("asset_kept", repository.asset("asset_kept")?.id)
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
            val opaqueEnvelope = "{\"ciphertext\":\"opaque-未来-schema\"}"
            repository.putUnknown(
                UnknownEntity(
                    remoteId = "a".repeat(64),
                    serverVersion = 7,
                    keyVersion = 2,
                    encryptedEnvelope = opaqueEnvelope,
                    updatedAt = Instant.now().toString(),
                )
            )
            assertEquals(1, database.dao().unknownCount())
            assertEquals(7L, repository.unknown("a".repeat(64))?.serverVersion)
            assertEquals(1, repository.observeUnknownCount().first())
            assertEquals(opaqueEnvelope.encodeToByteArray().size.toLong(), repository.unknownByteCount())
            assertEquals(opaqueEnvelope.encodeToByteArray().size.toLong(), repository.unknownMaxByteCount())
            assertEquals(1, repository.unknownEntities(1).size)
            repository.putUnknown(
                requireNotNull(repository.unknown("a".repeat(64))).copy(
                    serverVersion = 8,
                    updatedAt = Instant.now().toString(),
                )
            )
            assertEquals(1, database.dao().unknownCount())
            assertEquals(8L, repository.unknown("a".repeat(64))?.serverVersion)
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
    fun portableImportUsesThePrivateFileThatWasActuallyPreviewed() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val database = Room.inMemoryDatabaseBuilder(context, MealCircuitDatabase::class.java).build()
        val external = File.createTempFile("replaceable-portable-", ".mcx", context.cacheDir)
        try {
            context.assets.open("fixtures/portable-v1.mcx").use { input ->
                external.outputStream().use { output -> input.copyTo(output) }
            }
            val repository = DomainRepository(database, "device_android_staged_fixture")
            val metadata = context.assets.open("fixtures/portable-v1-meta.json").bufferedReader().use {
                repository.json.parseToJsonElement(it.readText()).jsonObject
            }
            val recovery = metadata.getValue("recovery_key").jsonPrimitive.content
            val portable = PortableData(context, repository)
            val staging = external.inputStream().use { input ->
                portable.previewAndStage(input, recovery, ImportMode.RESTORE)
            }
            try {
                external.writeText("内容已在预检后被替换")
                val applied = portable.importStaged(staging, recovery, ImportMode.RESTORE)
                assertEquals(staging.preview, applied)
                val entityId = metadata.getValue("entity_id").jsonPrimitive.content
                assertEquals(
                    "合成测试燕麦",
                    repository.record(entityId)?.payloadJson?.let {
                        repository.json.parseToJsonElement(it).jsonObject
                            .getValue("food").jsonObject.getValue("name").jsonPrimitive.content
                    },
                )
            } finally {
                portable.discardStaged(staging)
                assertTrue(!staging.source.exists())
            }
        } finally {
            external.delete()
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
        assertEquals(
            androidx.work.ExistingWorkPolicy.APPEND_OR_REPLACE,
            org.mealcircuit.app.sync.SYNC_EXISTING_WORK_POLICY,
        )
    }

    @Test
    fun unmeteredFollowUpWaitsForUnmeteredNetwork() {
        val request = SyncWorker.buildRequest(NetworkType.UNMETERED)
        assertEquals(NetworkType.UNMETERED, request.workSpec.constraints.requiredNetworkType)
        assertEquals(BackoffPolicy.EXPONENTIAL, request.workSpec.backoffPolicy)
        assertEquals(30_000L, request.workSpec.backoffDelayDuration)
    }

    @Test
    fun keystoreWrappedSecretsCanBeCommittedAndDeletedAsOneSet() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val vault = SecretVault(context)
        val names = listOf("instrumentation.atomic.one", "instrumentation.atomic.two")
        vault.putAll(mapOf(names[0] to byteArrayOf(1), names[1] to byteArrayOf(2)))
        assertTrue(vault.get(names[0])!!.contentEquals(byteArrayOf(1)))
        assertTrue(vault.get(names[1])!!.contentEquals(byteArrayOf(2)))
        vault.deleteAll(names)
        assertEquals(null, vault.get(names[0]))
        assertEquals(null, vault.get(names[1]))
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
            val api = SyncApi(requireNotNull(configuration.serverUrl), vault)
            vault.put("sync.access_token", "synthetic-expired-access-token".toByteArray())
            coroutineScope {
                List(2) { async { api.authorized("/v1/devices") } }.awaitAll()
            }
            val engine = SyncEngine(
                repository,
                api,
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

    private fun dailyRecordPayload(entityId: String, rawInput: String) = buildJsonObject {
        put("id", entityId)
        put("record_date", "2026-07-10")
        put("raw_input", rawInput)
        put("created_at", Instant.now().toString())
    }

    private suspend fun seedAccountScopedState(
        repository: DomainRepository,
        entityId: String,
        suffix: String,
    ) {
        val head = repository.heads().first { it.entityId == entityId }
        repository.putShadow(
            SyncShadowEntity(
                remoteId = "remote_$suffix",
                entityId = entityId,
                serverVersion = 7,
                revisionId = head.revisionId,
                payloadJson = requireNotNull(repository.record(entityId)).payloadJson,
                updatedAt = Instant.now().toString(),
            )
        )
        repository.putConflict(
            SyncConflictEntity(
                id = "conflict_$suffix",
                entityId = entityId,
                entityKind = "daily_record",
                baseRevisionJson = null,
                localRevisionJson = "{}",
                remoteRevisionJson = "{}",
                conflictingPathsJson = "[]",
                status = "unresolved",
                createdAt = Instant.now().toString(),
                resolvedAt = null,
            )
        )
        repository.putUnknown(
            UnknownEntity(
                remoteId = "unknown_$suffix",
                serverVersion = 8,
                keyVersion = 1,
                encryptedEnvelope = "{}",
                updatedAt = Instant.now().toString(),
            )
        )
        repository.markHeadConflict(entityId, true)
    }
}
