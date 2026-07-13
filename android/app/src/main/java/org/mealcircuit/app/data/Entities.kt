package org.mealcircuit.app.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(tableName = "app_metadata")
data class AppMetadataEntity(
    @PrimaryKey val key: String,
    val value: String,
)

@Entity(tableName = "domain_revisions", indices = [Index("entityId"), Index("entityKind")])
data class DomainRevisionEntity(
    @PrimaryKey val revisionId: String,
    val entityId: String,
    val entityKind: String,
    val parentRevisionIdsJson: String,
    val payloadJson: String,
    val schemaVersion: Int,
    val authorDeviceId: String,
    val deleted: Boolean,
    val createdAt: String,
)

@Entity(tableName = "entity_heads", indices = [Index("entityKind")])
data class EntityHeadEntity(
    @PrimaryKey val entityId: String,
    val entityKind: String,
    val revisionId: String,
    val conflicted: Boolean,
    val updatedAt: String,
)

@Entity(tableName = "materialized_records", indices = [Index("entityKind"), Index("sortKey")])
data class MaterializedRecordEntity(
    @PrimaryKey val entityId: String,
    val entityKind: String,
    val payloadJson: String,
    val deleted: Boolean,
    val sortKey: String,
    val updatedAt: String,
)

@Entity(tableName = "sync_outbox", indices = [Index(value = ["opId"], unique = true), Index("entityId")])
data class SyncOutboxEntity(
    @PrimaryKey(autoGenerate = true) val localSequence: Long = 0,
    val opId: String,
    val remoteId: String,
    val entityId: String,
    val revisionId: String,
    val baseServerVersion: Long,
    val encryptedEnvelope: String?,
    val keyVersion: Int,
    val state: String,
    val createdAt: String,
    val updatedAt: String,
)

@Entity(tableName = "sync_shadow", indices = [Index(value = ["entityId"], unique = true)])
data class SyncShadowEntity(
    @PrimaryKey val remoteId: String,
    val entityId: String,
    val serverVersion: Long,
    val revisionId: String,
    val payloadJson: String,
    val updatedAt: String,
)

@Entity(tableName = "sync_conflicts", indices = [Index("entityId"), Index("status")])
data class SyncConflictEntity(
    @PrimaryKey val id: String,
    val entityId: String,
    val entityKind: String,
    val baseRevisionJson: String?,
    val localRevisionJson: String,
    val remoteRevisionJson: String,
    val conflictingPathsJson: String,
    val status: String,
    val createdAt: String,
    val resolvedAt: String?,
)

@Entity(tableName = "sync_unknown_entities")
data class UnknownEntity(
    @PrimaryKey val remoteId: String,
    val serverVersion: Long,
    val keyVersion: Int,
    val encryptedEnvelope: String,
    val updatedAt: String,
)

@Entity(tableName = "managed_assets", indices = [Index(value = ["sha256"], unique = true)])
data class ManagedAssetEntity(
    @PrimaryKey val id: String,
    val sha256: String,
    val mediaType: String,
    val extension: String,
    val byteCount: Long,
    val relativePath: String?,
    val unresolved: Boolean,
    val createdAt: String,
)

@Entity(tableName = "sync_configuration")
data class SyncConfigurationEntity(
    @PrimaryKey val singleton: Int = 1,
    val enabled: Boolean = false,
    val serverUrl: String? = null,
    val accountId: String? = null,
    val remoteDeviceId: String? = null,
    val deviceName: String = "",
    val keyVersion: Int = 1,
    val mediaPolicy: String = "all_wifi",
    val cursor: Long = 0,
    val updatedAt: String,
)
