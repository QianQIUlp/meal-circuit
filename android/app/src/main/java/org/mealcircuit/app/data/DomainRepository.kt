package org.mealcircuit.app.data

import androidx.room.withTransaction
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.domain.validateStateChange
import java.time.Instant
import java.io.File

class DomainRepository(
    private val database: MealCircuitDatabase,
    val deviceId: String,
    val json: Json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
        explicitNulls = false
    },
) {
    private val dao = database.dao()
    private val mutationGate = Mutex()

    suspend fun <T> withMutationGate(block: suspend () -> T): T {
        val owner = requireNotNull(currentCoroutineContext()[Job]) { "数据写入门禁需要协程任务上下文" }
        if (mutationGate.holdsLock(owner)) return block()
        mutationGate.lock(owner)
        return try {
            block()
        } finally {
            mutationGate.unlock(owner)
        }
    }

    suspend fun ensureMetadata(instanceId: String) = database.withTransaction {
        if (dao.metadata("schema_version") == null) dao.putMetadata(AppMetadataEntity("schema_version", "2"))
        if (dao.metadata("instance_id") == null) dao.putMetadata(AppMetadataEntity("instance_id", instanceId))
        if (dao.metadata("device_id") == null) dao.putMetadata(AppMetadataEntity("device_id", deviceId))
        if (dao.metadata("created_at") == null) {
            dao.putMetadata(AppMetadataEntity("created_at", Instant.now().toString()))
        }
    }

    suspend fun cleanupOrphanedAssetFiles(filesDir: File): Int {
        val root = File(filesDir, "assets")
        return cleanupUnreferencedAssetFiles(filesDir, root.listFiles()?.asIterable() ?: emptyList())
    }

    suspend fun cleanupUnreferencedAssetFiles(filesDir: File, candidates: Iterable<File>): Int {
        val root = File(filesDir, "assets").canonicalFile
        val known = dao.assets()
            .mapNotNull { it.relativePath }
            .map { File(filesDir, it).canonicalFile }
            .toSet()
        var removed = 0
        candidates
            .map { it.canonicalFile }
            .distinct()
            .filter { it.isFile && it.parentFile == root && it !in known }
            .forEach { file -> if (file.delete()) removed += 1 }
        return removed
    }

    fun observe(kind: EntityKind): Flow<List<MaterializedRecordEntity>> =
        dao.observeRecords(kind.serialized())
    suspend fun records(kind: EntityKind): List<MaterializedRecordEntity> = dao.records(kind.serialized())

    fun observeConflicts(): Flow<List<SyncConflictEntity>> = dao.observeConflicts()
    fun observeSyncConfiguration(): Flow<SyncConfigurationEntity?> = dao.observeSyncConfiguration()
    fun observePendingCount(): Flow<Int> = dao.observePendingCount()
    fun observeUnknownCount(): Flow<Int> = dao.observeUnknownCount()
    fun observeHeads(): Flow<List<EntityHeadEntity>> = dao.observeHeads()

    suspend fun save(
        kind: EntityKind,
        payload: JsonObject,
        entityId: String = DomainRevision.id(kind.prefix()),
        deleted: Boolean = false,
    ): DomainRevision = withMutationGate {
        database.withTransaction { saveInTransaction(kind, payload, entityId, deleted) }
    }

    internal inner class MutationTransaction internal constructor() {
        suspend fun save(
            kind: EntityKind,
            payload: JsonObject,
            entityId: String = DomainRevision.id(kind.prefix()),
            deleted: Boolean = false,
        ): DomainRevision = saveInTransaction(kind, payload, entityId, deleted)

        suspend fun asset(id: String): ManagedAssetEntity? = dao.asset(id)

        suspend fun record(id: String): MaterializedRecordEntity? = dao.record(id)

        suspend fun records(kind: EntityKind): List<MaterializedRecordEntity> =
            dao.records(kind.serialized())

        suspend fun head(id: String): EntityHeadEntity? = dao.head(id)

        suspend fun heads(): List<EntityHeadEntity> = dao.heads()

        suspend fun putAsset(value: ManagedAssetEntity) = dao.putAsset(value)
    }

    internal suspend fun <T> mutateTransaction(
        onFailure: suspend () -> Unit = {},
        block: suspend MutationTransaction.() -> T,
    ): T =
        withMutationGate {
            try {
                database.withTransaction { MutationTransaction().block() }
            } catch (error: Throwable) {
                try {
                    withContext(NonCancellable) { onFailure() }
                } catch (cleanupError: Throwable) {
                    if (cleanupError !== error) error.addSuppressed(cleanupError)
                }
                throw error
            }
        }

    private suspend fun saveInTransaction(
        kind: EntityKind,
        payload: JsonObject,
        entityId: String,
        deleted: Boolean,
    ): DomainRevision {
        val head = dao.head(entityId)
        require(head?.conflicted != true) { "该记录存在同步冲突，请先解决冲突再编辑" }
        head?.let { current ->
            dao.revision(current.revisionId)?.asDomain(json)?.let { previous ->
                validateStateChange(kind, previous.payload, payload)
            }
        }
        val revision = DomainRevision.create(
            kind = kind,
            entityId = entityId,
            parents = head?.let { listOf(it.revisionId) }.orEmpty(),
            deviceId = deviceId,
            payload = payload,
            deleted = deleted,
        )
        storeRevision(revision, materialize = true)
        val sync = dao.syncConfiguration()
        if (sync?.enabled == true) {
            dao.coalescePending(entityId)
            dao.enqueue(
                SyncOutboxEntity(
                    opId = DomainRevision.id("op"),
                    remoteId = "pending:$entityId",
                    entityId = entityId,
                    revisionId = revision.revisionId,
                    baseServerVersion = dao.shadowForEntity(entityId)?.serverVersion ?: 0,
                    encryptedEnvelope = null,
                    keyVersion = sync.keyVersion,
                    state = "pending",
                    createdAt = revision.createdAt,
                    updatedAt = revision.createdAt,
                )
            )
        }
        return revision
    }

    suspend fun storeRevision(revision: DomainRevision, materialize: Boolean) {
        revision.validate()
        val entity = revision.asEntity(json)
        val inserted = dao.insertRevision(entity)
        if (inserted == -1L) {
            require(dao.revision(revision.revisionId)?.asDomain(json) == revision) {
                "修订 ${revision.revisionId} 已存在，但内容不一致"
            }
        }
        if (materialize) {
            val previousHead = dao.head(revision.entityId)
            dao.putHead(
                EntityHeadEntity(
                    entityId = revision.entityId,
                    entityKind = revision.entityKind.serialized(),
                    revisionId = revision.revisionId,
                    conflicted = previousHead?.conflicted ?: false,
                    updatedAt = revision.createdAt,
                )
            )
            dao.putRecord(
                MaterializedRecordEntity(
                    entityId = revision.entityId,
                    entityKind = revision.entityKind.serialized(),
                    payloadJson = json.encodeToString(revision.payload),
                    deleted = revision.deleted,
                    sortKey = revision.payload["record_date"]?.toString()?.trim('"')
                        ?: (revision.payload["review"] as? JsonObject)?.get("review_date")?.toString()?.trim('"')
                        ?: (revision.payload["checkin"] as? JsonObject)?.get("checkin_date")?.toString()?.trim('"')
                        ?: revision.createdAt,
                    updatedAt = revision.createdAt,
                )
            )
        }
    }

    suspend fun commitRevision(
        revision: DomainRevision,
        queue: Boolean = true,
        managedAsset: ManagedAssetEntity? = null,
    ) =
        database.withTransaction {
            storeRevision(revision, materialize = true)
            managedAsset?.let { dao.putAsset(it) }
            if (queue) queueRevision(revision)
        }

    suspend fun commitRemoteRevision(
        revision: DomainRevision,
        shadow: SyncShadowEntity,
        managedAsset: ManagedAssetEntity? = null,
        materialize: Boolean = true,
    ) = database.withTransaction {
        storeRevision(revision, materialize = materialize)
        managedAsset?.let { dao.putAsset(it) }
        dao.putShadow(shadow)
    }

    suspend fun commitConflictResolution(
        conflictId: String,
        resolved: DomainRevision,
        tombstone: DomainRevision? = null,
        managedAsset: ManagedAssetEntity? = null,
    ) = withMutationGate {
        database.withTransaction {
            dao.deleteConflictOutbox(resolved.entityId)
            tombstone?.let { dao.deleteConflictOutbox(it.entityId) }
            storeRevision(resolved, materialize = true)
            managedAsset?.let { dao.putAsset(it) }
            queueRevision(resolved)
            tombstone?.let {
                storeRevision(it, materialize = true)
                queueRevision(it)
            }
            val timestamp = Instant.now().toString()
            dao.resolveConflict(conflictId, timestamp)
            dao.markHeadConflict(resolved.entityId, false)
            tombstone?.let { dao.markHeadConflict(it.entityId, false) }
        }
    }

    suspend fun commitKindConflictResolutionKeepingLocal(
        conflictId: String,
        local: DomainRevision,
    ) = withMutationGate {
        database.withTransaction {
            val head = requireNotNull(dao.head(local.entityId)) { "本地记录已不存在" }
            require(head.revisionId == local.revisionId && head.entityKind == local.entityKind.serialized()) {
                "本地记录在冲突解决期间已变化"
            }
            dao.deleteConflictOutbox(local.entityId)
            dao.resolveConflict(conflictId, Instant.now().toString())
            dao.markHeadConflict(local.entityId, false)
            queueRevision(local)
        }
    }

    suspend fun commitLogicalMerge(merged: DomainRevision, tombstone: DomainRevision) =
        database.withTransaction {
            storeRevision(merged, materialize = true)
            queueRevision(merged)
            storeRevision(tombstone, materialize = true)
            queueRevision(tombstone)
        }

    suspend fun commitSyncConflict(
        value: SyncConflictEntity,
        entityId: String,
        shadow: SyncShadowEntity? = null,
    ) =
        database.withTransaction {
            shadow?.let { dao.putShadow(it) }
            dao.putConflict(value)
            dao.markHeadConflict(entityId, true)
            dao.markOutboxConflict(entityId, Instant.now().toString())
        }

    private suspend fun queueRevision(revision: DomainRevision) {
        val sync = dao.syncConfiguration()
        if (sync?.enabled != true) return
        dao.coalescePending(revision.entityId)
        dao.enqueue(
            SyncOutboxEntity(
                opId = DomainRevision.id("op"), remoteId = "pending:${revision.entityId}",
                entityId = revision.entityId, revisionId = revision.revisionId,
                baseServerVersion = dao.shadowForEntity(revision.entityId)?.serverVersion ?: 0,
                encryptedEnvelope = null, keyVersion = sync.keyVersion, state = "pending",
                createdAt = revision.createdAt, updatedAt = revision.createdAt,
            )
        )
    }

    suspend fun revision(id: String): DomainRevision? = dao.revision(id)?.asDomain(json)
    suspend fun revisions(): List<DomainRevision> = dao.revisions().map { it.asDomain(json) }
    suspend fun record(id: String): MaterializedRecordEntity? = dao.record(id)
    suspend fun heads(): List<EntityHeadEntity> = dao.heads()
    suspend fun pending(limit: Int = 100): List<SyncOutboxEntity> = dao.pending(limit)
    suspend fun syncConfiguration(): SyncConfigurationEntity? = dao.syncConfiguration()
    suspend fun putSyncConfiguration(value: SyncConfigurationEntity) = dao.putSyncConfiguration(value)
    suspend fun updateMediaPolicy(value: String) = withMutationGate {
        require(value in setOf("all", "all_wifi", "on_demand")) { "照片同步策略无效" }
        val current = requireNotNull(dao.syncConfiguration()) { "同步尚未启用" }
        require(current.enabled) { "同步尚未启用" }
        dao.putSyncConfiguration(current.copy(mediaPolicy = value, updatedAt = Instant.now().toString()))
    }
    suspend fun configureSync(value: SyncConfigurationEntity) = withMutationGate {
        require(value.enabled)
        database.withTransaction {
            clearAccountScopedSyncState()
            val configured = value.copy(cursor = 0)
            dao.putSyncConfiguration(configured)
            dao.heads().forEach { head ->
                val revision = requireNotNull(dao.revision(head.revisionId)).asDomain(json)
                dao.enqueue(
                    SyncOutboxEntity(
                        opId = DomainRevision.id("op"),
                        remoteId = "pending:${revision.entityId}",
                        entityId = revision.entityId,
                        revisionId = revision.revisionId,
                        baseServerVersion = 0,
                        encryptedEnvelope = null,
                        keyVersion = configured.keyVersion,
                        state = "pending",
                        createdAt = revision.createdAt,
                        updatedAt = revision.createdAt,
                    )
                )
            }
        }
    }
    suspend fun disableSync(value: SyncConfigurationEntity) = withMutationGate {
        require(!value.enabled)
        database.withTransaction {
            clearAccountScopedSyncState()
            dao.putSyncConfiguration(value.copy(cursor = 0))
        }
    }
    suspend fun outbox(opId: String) = dao.outbox(opId)
    suspend fun pendingForEntity(entityId: String) = dao.pendingForEntity(entityId)
    suspend fun deleteOutbox(opId: String) = dao.deleteOutbox(opId)
    suspend fun prepareOutbox(opId: String, remoteId: String, envelope: String) =
        dao.prepareOutbox(opId, remoteId, envelope, Instant.now().toString())
    suspend fun putShadow(value: SyncShadowEntity) = dao.putShadow(value)
    suspend fun shadow(remoteId: String) = dao.shadow(remoteId)
    suspend fun shadowForEntity(entityId: String) = dao.shadowForEntity(entityId)
    suspend fun putConflict(value: SyncConflictEntity) = dao.putConflict(value)
    suspend fun conflict(id: String) = dao.conflict(id)
    suspend fun resolveConflict(id: String, time: String) = dao.resolveConflict(id, time)
    suspend fun markHeadConflict(entityId: String, value: Boolean) = dao.markHeadConflict(entityId, value)
    suspend fun markOutboxConflict(entityId: String) =
        dao.markOutboxConflict(entityId, Instant.now().toString())
    suspend fun putUnknown(value: UnknownEntity) = dao.putUnknown(value)
    suspend fun unknown(remoteId: String) = dao.unknown(remoteId)
    suspend fun unknownEntities() = dao.unknownEntities()
    suspend fun unknownEntities(limit: Int) = dao.unknownEntities(limit)
    suspend fun unknownByteCount() = dao.unknownByteCount()
    suspend fun unknownMaxByteCount() = dao.unknownMaxByteCount()
    suspend fun deleteUnknown(remoteId: String) = dao.deleteUnknown(remoteId)
    suspend fun putAsset(value: ManagedAssetEntity) = dao.putAsset(value)
    suspend fun asset(id: String) = dao.asset(id)
    suspend fun assets() = dao.assets()
    suspend fun unresolvedAssets() = dao.unresolvedAssets()

    suspend fun headRevisions(): List<DomainRevision> = dao.heads().map { head ->
        requireNotNull(dao.revision(head.revisionId)).asDomain(json)
    }

    suspend fun rotationReadiness(): Triple<Int, Int, Int> = Triple(
        dao.pendingCount(), dao.unresolvedConflictCount(), dao.unknownCount()
    )

    suspend fun finalizeKeyRotation(keyVersion: Int) = database.withTransaction {
        require(keyVersion > 1)
        val current = requireNotNull(dao.syncConfiguration())
        dao.clearOutbox()
        dao.clearShadows()
        dao.putSyncConfiguration(
            current.copy(keyVersion = keyVersion, cursor = 0, updatedAt = Instant.now().toString())
        )
    }

    private suspend fun clearAccountScopedSyncState() {
        dao.clearOutbox()
        dao.clearShadows()
        dao.clearConflicts()
        dao.clearUnknownEntities()
        dao.clearHeadConflicts()
    }

    suspend fun <T> importTransaction(block: suspend () -> T): T = database.withTransaction { block() }
}

