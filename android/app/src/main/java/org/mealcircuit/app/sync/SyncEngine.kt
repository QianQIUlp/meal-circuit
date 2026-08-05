package org.mealcircuit.app.sync

import android.content.Context
import android.net.ConnectivityManager
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
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
import org.mealcircuit.app.data.serialized
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.canonicalizeLogicalPayload
import org.mealcircuit.app.domain.threeWayMerge
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.io.MAX_MANAGED_ASSET_BYTES
import org.mealcircuit.app.io.readUpTo
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
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
    var unknownSkipped: Int = 0,
    var unknownEvicted: Int = 0,
    var cursor: Long = 0,
    var fullResync: Boolean = false,
    var assetsUploaded: Int = 0,
    var assetsDownloaded: Int = 0,
    var deferredAssetTransfer: Boolean = false,
    val assetErrors: MutableList<String> = mutableListOf(),
    var transientAssetFailures: Int = 0,
    var permanentAssetFailures: Int = 0,
)

class PermanentAssetException(message: String) : Exception(message)

class SyncEngine(
    private val repository: DomainRepository,
    private val api: SyncApi,
    private val cipher: AccountCipher,
    private val context: Context,
    private val json: Json = repository.json,
) {
    suspend fun run(includeOnDemandMedia: Boolean = false): SyncSummary = repository.withMutationGate {
        val summary = SyncSummary()
        val existingUnknown = repository.rotationReadiness().third
        require(existingUnknown <= MAX_STORED_UNKNOWN) { "无法识别的同步记录过多" }
        val existingUnknownBytes = repository.unknownByteCount()
        require(repository.unknownMaxByteCount() <= MAX_UNKNOWN_ENVELOPE_BYTES) {
            "已有未知同步记录超过单项存储上限"
        }
        require(existingUnknownBytes <= MAX_STORED_UNKNOWN_BYTES) { "未知同步记录占用空间超过安全上限" }
        val budget = SyncBudget(existingUnknown, storedBytes = existingUnknownBytes)
        reprocessUnknowns(
            summary,
            budget,
            if (existingUnknown >= MAX_STORED_UNKNOWN) MAX_STORED_UNKNOWN else MAX_UNKNOWN_REPROCESS_PER_RUN,
        )
        val pausePullForUnknowns = shouldPauseSyncForUnknownCount(budget.storedAtStart)
        val capabilities = api.capabilities()
        require(capabilities["protocol"]?.jsonPrimitive?.content == "mealcircuit.sync")
        require(capabilities["e2ee_required"]?.jsonPrimitive?.content == "true")
        val minVersion = capabilities["min_version"]?.jsonPrimitive?.content?.toIntOrNull() ?: error("同步服务缺少最低协议版本")
        val maxVersion = capabilities["max_version"]?.jsonPrimitive?.content?.toIntOrNull() ?: error("同步服务缺少最高协议版本")
        require(1 in minVersion..maxVersion) { "同步协议版本不兼容" }
        val batchLimit = minOf(100, capabilities["max_batch"]?.jsonPrimitive?.content?.toIntOrNull() ?: 100)
        require(batchLimit > 0)
        var outboxBatches = 0
        while (true) {
            val operations = prepareOperations(batchLimit)
            if (operations.isEmpty()) break
            require(outboxBatches < MAX_OUTBOX_BATCHES) { "同步待上传队列超过安全上限" }
            val response = api.push(buildJsonObject { put("operations", JsonArray(operations)) })
            processPush(response, operations, summary, budget)
            summary.pushed += operations.size
            outboxBatches += 1
        }
        var config = repository.syncConfiguration() ?: error("尚未配置同步")
        if (pausePullForUnknowns) {
            summary.unknown = budget.storedAtStart
            summary.cursor = config.cursor
            syncAssets(config.mediaPolicy, summary, includeOnDemandMedia)
            return@withMutationGate summary
        }
        val capabilityPullLimit = deriveSafePullLimit(
            serverMaxPull = capabilities["max_pull"]?.jsonPrimitive?.content?.toIntOrNull()
                ?: MAX_PULL_CHANGES,
            maxEntityBytes = capabilities["max_entity_bytes"]?.jsonPrimitive?.content?.toLongOrNull()
                ?: DEFAULT_MAX_ENTITY_BYTES,
            maxPullResponseBytes = capabilities["max_pull_response_bytes"]
                ?.jsonPrimitive?.content?.toLongOrNull() ?: MAX_SYNC_JSON_BYTES.toLong(),
        )
        val pullLimit = minOf(MAX_PULL_CHANGES, MAX_UNKNOWN_PER_RUN, capabilityPullLimit)
        require(pullLimit > 0)
        var offset = 0
        repeat(MAX_PULL_PAGES) {
            val response = api.pull(config.cursor, offset, pullLimit)
            summary.fullResync = summary.fullResync || response["requires_full_resync"]?.jsonPrimitive?.content == "true"
            processPull(response, pullLimit, summary, budget)
            val next = response.getValue("cursor").jsonPrimitive.long
            require(next >= config.cursor)
            config = config.copy(cursor = next, updatedAt = Instant.now().toString())
            repository.putSyncConfiguration(config)
            val hasMore = response["has_more"]?.jsonPrimitive?.content == "true"
            if (!hasMore) {
                summary.cursor = next
                api.ack(next)
                syncAssets(config.mediaPolicy, summary, includeOnDemandMedia)
                return@withMutationGate summary
            }
            offset = response["snapshot_offset"]?.jsonPrimitive?.content?.toIntOrNull()
                ?: error("同步快照缺少偏移量")
            require(offset >= 0) { "同步快照偏移量无效" }
        }
        error("同步分页超过安全上限")
    }

    private suspend fun prepareOperations(limit: Int): List<JsonObject> = repository.pending(limit).map { item ->
        val revision = repository.revision(item.revisionId) ?: error("缺少版本 ${item.revisionId}")
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

    private suspend fun processPush(
        response: JsonObject,
        operations: List<JsonObject>,
        summary: SyncSummary,
        budget: SyncBudget,
    ) {
        val expected = operations.associate {
            it.getValue("op_id").jsonPrimitive.content to it.getValue("remote_id").jsonPrimitive.content
        }
        require(expected.size == operations.size) { "同步操作重复" }
        val results = response["results"]?.jsonArray ?: error("同步服务缺少推送结果")
        require(results.size == expected.size) { "同步推送响应不完整" }
        val resultIds = results.map { it.jsonObject.getValue("op_id").jsonPrimitive.content }
        require(resultIds.toSet().size == resultIds.size && resultIds.toSet() == expected.keys) {
            "同步推送响应与请求不匹配"
        }
        for (value in results) {
            val result = value.jsonObject
            val opId = result.getValue("op_id").jsonPrimitive.content
            val outbox = requireNotNull(repository.outbox(opId)) { "同步待上传条目已消失" }
            val local = requireNotNull(repository.revision(outbox.revisionId)) { "同步修订不存在" }
            val remoteId = result.getValue("remote_id").jsonPrimitive.content
            val version = result.getValue("server_version").jsonPrimitive.long
            require(remoteId == expected.getValue(opId) && REMOTE_ID.matches(remoteId) && version > 0)
            when (result.getValue("status").jsonPrimitive.content) {
                "accepted" -> {
                repository.putShadow(local.shadow(remoteId, version, json))
                repository.deleteOutbox(opId)
                summary.accepted += 1
                }
                "conflict" -> {
                val envelope = json.decodeFromJsonElement(
                    EncryptedEnvelope.serializer(),
                    result.getValue("envelope"),
                )
                val remoteResult = runCatching { cipher.open(remoteId, envelope) }
                if (remoteResult.isFailure) {
                    putUnknown(
                        UnknownEntity(
                            remoteId,
                            version,
                            envelope.keyVersion,
                            result.getValue("envelope").toString(),
                            Instant.now().toString(),
                        ),
                        summary,
                        budget,
                    )
                    repository.markOutboxConflict(local.entityId)
                    continue
                }
                val remote = remoteResult.getOrThrow()
                if (local.sameContent(remote)) {
                    repository.storeRevision(remote, materialize = false)
                    repository.putShadow(remote.shadow(remoteId, version, json))
                    repository.deleteOutbox(opId)
                    summary.merged += 1
                } else {
                    merge(local, remote, remoteId, version, summary)
                }
                }
                else -> error("同步服务返回了不支持的推送状态")
            }
        }
    }

    private suspend fun putUnknown(value: UnknownEntity, summary: SyncSummary, budget: SyncBudget): Boolean {
        val existing = repository.unknown(value.remoteId)
        val encodedBytes = value.encryptedEnvelope.utf8ByteCount()
        require(encodedBytes <= MAX_UNKNOWN_ENVELOPE_BYTES) { "未知同步记录超过单项安全上限" }
        if (existing != null && existing.serverVersion >= value.serverVersion) {
            summary.unknown += 1
            return true
        }
        if (!budget.tryReserve(
                isNew = existing == null,
                previousBytes = existing?.encryptedEnvelope?.utf8ByteCount() ?: 0,
                newBytes = encodedBytes,
            )
        ) {
            summary.unknownSkipped += 1
            return false
        }
        repository.putUnknown(value)
        summary.unknown += 1
        return true
    }

    private suspend fun reprocessUnknowns(summary: SyncSummary, budget: SyncBudget, limit: Int) {
        repository.unknownEntities(limit).forEach { unknown ->
            val envelopeElement = runCatching { json.parseToJsonElement(unknown.encryptedEnvelope) }.getOrNull()
            val envelope = envelopeElement?.let {
                runCatching { json.decodeFromJsonElement(EncryptedEnvelope.serializer(), it) }.getOrNull()
            }
            if (envelopeElement == null || envelope == null ||
                runCatching { cipher.open(unknown.remoteId, envelope) }.isFailure
            ) {
                val attempts = unknown.reprocessAttempts + 1
                if (shouldEvictUnknown(attempts)) {
                    repository.deleteUnknown(unknown.remoteId)
                    budget.release(unknown.encryptedEnvelope.utf8ByteCount())
                    summary.unknownEvicted += 1
                } else {
                    repository.putUnknown(
                        unknown.copy(reprocessAttempts = attempts, updatedAt = Instant.now().toString())
                    )
                }
                return@forEach
            }
            processPull(
                buildJsonObject {
                    put("changes", JsonArray(listOf(buildJsonObject {
                        put("remote_id", unknown.remoteId)
                        put("server_version", unknown.serverVersion)
                        put("key_version", unknown.keyVersion)
                        put("envelope", envelopeElement)
                    })))
                },
                requestedLimit = 1,
                summary = summary,
                budget = budget,
            )
            repository.deleteUnknown(unknown.remoteId)
            budget.release(unknown.encryptedEnvelope.utf8ByteCount())
        }
    }

    private suspend fun processPull(
        response: JsonObject,
        requestedLimit: Int,
        summary: SyncSummary,
        budget: SyncBudget,
    ) {
        val changes = response["changes"]?.jsonArray ?: JsonArray(emptyList())
        require(changes.size <= requestedLimit && changes.size <= MAX_PULL_CHANGES) {
            "同步拉取响应超过请求上限"
        }
        for (value in changes) {
            val change = value.jsonObject
            val remoteId = change.getValue("remote_id").jsonPrimitive.content
            val version = change.getValue("server_version").jsonPrimitive.long
            require(REMOTE_ID.matches(remoteId) && version > 0) { "同步变更标识无效" }
            if ((repository.shadow(remoteId)?.serverVersion ?: -1) >= version) continue
            val envelopeElement = change.getValue("envelope")
            val envelope = runCatching {
                json.decodeFromJsonElement(EncryptedEnvelope.serializer(), envelopeElement)
            }.getOrElse {
                putUnknown(
                    UnknownEntity(
                        remoteId,
                        version,
                        change["key_version"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                        envelopeElement.toString(),
                        Instant.now().toString(),
                    ),
                    summary,
                    budget,
                )
                continue
            }
            val remote = runCatching { cipher.open(remoteId, envelope) }.getOrElse {
                putUnknown(
                    UnknownEntity(remoteId, version, envelope.keyVersion, envelopeElement.toString(), Instant.now().toString()),
                    summary,
                    budget,
                )
                continue
            }
            val currentHead = repository.heads().firstOrNull { it.entityId == remote.entityId }
            val current = currentHead?.let { repository.revision(it.revisionId) }
            if (current != null && current.entityKind != remote.entityKind) {
                recordAncestryConflict(current, remote, remoteId, version, "\$entity_kind", summary)
                continue
            }
            if (currentHead?.conflicted == true) {
                repository.storeRevision(remote, materialize = false)
                summary.conflicts += 1
                continue
            }
            if (current?.sameContent(remote) == true) {
                repository.commitRemoteRevision(
                    remote,
                    remote.shadow(remoteId, version, json),
                    managedAssetForRevision(remote),
                    materialize = false,
                )
                repository.pendingForEntity(remote.entityId)?.let { pending ->
                    if (pending.state != "conflict") repository.deleteOutbox(pending.opId)
                }
                summary.merged += 1
                continue
            }
            val logicalSibling = logicalSibling(remote)
            if (logicalSibling?.second == true) {
                repository.storeRevision(remote, materialize = false)
                summary.conflicts += 1
                continue
            }
            val logicalLocal = logicalSibling?.first
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
            val pending = repository.pendingForEntity(remote.entityId)
            if (pending == null && current != null && !isRevisionDescendant(
                    candidate = remote,
                    ancestor = current,
                    loadRevision = repository::revision,
                )
            ) {
                recordAncestryConflict(current, remote, remoteId, version, "\$ancestry", summary)
                continue
            }
            if (pending != null) {
                val local = repository.revision(pending.revisionId) ?: continue
                merge(local, remote, remoteId, version, summary)
            } else {
                repository.commitRemoteRevision(
                    remote,
                    remote.shadow(remoteId, version, json),
                    managedAssetForRevision(remote),
                )
                summary.applied += 1
            }
        }
    }

    private suspend fun logicalSibling(remote: DomainRevision): Pair<DomainRevision, Boolean>? {
        val key = logicalKey(remote) ?: return null
        if (remote.deleted) return null
        val record = repository.records(remote.entityKind).firstOrNull { item ->
            item.entityId != remote.entityId && logicalKey(item.payloadJson, remote.entityKind) == key
        } ?: return null
        val head = repository.heads().firstOrNull { it.entityId == record.entityId } ?: return null
        return repository.revision(head.revisionId)?.takeUnless { it.deleted }?.let { it to head.conflicted }
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

    private suspend fun recordAncestryConflict(
        local: DomainRevision,
        remote: DomainRevision,
        remoteId: String,
        serverVersion: Long,
        conflictPath: String,
        summary: SyncSummary,
    ) {
        repository.storeRevision(remote, materialize = false)
        repository.commitSyncConflict(
            SyncConflictEntity(
                id = DomainRevision.id("conflict"),
                entityId = local.entityId,
                entityKind = local.entityKind.serialized(),
                baseRevisionJson = null,
                localRevisionJson = json.encodeToString(local),
                remoteRevisionJson = json.encodeToString(remote),
                conflictingPathsJson = json.encodeToString(listOf(conflictPath)),
                status = "unresolved",
                createdAt = Instant.now().toString(),
                resolvedAt = null,
            ),
            local.entityId,
            remote.shadow(remoteId, serverVersion, json),
        )
        summary.conflicts += 1
    }

    private suspend fun syncAssets(policy: String, summary: SyncSummary, includeOnDemandMedia: Boolean) {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val unmetered = !connectivity.isActiveNetworkMetered
        val uploadAllowed = policy != "all_wifi" || unmetered
        val downloadAllowed = allowsAssetDownload(policy, unmetered, includeOnDemandMedia)
        repository.headRevisions()
            .filter { it.entityKind == EntityKind.ASSET }
            .forEach { revision ->
                repository.putAsset(requireNotNull(managedAssetForRevision(revision)))
            }
        val reachability = classifyManagedAssetsByReachability(
            repository.assets(),
            referencedAssetIds(repository.revisions()),
        )
        val selectedAssets = if (includeOnDemandMedia) {
            reachability.allForKeyRotation
        } else {
            reachability.referenced
        }
        val reachableAssets = selectedAssets.map { asset ->
            if (asset.unresolved || asset.relativePath == null) return@map asset
            val valid = runCatching {
                val file = localAssetFile(asset)
                file.isFile && file.length() == asset.byteCount && file.sha256() == asset.sha256
            }.getOrDefault(false)
            if (valid) {
                asset
            } else {
                val unresolved = asset.copy(relativePath = null, unresolved = true)
                repository.putAsset(unresolved)
                unresolved
            }
        }
        val uploadCandidates = reachableAssets.filter { !it.unresolved && it.relativePath != null }
        val downloadCandidates = reachableAssets.filter { it.unresolved }
        summary.deferredAssetTransfer = shouldDeferAssetTransfer(
            policy,
            unmetered,
            uploadCandidates.isNotEmpty() || downloadCandidates.isNotEmpty(),
        )
        if (uploadAllowed) summary.recordAssetFailures(safelyProcessAssets(
            uploadCandidates,
            summary.assetErrors,
        ) { asset ->
            val file = localAssetFile(asset)
            require(file.isFile && file.length() == asset.byteCount && file.sha256() == asset.sha256) {
                "本地资源 ${asset.id} 与已验证元数据不一致"
            }
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
        })
        if (downloadAllowed) summary.recordAssetFailures(safelyProcessAssets(
            downloadCandidates,
            summary.assetErrors,
        ) { asset ->
            var temporary: File? = null
            try {
                val target = expectedAssetFile(asset)
                val root = requireNotNull(target.parentFile)
                val partFile = File.createTempFile(".${asset.sha256}.", ".part", root)
                temporary = partFile
                val blobId = cipher.blobId(asset.id)
                val count = maxOf(1, ceil(asset.byteCount.toDouble() / BLOB_CHUNK).toInt())
                val digest = MessageDigest.getInstance("SHA-256")
                var written = 0L
                FileOutputStream(partFile).use { output ->
                    repeat(count) { index ->
                        val encrypted = api.downloadChunk(blobId, index)
                            ?: throw PermanentAssetException("资源 ${asset.id} 缺少分块 $index")
                        val plain = cipher.openBlobChunk(blobId, index, count, encrypted)
                        written += plain.size
                        if (written > asset.byteCount) {
                            throw PermanentAssetException("资源 ${asset.id} 超过声明的字节数")
                        }
                        output.write(plain)
                        digest.update(plain)
                    }
                    output.fd.sync()
                }
                if (written != asset.byteCount || digest.digest().hex() != asset.sha256) {
                    throw PermanentAssetException("资源 ${asset.id} 与认证元数据不一致")
                }
                Files.move(
                    partFile.toPath(),
                    target.toPath(),
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING,
                )
                repository.putAsset(
                    asset.copy(relativePath = "assets/${target.name}", unresolved = false)
                )
                summary.assetsDownloaded += 1
            } finally {
                temporary?.delete()
            }
        })
    }

    private suspend fun managedAssetForRevision(revision: DomainRevision): ManagedAssetEntity? {
        if (revision.entityKind != EntityKind.ASSET) return null
        val payload = revision.payload
        val sha256 = payload.getValue("sha256").jsonPrimitive.content
        val mediaType = payload.getValue("media_type").jsonPrimitive.content
        val extension = payload.getValue("extension").jsonPrimitive.content
        val byteCount = payload.getValue("byte_count").jsonPrimitive.long
        val existing = repository.asset(revision.entityId)
        val reusablePath = existing?.takeIf {
            it.sha256 == sha256 && it.mediaType == mediaType && it.extension == extension &&
                it.byteCount == byteCount && it.relativePath != null && runCatching {
                    val file = localAssetFile(it)
                    file.isFile && file.length() == it.byteCount && file.sha256() == it.sha256
                }.getOrDefault(false)
        }?.relativePath
        return ManagedAssetEntity(
            id = revision.entityId,
            sha256 = sha256,
            mediaType = mediaType,
            extension = extension,
            byteCount = byteCount,
            relativePath = reusablePath,
            unresolved = reusablePath == null,
            createdAt = revision.createdAt,
        )
    }

    private fun localAssetFile(asset: ManagedAssetEntity): File {
        val expected = expectedAssetFile(asset)
        val configured = File(context.filesDir, requireNotNull(asset.relativePath)).canonicalFile
        require(configured == expected) { "资源路径不在受管位置内" }
        return configured
    }

    private fun expectedAssetFile(asset: ManagedAssetEntity): File {
        validateAssetMetadata(asset)
        val filesRoot = context.filesDir.canonicalFile
        val root = File(filesRoot, "assets").apply { mkdirs() }.canonicalFile
        require(root.isDirectory && root.parentFile == filesRoot) { "受管资源目录不可用" }
        val target = File(root, "${asset.sha256}${asset.extension}").canonicalFile
        require(target.parentFile == root) { "资源路径逃逸受管目录" }
        return target
    }

    private fun validateAssetMetadata(asset: ManagedAssetEntity) {
        require(ASSET_SHA256.matches(asset.sha256))
        require(asset.byteCount in 0L..MAX_MANAGED_ASSET_BYTES.toLong())
        require(asset.extension in ASSET_MEDIA_EXTENSIONS[asset.mediaType].orEmpty()) {
            "资源扩展名与媒体类型不一致"
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
        repository.commitRevision(combined, managedAsset = managedAssetForRevision(combined))
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

private fun DomainRevision.sameContent(other: DomainRevision): Boolean =
    entityId == other.entityId && entityKind == other.entityKind && deleted == other.deleted && payload == other.payload

private fun File.sha256(): String {
    val digest = MessageDigest.getInstance("SHA-256")
    inputStream().use { input ->
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            digest.update(buffer, 0, count)
        }
    }
    return digest.digest().hex()
}

internal fun deriveSafePullLimit(
    serverMaxPull: Int,
    maxEntityBytes: Long,
    maxPullResponseBytes: Long,
): Int {
    require(serverMaxPull > 0 && maxEntityBytes > 0 && maxPullResponseBytes > 0) {
        "同步服务分页能力无效"
    }
    val responseBudget = minOf(maxPullResponseBytes, MAX_SYNC_JSON_BYTES.toLong())
    val usableBytes = responseBudget - PULL_RESPONSE_FIXED_OVERHEAD_BYTES
    require(
        usableBytes >= PULL_CHANGE_OVERHEAD_BYTES &&
            maxEntityBytes <= usableBytes - PULL_CHANGE_OVERHEAD_BYTES
    ) {
        "同步服务响应预算不足以容纳单条记录"
    }
    val byteBound = usableBytes / (maxEntityBytes + PULL_CHANGE_OVERHEAD_BYTES)
    return minOf(serverMaxPull.toLong(), byteBound).coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
}

private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }

internal suspend fun isRevisionDescendant(
    candidate: DomainRevision,
    ancestor: DomainRevision,
    loadRevision: suspend (String) -> DomainRevision?,
    maxVisited: Int = MAX_ANCESTRY_REVISIONS,
): Boolean {
    require(maxVisited > 0)
    if (candidate.entityId != ancestor.entityId || candidate.entityKind != ancestor.entityKind) return false
    if (candidate.revisionId == ancestor.revisionId) return true
    val pending = ArrayDeque<String>()
    candidate.parentRevisionIds.forEach { parent ->
        if (pending.size < maxVisited) pending.addLast(parent)
    }
    val visited = mutableSetOf<String>()
    while (pending.isNotEmpty() && visited.size < maxVisited) {
        val revisionId = pending.removeFirst()
        if (revisionId == ancestor.revisionId) return true
        if (!visited.add(revisionId)) continue
        val revision = loadRevision(revisionId) ?: continue
        if (revision.entityId != candidate.entityId || revision.entityKind != candidate.entityKind) continue
        revision.parentRevisionIds.forEach { parent ->
            if (parent !in visited && pending.size + visited.size < maxVisited) pending.addLast(parent)
        }
    }
    return false
}

internal fun allowsAssetDownload(policy: String, unmetered: Boolean, includeOnDemandMedia: Boolean): Boolean =
    includeOnDemandMedia || policy == "all" || (policy == "all_wifi" && unmetered)

internal fun shouldDeferAssetTransfer(policy: String, unmetered: Boolean, hasPendingAssets: Boolean): Boolean =
    policy == "all_wifi" && !unmetered && hasPendingAssets

internal data class AssetProcessingResult(
    val transientFailures: Int = 0,
    val permanentFailures: Int = 0,
)

internal suspend fun safelyProcessAssets(
    assets: Iterable<ManagedAssetEntity>,
    errors: MutableList<String>,
    operation: suspend (ManagedAssetEntity) -> Unit,
): AssetProcessingResult {
    var transientFailures = 0
    var permanentFailures = 0
    for (asset in assets) {
        try {
            operation(asset)
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            errors += assetError(asset.id, error)
            when (syncFailureDisposition(error)) {
                SyncFailureDisposition.RETRY -> transientFailures += 1
                SyncFailureDisposition.FAILURE -> permanentFailures += 1
            }
        }
    }
    return AssetProcessingResult(transientFailures, permanentFailures)
}

private fun assetError(assetId: String, @Suppress("UNUSED_PARAMETER") error: Exception): String =
    "$assetId: 照片资源同步失败"

private fun SyncSummary.recordAssetFailures(result: AssetProcessingResult) {
    transientAssetFailures += result.transientFailures
    permanentAssetFailures += result.permanentFailures
}

internal data class SyncBudget(
    var storedAtStart: Int,
    var addedThisRun: Int = 0,
    var storedBytes: Long = 0,
    var addedBytesThisRun: Long = 0,
) {
    fun reserve(isNew: Boolean) {
        check(tryReserve(isNew)) { "未知同步记录的存储数量已达上限" }
    }

    fun tryReserve(isNew: Boolean): Boolean {
        if (!isNew) return true
        if (addedThisRun >= MAX_UNKNOWN_PER_RUN) return false
        if (storedAtStart + addedThisRun >= MAX_STORED_UNKNOWN) return false
        addedThisRun += 1
        return true
    }

    fun reserve(isNew: Boolean, previousBytes: Int, newBytes: Int) {
        check(tryReserve(isNew, previousBytes, newBytes)) { "未知同步记录的存储空间已达上限" }
    }

    fun tryReserve(isNew: Boolean, previousBytes: Int, newBytes: Int): Boolean {
        require(previousBytes >= 0 && newBytes in 0..MAX_UNKNOWN_ENVELOPE_BYTES)
        if (addedBytesThisRun + newBytes > MAX_UNKNOWN_BYTES_PER_RUN) return false
        val updatedStoredBytes = storedBytes - previousBytes + newBytes
        if (updatedStoredBytes !in 0L..MAX_STORED_UNKNOWN_BYTES) return false
        if (!tryReserve(isNew)) return false
        addedBytesThisRun += newBytes
        storedBytes = updatedStoredBytes
        return true
    }

    fun release(bytes: Int) {
        require(bytes >= 0)
        storedAtStart = (storedAtStart - 1).coerceAtLeast(0)
        storedBytes = (storedBytes - bytes).coerceAtLeast(0)
    }
}

internal fun shouldPauseSyncForUnknownCount(count: Int): Boolean = count >= MAX_STORED_UNKNOWN

internal fun shouldEvictUnknown(reprocessAttempts: Int): Boolean =
    reprocessAttempts >= MAX_UNKNOWN_REPROCESS_ATTEMPTS

private const val BLOB_CHUNK = 4 * 1024 * 1024
private const val MAX_OUTBOX_BATCHES = 1_000
// Preserve the previous 50,000-change per-run ceiling even when a server allows
// only one maximum-sized encrypted entity per byte-bounded response page.
private const val MAX_PULL_PAGES = 50_000
private const val MAX_PULL_CHANGES = 500
private const val DEFAULT_MAX_ENTITY_BYTES = 1024L * 1024L
private const val PULL_RESPONSE_FIXED_OVERHEAD_BYTES = 4L * 1024L
private const val PULL_CHANGE_OVERHEAD_BYTES = 1024L
private const val MAX_UNKNOWN_PER_RUN = 500
private const val MAX_UNKNOWN_REPROCESS_PER_RUN = 500
private const val MAX_UNKNOWN_REPROCESS_ATTEMPTS = 10
private const val MAX_STORED_UNKNOWN = 2_000
private const val MAX_UNKNOWN_ENVELOPE_BYTES = 16 * 1024 * 1024
private const val MAX_UNKNOWN_BYTES_PER_RUN = 32L * 1024L * 1024L
private const val MAX_STORED_UNKNOWN_BYTES = 64L * 1024L * 1024L
private const val MAX_ANCESTRY_REVISIONS = 1_024
private val REMOTE_ID = Regex("^[0-9a-f]{64}$")
private val ASSET_SHA256 = Regex("^[0-9a-f]{64}$")
private val ASSET_MEDIA_EXTENSIONS = mapOf(
    "image/jpeg" to setOf(".jpg", ".jpeg"),
    "image/png" to setOf(".png"),
    "image/gif" to setOf(".gif"),
    "image/webp" to setOf(".webp"),
)

private fun String.utf8ByteCount(): Int = encodeToByteArray().size
