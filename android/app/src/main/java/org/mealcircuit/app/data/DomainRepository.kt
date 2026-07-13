package org.mealcircuit.app.data

import androidx.room.withTransaction
import kotlinx.coroutines.flow.Flow
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

    suspend fun ensureMetadata(instanceId: String) = database.withTransaction {
        if (dao.metadata("schema_version") == null) dao.putMetadata(AppMetadataEntity("schema_version", "2"))
        if (dao.metadata("instance_id") == null) dao.putMetadata(AppMetadataEntity("instance_id", instanceId))
        if (dao.metadata("device_id") == null) dao.putMetadata(AppMetadataEntity("device_id", deviceId))
        if (dao.metadata("created_at") == null) {
            dao.putMetadata(AppMetadataEntity("created_at", Instant.now().toString()))
        }
    }

    suspend fun cleanupOrphanedAssetFiles(filesDir: File): Int {
        val known = dao.assets().mapNotNull { it.relativePath }.map { File(filesDir, it).canonicalFile }.toSet()
        val root = File(filesDir, "assets")
        var removed = 0
        root.listFiles()?.filter { it.isFile && it.canonicalFile !in known }?.forEach { file ->
            if (file.delete()) removed += 1
        }
        return removed
    }

    fun observe(kind: EntityKind): Flow<List<MaterializedRecordEntity>> =
        dao.observeRecords(kind.serialized())
    suspend fun records(kind: EntityKind): List<MaterializedRecordEntity> = dao.records(kind.serialized())

    fun observeConflicts(): Flow<List<SyncConflictEntity>> = dao.observeConflicts()
    fun observeSyncConfiguration(): Flow<SyncConfigurationEntity?> = dao.observeSyncConfiguration()
    fun observePendingCount(): Flow<Int> = dao.observePendingCount()
    fun observeHeads(): Flow<List<EntityHeadEntity>> = dao.observeHeads()

    suspend fun save(
        kind: EntityKind,
        payload: JsonObject,
        entityId: String = DomainRevision.id(kind.prefix()),
        deleted: Boolean = false,
    ): DomainRevision = database.withTransaction {
        val head = dao.head(entityId)
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
        revision
    }

    suspend fun storeRevision(revision: DomainRevision, materialize: Boolean) {
        revision.validate()
        dao.insertRevision(revision.asEntity(json))
        if (materialize) {
            dao.putHead(
                EntityHeadEntity(
                    entityId = revision.entityId,
                    entityKind = revision.entityKind.serialized(),
                    revisionId = revision.revisionId,
                    conflicted = false,
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

    suspend fun commitRevision(revision: DomainRevision, queue: Boolean = true) =
        database.withTransaction {
            storeRevision(revision, materialize = true)
            if (queue) queueRevision(revision)
        }

    suspend fun commitConflictResolution(
        conflictId: String,
        resolved: DomainRevision,
        tombstone: DomainRevision? = null,
    ) = database.withTransaction {
        storeRevision(resolved, materialize = true)
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

    suspend fun commitLogicalMerge(merged: DomainRevision, tombstone: DomainRevision) =
        database.withTransaction {
            storeRevision(merged, materialize = true)
            queueRevision(merged)
            storeRevision(tombstone, materialize = true)
            queueRevision(tombstone)
        }

    suspend fun commitSyncConflict(value: SyncConflictEntity, entityId: String) =
        database.withTransaction {
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