fun DomainRevision.asEntity(json: Json) = DomainRevisionEntity(
    revisionId = revisionId,
    entityId = entityId,
    entityKind = entityKind.serialized(),
    parentRevisionIdsJson = json.encodeToString(parentRevisionIds),
    payloadJson = json.encodeToString(payload),
    schemaVersion = schemaVersion,
    authorDeviceId = authorDeviceId,
    deleted = deleted,
    createdAt = createdAt,
)

fun DomainRevisionEntity.asDomain(json: Json) = DomainRevision(
    schemaVersion = schemaVersion,
    entityId = entityId,
    entityKind = EntityKind.entries.first { it.serialized() == entityKind },
    revisionId = revisionId,
    parentRevisionIds = json.decodeFromString(parentRevisionIdsJson),
    createdAt = createdAt,
    authorDeviceId = authorDeviceId,
    deleted = deleted,
    payload = json.decodeFromString(payloadJson),
).validate()

fun EntityKind.serialized(): String = when (this) {
    EntityKind.TASK -> "task"
    EntityKind.TASK_INPUT -> "task_input"
    EntityKind.ANALYSIS_RESULT -> "analysis_result"
    EntityKind.CORRECTION -> "correction"
    EntityKind.FOOD_ITEM -> "food_item"
    EntityKind.DAILY_RECORD -> "daily_record"
    EntityKind.CHECKIN_DAY -> "checkin_day"
    EntityKind.CHECKIN_DRAFT -> "checkin_draft"
    EntityKind.DAILY_REVIEW -> "daily_review"
    EntityKind.MEMORY -> "memory"
    EntityKind.ADJUSTMENT -> "adjustment"
    EntityKind.PREFERENCES -> "preferences"
    EntityKind.ASSET -> "asset"
}

fun EntityKind.prefix(): String = when (this) {
    EntityKind.FOOD_ITEM -> "food"
    EntityKind.DAILY_RECORD -> "record"
    EntityKind.DAILY_REVIEW -> "review"
    EntityKind.ANALYSIS_RESULT -> "result"
    else -> serialized()
}
