package org.mealcircuit.app.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface MealCircuitDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putMetadata(value: AppMetadataEntity)

    @Query("SELECT value FROM app_metadata WHERE `key`=:key")
    suspend fun metadata(key: String): String?

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertRevision(value: DomainRevisionEntity): Long

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putHead(value: EntityHeadEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putRecord(value: MaterializedRecordEntity)

    @Query("SELECT * FROM materialized_records WHERE entityKind=:kind AND deleted=0 ORDER BY sortKey DESC")
    fun observeRecords(kind: String): Flow<List<MaterializedRecordEntity>>

    @Query("SELECT * FROM materialized_records WHERE entityKind=:kind AND deleted=0 ORDER BY sortKey DESC")
    suspend fun records(kind: String): List<MaterializedRecordEntity>

    @Query("SELECT * FROM materialized_records WHERE entityId=:entityId")
    suspend fun record(entityId: String): MaterializedRecordEntity?

    @Query("SELECT * FROM domain_revisions WHERE revisionId=:revisionId")
    suspend fun revision(revisionId: String): DomainRevisionEntity?

    @Query("SELECT * FROM domain_revisions ORDER BY entityKind,entityId,createdAt,revisionId")
    suspend fun revisions(): List<DomainRevisionEntity>

    @Query("SELECT * FROM entity_heads WHERE entityId=:entityId")
    suspend fun head(entityId: String): EntityHeadEntity?

    @Query("SELECT * FROM entity_heads ORDER BY entityKind,entityId")
    suspend fun heads(): List<EntityHeadEntity>

    @Query("SELECT * FROM entity_heads ORDER BY entityKind,entityId")
    fun observeHeads(): Flow<List<EntityHeadEntity>>

    @Insert
    suspend fun enqueue(value: SyncOutboxEntity)

    @Query("DELETE FROM sync_outbox WHERE entityId=:entityId AND state='pending'")
    suspend fun coalescePending(entityId: String)

    @Query("SELECT * FROM sync_outbox WHERE state='pending' ORDER BY localSequence LIMIT :limit")
    suspend fun pending(limit: Int = 100): List<SyncOutboxEntity>

    @Query("SELECT * FROM sync_outbox WHERE opId=:opId")
    suspend fun outbox(opId: String): SyncOutboxEntity?

    @Query("SELECT * FROM sync_outbox WHERE entityId=:entityId AND state IN ('pending','sending') ORDER BY localSequence DESC LIMIT 1")
    suspend fun pendingForEntity(entityId: String): SyncOutboxEntity?

    @Query("DELETE FROM sync_outbox WHERE opId=:opId")
    suspend fun deleteOutbox(opId: String)

    @Query("UPDATE sync_outbox SET remoteId=:remoteId,encryptedEnvelope=:envelope,updatedAt=:updatedAt WHERE opId=:opId")
    suspend fun prepareOutbox(opId: String, remoteId: String, envelope: String, updatedAt: String)

    @Query("UPDATE sync_outbox SET state='conflict',updatedAt=:updatedAt WHERE entityId=:entityId")
    suspend fun markOutboxConflict(entityId: String, updatedAt: String)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putShadow(value: SyncShadowEntity)

    @Query("SELECT * FROM sync_shadow WHERE entityId=:entityId")
    suspend fun shadowForEntity(entityId: String): SyncShadowEntity?

    @Query("SELECT * FROM sync_shadow WHERE remoteId=:remoteId")
    suspend fun shadow(remoteId: String): SyncShadowEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putConflict(value: SyncConflictEntity)

    @Query("UPDATE entity_heads SET conflicted=:conflicted WHERE entityId=:entityId")
    suspend fun markHeadConflict(entityId: String, conflicted: Boolean)

    @Query("SELECT * FROM sync_conflicts WHERE status='unresolved' ORDER BY createdAt")
    fun observeConflicts(): Flow<List<SyncConflictEntity>>

    @Query("SELECT * FROM sync_conflicts WHERE id=:id")
    suspend fun conflict(id: String): SyncConflictEntity?

    @Query("UPDATE sync_conflicts SET status='resolved',resolvedAt=:resolvedAt WHERE id=:id")
    suspend fun resolveConflict(id: String, resolvedAt: String)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putUnknown(value: UnknownEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putAsset(value: ManagedAssetEntity)

    @Query("SELECT * FROM managed_assets WHERE unresolved=1 OR relativePath IS NULL")
    suspend fun unresolvedAssets(): List<ManagedAssetEntity>

    @Query("SELECT * FROM managed_assets WHERE id=:id")
    suspend fun asset(id: String): ManagedAssetEntity?

    @Query("SELECT * FROM managed_assets ORDER BY createdAt,id")
    suspend fun assets(): List<ManagedAssetEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putSyncConfiguration(value: SyncConfigurationEntity)

    @Query("SELECT * FROM sync_configuration WHERE singleton=1")
    fun observeSyncConfiguration(): Flow<SyncConfigurationEntity?>

    @Query("SELECT * FROM sync_configuration WHERE singleton=1")
    suspend fun syncConfiguration(): SyncConfigurationEntity?

    @Query("SELECT COUNT(*) FROM sync_outbox WHERE state='pending'")
    fun observePendingCount(): Flow<Int>

    @Query("SELECT COUNT(*) FROM sync_outbox WHERE state='pending'")
    suspend fun pendingCount(): Int

    @Query("SELECT COUNT(*) FROM sync_conflicts WHERE status='unresolved'")
    suspend fun unresolvedConflictCount(): Int

    @Query("SELECT COUNT(*) FROM sync_unknown_entities")
    suspend fun unknownCount(): Int

    @Query("DELETE FROM sync_outbox")
    suspend fun clearOutbox()

    @Query("DELETE FROM sync_shadow")
    suspend fun clearShadows()
}
