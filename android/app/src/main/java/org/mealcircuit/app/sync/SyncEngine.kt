package org.mealcircuit.app.sync

import android.content.Context
import android.net.ConnectivityManager
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.SyncConflictEntity
import org.mealcircuit.app.data.SyncShadowEntity
import org.mealcircuit.app.data.UnknownEntity
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.data.asDomain
import org.mealcircuit.app.data.serialized
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.canonicalizeLogicalPayload
import org.mealcircuit.app.domain.threeWayMerge
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.io.readUpTo
import java.time.Instant
import java.security.MessageDigest
import kotlin.math.ceil

data class SyncSummary(
    var pushed: Int = 0,
    var accepted: Int = 0,
    var applied: Int = 0,
    var merged: Int = 0,
    var conflicts: Int = 0,
    var unknown: Int = 0,
    var cursor: Long = 0,
    var fullResync: Boolean = false,
    var assetsUploaded: Int = 0,
    var assetsDownloaded: Int = 0,
    val assetErrors: MutableList<String> = mutableListOf(),
)

class SyncEngine(
    private val repository: DomainRepository,
    private val api: SyncApi,
    private val cipher: AccountCipher,
    private val context: Context,
    private val json: Json = repository.json,
) {
    suspend fun run(includeOnDemandMedia: Boolean = false): SyncSummary {
        val summary = SyncSummary()
        val capabilities = api.capabilities()
        require(capabilities["protocol"]?.jsonPrimitive?.content == "mealcircuit.sync")
        require(capabilities["e2ee_required"]?.jsonPrimitive?.content == "true")
        val minVersion = capabilities["min_version"]?.jsonPrimitive?.content?.toIntOrNull() ?: error("Missing min_version")
        val maxVersion = capabilities["max_version"]?.jsonPrimitive?.content?.toIntOrNull() ?: error("Missing max_version")
        require(1 in minVersion..maxVersion) { "Synchronization protocol is incompatible" }
        val batchLimit = minOf(100, capabilities["max_batch"]?.jsonPrimitive?.content?.toIntOrNull() ?: 100)
        val pullLimit = minOf(500, capabilities["max_pull"]?.jsonPrimitive?.content?.toIntOrNull() ?: 500)
        require(batchLimit > 0 && pullLimit > 0)
        var outboxBatches = 0
        while (true) {
            val operations = prepareOperations(batchLimit)
            if (operations.isEmpty()) break
            val response = api.push(buildJsonObject { put("operations", JsonArray(operations)) })
            processPush(response, summary)
            summary.pushed += operations.size
            outboxBatches += 1
            require(outboxBatches < 1000) { "Synchronization outbox exceeded safety limit" }
        }
        var config = repository.syncConfiguration() ?: error("Synchronization is not configured")
        var offset = 0
        repeat(100) {
            val response = api.pull(config.cursor, offset, pullLimit)
            summary.fullResync = summary.fullResync || response["requires_full_resync"]?.jsonPrimitive?.content == "true"
            processPull(response, summary)
            val next = response.getValue("cursor").jsonPrimitive.long
            require(next >= config.cursor)
            config = config.copy(cursor = next, updatedAt = Instant.now().toString())
            repository.putSyncConfiguration(config)
            val hasMore = response["has_more"]?.jsonPrimitive?.content == "true"
            if (!hasMore) {
                summary.cursor = next
                api.ack(next)
                syncAssets(config.mediaPolicy, summary, includeOnDemandMedia)
                return summary
            }
            offset = response["snapshot_offset"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
        }
        error("Synchronization pagination exceeded safety limit")
    }

    private suspend fun prepareOperations(limit: Int): List<JsonObject> = repository.pending(limit).map { item ->
        val revision = repository.revision(item.revisionId) ?: error("Missing revision ${item.revisionId}")
        val envelope = cipher.seal(revision)
        val encoded = json.encodeToString(EncryptedEnvelope.serializer(), envelope)
        repository.prepareOutbox(item.opId, envelope.remoteId, encoded)
        buildJsonObject {
            put("op_id", item.opId)
            put("remote_id", envelope.remoteId)
            put("base_server_version", item.baseServerVersion)
            put("key_version", envelope.keyVersion)
            put("envelope", json.parseToJsonElement(encoded))
        }
    }

    private suspend fun processPush(response: JsonObject, summary: SyncSummary) {
        for (value in response["results"]?.jsonArray.orEmpty()) {
            val result = value.jsonObject
            val opId = result.getValue("op_id").jsonPrimitive.content
            val outbox = repository.outbox(opId) ?: continue
            val local = repository.revision(outbox.revisionId) ?: continue
            val remoteId = result.getValue("remote_id").jsonPrimitive.content
            val version = result.getValue("server_version").jsonPrimitive.long
            if (result.getValue("status").jsonPrimitive.content == "accepted") {
                repository.putShadow(local.shadow(remoteId, version, json))
                repository.deleteOutbox(opId)
                summary.accepted += 1
            } else {
                val envelope = json.decodeFromJsonElement(
                    EncryptedEnvelope.serializer(),
                    result.getValue("envelope"),
                )
                val remoteResult = runCatching { cipher.open(remoteId, envelope) }
                if (remoteResult.isFailure) {
                    repository.putUnknown(
                        UnknownEntity(remoteId, version, envelope.keyVersion, result.getValue("envelope").toString(), Instant.now().toString())
                    )
                    repository.markOutboxConflict(local.entityId)
                    summary.unknown += 1
                    continue
                }
                val remote = remoteResult.getOrThrow()
                merge(local, remote, remoteId, version, summary)
            }
        }
    }

    private suspend fun processPull(response: JsonObject, summary: SyncSummary) {
        for (value in response["changes"]?.jsonArray.orEmpty()) {
            val change = value.jsonObject
            val remoteId = change.getValue("remote_id").jsonPrimitive.content
            val version = change.getValue("server_version").jsonPrimitive.long
            if ((repository.shadow(remoteId)?.serverVersion ?: -1) >= version) continue
            val envelopeElement = change.getValue("envelope")
            val envelope = runCatching {
                json.decodeFromJsonElement(EncryptedEnvelope.serializer(), envelopeElement)
            }.getOrElse {
                repository.putUnknown(
                    UnknownEntity(
                        remoteId,
                        version,
                        change.getValue("key_version").jsonPrimitive.content.toInt(),
                        envelopeElement.toString(),
                        Instant.now().toString(),
                    )
                )
                summary.unknown += 1
                continue
            }
            val remote = runCatching { cipher.open(remoteId, envelope) }.getOrElse {
                repository.putUnknown(
                    UnknownEntity(remoteId, version, envelope.keyVersion, envelopeElement.toString(), Instant.now().toString())
                )
                summary.unknown += 1
                continue
            }
            val logicalLocal = logicalSibling(remote)
            if (logicalLocal != null) {
                repository.storeRevision(remote, materialize = false)
                repository.putShadow(remote.shadow(remoteId, version, json))
                val canonicalId = minOf(logicalLocal.entityId, remote.entityId)
                val logicalMerge = when (remote.entityKind) {
                    EntityKind.CHECKIN_DAY -> mergeLogicalCheckins(logicalLocal, remote, canonicalId)
                    EntityKind.DAILY_REVIEW -> mergePendingReviews(logicalLocal, remote, canonicalId)?.let { it to emptyList() }
                    else -> null
                }
                if (logicalMerge != null && logicalMerge.second.isEmpty()) {
                    commitLogicalMerge(logicalLocal, remote, logicalMerge.first, canonicalId)
                    summary.merged += 1
                } else {
                    recordLogicalConflict(
                        logicalLocal,
                        remote,
                        logicalMerge?.second?.ifEmpty { listOf("\$logical_key") }
                            ?: listOf(if (remote.entityKind == EntityKind.DAILY_REVIEW) "\$active_result" else "\$logical_key"),
                    )
                    summary.conflicts += 1
                }
                continue
            }
            if (remote.entityKind == org.mealcircuit.app.domain.EntityKind.ASSET) {
                val payload = remote.payload
                repository.putAsset(
                    ManagedAssetEntity(
                        id = remote.entityId,
                        sha256 = payload.getValue("sha256").jsonPrimitive.content,
                        mediaType = payload.getValue("media_type").jsonPrimitive.content,
                        extension = payload.getValue("extension").jsonPrimitive.content,
                        byteCount = payload.getValue("byte_count").jsonPrimitive.long,
                        relativePath = repository.asset(remote.entityId)?.relativePath,
                        unresolved = repository.asset(remote.entityId)?.relativePath == null,
                        createdAt = remote.createdAt,
                    )
                )
            }
            val pending = repository.pendingForEntity(remote.entityId)
            if (pending != null) {
                val local = repository.revision(pending.revisionId) ?: continue
                merge(local, remote, remoteId, version, summary)
            } else {
                repository.storeRevision(remote, materialize = true)
                repository.putShadow(remote.shadow(remoteId, version, json))
                summary.applied += 1
            }
        }
    }

    private suspend fun logicalSibling(remote: DomainRevision): DomainRevision? {
        val key = logicalKey(remote) ?: return null
        if (remote.deleted) return null
        val record = repository.records(remote.entityKind).firstOrNull { item ->
            item.entityId != remote.entityId && logicalKey(item.payloadJson, remote.entityKind) == key
        } ?: return null
        val head = repository.heads().firstOrNull { it.entityId == record.entityId } ?: return null
        return repository.revision(head.revisionId)?.takeUnless { it.deleted }
    }

    private fun logicalKey(revision: DomainRevision): String? = when (revision.entityKind) {
        EntityKind.CHECKIN_DAY, EntityKind.CHECKIN_DRAFT -> revision.payload["checkin"]?.jsonObject
            ?.get("checkin_date")?.jsonPrimitive?.content
        EntityKind.DAILY_REVIEW -> revision.payload["review"]?.jsonObject
            ?.get("review_date")?.jsonPrimitive?.content
        else -> null
    }

    private fun logicalKey(payload: String, kind: EntityKind): String? = runCatching {
        val value = json.parseToJsonElement(payload).jsonObject
        when (kind) {
            EntityKind.CHECKIN_DAY, EntityKind.CHECKIN_DRAFT -> value.getValue("checkin").jsonObject
                .getValue("checkin_date").jsonPrimitive.content
            EntityKind.DAILY_REVIEW -> value.getValue("review").jsonObject
                .getValue("review_date").jsonPrimitive.content
            else -> null
        }
    }.getOrNull()

    private fun mergeLogicalCheckins(
        local: DomainRevision,
        remote: DomainRevision,
        canonicalId: String,
    ): Pair<JsonObject, List<String>> {
        fun byKey(value: JsonObject) = value["modules"]?.jsonArray.orEmpty().associateBy {
            it.jsonObject.getValue("module").jsonObject.getValue("module_key").jsonPrimitive.content
        }
        fun active(value: JsonObject): Boolean {
            val module = value.getValue("module").jsonObject
            return module["status"]?.jsonPrimitive?.content != "not_started" ||
                (module["version"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0) > 0 ||
                module["answers_json"]?.jsonObject?.isNotEmpty() == true ||
                module["draft_json"]?.jsonObject?.isNotEmpty() == true
        }
        val left = byKey(local.payload)
        val right = byKey(remote.payload)
        val paths = mutableListOf<String>()
        val modules = (left.keys + right.keys).sorted().map { key ->
            val a = left[key]?.jsonObject
            val b = right[key]?.jsonObject
            val selected = when {
                a == null -> b!!
                b == null -> a
                !active(a) && active(b) -> b
                !active(b) && active(a) -> a
                !active(a) && !active(b) -> a
                else -> {
                    val am = a.getValue("module").jsonObject
                    val bm = b.getValue("module").jsonObject
                    if (am["status"] != bm["status"]) {
                        paths += "modules[$key].status"
                        a
                    } else {
                        val mergedModule = am.toMutableMap()
                        listOf("answers_json", "draft_json").forEach { field ->
                            val av = am[field] as? JsonObject ?: JsonObject(emptyMap())
                            val bv = bm[field] as? JsonObject ?: JsonObject(emptyMap())
                            val merged = threeWayMerge(JsonObject(emptyMap()), av, bv)
                            paths += merged.conflicts.map { "modules[$key].$field.$it" }
                            mergedModule[field] = merged.value
                        }
                        mergedModule["version"] = json.parseToJsonElement(
                            maxOf(
                                am["version"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                                bm["version"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                            ).toString()
                        )
                        val history = (a["history"]?.jsonArray.orEmpty() + b["history"]?.jsonArray.orEmpty())
                            .associateBy { it.jsonObject.getValue("id").jsonPrimitive.content }
                            .toSortedMap().values.toList()
                        JsonObject(a + mapOf("module" to JsonObject(mergedModule), "history" to JsonArray(history)))
                    }
                }
            }
            val module = JsonObject(selected.getValue("module").jsonObject +
                ("checkin_id" to json.parseToJsonElement(json.encodeToString(canonicalId))))
            JsonObject(selected + ("module" to module))
        }
        return JsonObject(canonicalizeLogicalPayload(local.entityKind, local.payload, canonicalId) + ("modules" to JsonArray(modules))) to paths.distinct()
    }

    private fun mergePendingReviews(
        local: DomainRevision,
        remote: DomainRevision,
        canonicalId: String,
    ): JsonObject? {
        val a = local.payload.getValue("review").jsonObject
        val b = remote.payload.getValue("review").jsonObject
        if (a["status"]?.jsonPrimitive?.content != "pending" || b["status"]?.jsonPrimitive?.content != "pending" ||
            a["result_json"]?.let { it !is kotlinx.serialization.json.JsonNull } == true ||
            b["result_json"]?.let { it !is kotlinx.serialization.json.JsonNull } == true
        ) return null
        val review = a.toMutableMap()
        review["source_record_ids_json"] = JsonArray(
            (a["source_record_ids_json"]?.jsonArray.orEmpty() + b["source_record_ids_json"]?.jsonArray.orEmpty())
                .distinctBy { it.jsonPrimitive.content }.sortedBy { it.jsonPrimitive.content }
        )
        val versions = (a["source_checkin_versions_json"] as? JsonObject).orEmpty().toMutableMap()
        (b["source_checkin_versions_json"] as? JsonObject).orEmpty().forEach { (key, value) ->
            val current = versions[key]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
            if ((value.jsonPrimitive.content.toIntOrNull() ?: 0) > current) versions[key] = value
        }
        review["source_checkin_versions_json"] = JsonObject(versions)
        val payload = canonicalizeLogicalPayload(local.entityKind, local.payload, canonicalId).toMutableMap()
        payload["review"] = JsonObject(review + ("id" to json.parseToJsonElement(json.encodeToString(canonicalId))))
        val history = (local.payload["history"]?.jsonArray.orEmpty() + remote.payload["history"]?.jsonArray.orEmpty())
            .associateBy { it.jsonObject.getValue("id").jsonPrimitive.content }.toSortedMap().values.toList()
        payload["history"] = JsonArray(history)
        return JsonObject(payload)
    }

    private suspend fun commitLogicalMerge(
        local: DomainRevision,
        remote: DomainRevision,
        payload: JsonObject,
        canonicalId: String,
    ) {
        val merged = DomainRevision.create(
            local.entityKind, canonicalId, listOf(local.revisionId, remote.revisionId),
            repository.deviceId, canonicalizeLogicalPayload(local.entityKind, payload, canonicalId),
        )
        val alias = if (local.entityId == canonicalId) remote else local
        val tombstone = DomainRevision.create(
            alias.entityKind, alias.entityId, listOf(alias.revisionId), repository.deviceId,
            alias.payload, deleted = true,
        )
        repository.commitLogicalMerge(merged, tombstone)
    }

    private suspend fun recordLogicalConflict(
        local: DomainRevision,
        remote: DomainRevision,
        paths: List<String>,
    ) {
        repository.commitSyncConflict(
            SyncConflictEntity(
                DomainRevision.id("conflict"), local.entityId, local.entityKind.serialized(), null,
                json.encodeToString(local), json.encodeToString(remote), json.encodeToString(paths),
                "unresolved", Instant.now().toString(), null,
            ),
            local.entityId,
        )
    }

    private suspend fun syncAssets(policy: String, summary: SyncSummary, includeOnDemandMedia: Boolean) {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val unmetered = !connectivity.isActiveNetworkMetered
        val uploadAllowed = policy != "all_wifi" || unmetered
        val downloadAllowed = policy == "all" || (policy == "all_wifi" && unmetered) ||
            (policy == "on_demand" && includeOnDemandMedia)
        if (uploadAllowed) repository.assets().filter { !it.unresolved && it.relativePath != null }.forEach { asset ->
            runCatching {
                val file = context.filesDir.resolve(asset.relativePath!!)
                require(file.isFile && file.length() == asset.byteCount && file.readBytes().sha256() == asset.sha256)
                val blobId = cipher.blobId(asset.id)
                val count = maxOf(1, ceil(asset.byteCount.toDouble() / BLOB_CHUNK).toInt())
                val state = api.createBlob(buildJsonObject {
                    put("blob_id", blobId); put("byte_count", asset.byteCount)
                    put("chunk_count", count); put("key_version", cipher.keyVersion)
                })
                if (state["complete"]?.jsonPrimitive?.content != "true") {
                    file.inputStream().use { input ->
                        repeat(count) { index ->
                            val plain = input.readUpTo(BLOB_CHUNK)
                            api.uploadChunk(blobId, index, cipher.sealBlobChunk(blobId, index, count, plain))
                        }
                    }
                    api.completeBlob(blobId)
                }
                summary.assetsUploaded += 1
            }.onFailure { summary.assetErrors += "${asset.id}: ${it.message}" }
        }
        if (policy == "on_demand") return
        if (downloadAllowed) repository.unresolvedAssets().forEach { asset ->
            runCatching {
                val blobId = cipher.blobId(asset.id)
                val count = maxOf(1, ceil(asset.byteCount.toDouble() / BLOB_CHUNK).toInt())
                val bytes = buildList<ByteArray> {
                    repeat(count) { index ->
                        val encrypted = api.downloadChunk(blobId, index) ?: return@runCatching
                        add(cipher.openBlobChunk(blobId, index, count, encrypted))
                    }
                }.fold(ByteArray(0)) { result, block -> result + block }
                require(bytes.size.toLong() == asset.byteCount && bytes.sha256() == asset.sha256)
                val relative = "assets/${asset.sha256}${asset.extension}"
                context.filesDir.resolve(relative).apply { parentFile?.mkdirs(); writeBytes(bytes) }
                repository.putAsset(asset.copy(relativePath = relative, unresolved = false))
                summary.assetsDownloaded += 1
            }.onFailure { summary.assetErrors += "${asset.id}: ${it.message}" }
        }
    }

    private suspend fun merge(
        local: DomainRevision,
        remote: DomainRevision,
        remoteId: String,
        serverVersion: Long,
        summary: SyncSummary,
    ) {
        repository.storeRevision(remote, materialize = false)
        val oldShadow = repository.shadowForEntity(local.entityId)
        val base = oldShadow?.let { repository.revision(it.revisionId) }
        repository.putShadow(remote.shadow(remoteId, serverVersion, json))
        val merge = base?.let { threeWayMerge(it.payload, local.payload, remote.payload) }
        val deleteEdit = base != null && local.deleted != remote.deleted &&
            ((local.deleted != base.deleted && remote.payload != base.payload) ||
                (remote.deleted != base.deleted && local.payload != base.payload))
        val paths = merge?.conflicts.orEmpty() + if (deleteEdit) listOf("\$deleted") else emptyList()
        if (base == null || paths.isNotEmpty()) {
            val now = Instant.now().toString()
            repository.commitSyncConflict(
                SyncConflictEntity(
                    id = DomainRevision.id("conflict"),
                    entityId = local.entityId,
                    entityKind = local.entityKind.serialized(),
                    baseRevisionJson = base?.let { json.encodeToString(it) },
                    localRevisionJson = json.encodeToString(local),
                    remoteRevisionJson = json.encodeToString(remote),
                    conflictingPathsJson = json.encodeToString(paths.ifEmpty { listOf("$") }),
                    status = "unresolved",
                    createdAt = now,
                    resolvedAt = null,
                ),
                local.entityId,
            )
            summary.conflicts += 1
            return
        }
        val deleted = when {
            local.deleted == remote.deleted -> local.deleted
            local.deleted == base.deleted -> remote.deleted
            else -> local.deleted
        }
        val combined = DomainRevision.create(
            kind = local.entityKind,
            entityId = local.entityId,
            parents = listOf(local.revisionId, remote.revisionId),
            deviceId = repository.deviceId,
            payload = merge!!.value,
            deleted = deleted,
        )
        repository.commitRevision(combined)
        summary.merged += 1
    }
}

private fun DomainRevision.shadow(remoteId: String, serverVersion: Long, json: Json) =
    SyncShadowEntity(
        remoteId = remoteId,
        entityId = entityId,
        serverVersion = serverVersion,
        revisionId = revisionId,
        payloadJson = json.encodeToString(payload),
        updatedAt = Instant.now().toString(),
    )

private fun ByteArray.sha256() = MessageDigest.getInstance("SHA-256").digest(this)
    .joinToString("") { "%02x".format(it) }

private const val BLOB_CHUNK = 4 * 1024 * 1024
