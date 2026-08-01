package org.mealcircuit.app.sync

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.data.SyncConfigurationEntity
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.io.MAX_MANAGED_ASSET_BYTES
import org.mealcircuit.app.io.readUpTo
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import kotlin.math.ceil

private const val ROTATION_DATA_KEY = "sync.rotation.account_data_key"
private const val ROTATION_RECOVERY_KEY = "sync.rotation.recovery_key"
private const val ROTATION_MATERIAL = "sync.rotation.material"
private const val ROTATION_VERSION = "sync.rotation.key_version"
private const val ROTATION_MARKER = "sync.rotation.marker"
private const val ROTATION_CHUNK = 4 * 1024 * 1024
private const val ROTATION_MARKER_FORMAT = "mealcircuit.key-rotation"
private const val ROTATION_MARKER_VERSION = 1
private const val ROTATION_STAGE_BEGIN_PENDING = "begin_pending"
private const val ROTATION_STAGE_STAGED = "staged"
private const val ROTATION_STAGE_COMMIT_PENDING = "commit_pending"
private const val ROTATION_STAGE_REMOTE_COMMITTED = "remote_committed"

class KeyRotationManager(
    private val context: Context,
    private val repository: DomainRepository,
    private val vault: SecretVault,
) {
    private val json = repository.json

    suspend fun prepare(): String = repository.withMutationGate { prepareLocked() }

    private suspend fun prepareLocked(): String {
        val config = requireNotNull(repository.syncConfiguration()).also { require(it.enabled) }
        val binding = config.rotationBinding()
        val api = SyncApi(binding.serverUrl, vault)
        var remote = api.authorized("/v1/key-rotations/current").rotationState()
        var local = readStaging(binding)
        local = migrateLegacyStagingIfSafe(local, binding, remote)
        when (local) {
            is RotationStagingState.Complete -> {
                val staging = local.staging
                if (remote.inProgress) {
                    require(remote.ownedByCurrentDevice) { "另一台设备正在执行安全轮换" }
                    require(remote.targetKeyVersion == staging.targetKeyVersion) {
                        "服务端轮换锁与本机暂存版本不一致"
                    }
                    return staging.recoveryKey
                }
                if (remote.activeKeyVersion == staging.targetKeyVersion) return staging.recoveryKey
                require(rotationStagingCanBeCleared(remote.activeKeyVersion, config.keyVersion)) {
                    "服务端密钥版本已变化；已保留本地轮换恢复材料"
                }
                clearStaging()
            }
            is RotationStagingState.Invalid -> {
                if (remote.inProgress) {
                    require(remote.ownedByCurrentDevice) { "另一台设备正在执行安全轮换" }
                    require(
                        local.targetKeyVersion == null ||
                            local.targetKeyVersion == remote.targetKeyVersion
                    ) { "服务端轮换锁与损坏的本机暂存版本不一致" }
                    // DELETE must complete before the marker is cleared. If the network result is
                    // uncertain, the exception leaves all local evidence in place for the retry.
                    api.authorized("/v1/key-rotations/current", "DELETE")
                    clearStaging()
                    remote = api.authorized("/v1/key-rotations/current").rotationState()
                } else {
                    require(rotationStagingCanBeCleared(remote.activeKeyVersion, config.keyVersion)) {
                        "服务端可能已提交密钥轮换；已保留损坏的本地暂存以供恢复"
                    }
                    clearStaging()
                }
            }
            RotationStagingState.Absent -> {
                if (remote.inProgress) {
                    require(remote.ownedByCurrentDevice) { "另一台设备正在执行安全轮换" }
                    api.authorized("/v1/key-rotations/current", "DELETE")
                    clearStaging()
                    remote = api.authorized("/v1/key-rotations/current").rotationState()
                }
                require(rotationStagingCanBeCleared(remote.activeKeyVersion, config.keyVersion)) {
                    "服务端密钥版本领先于本机且没有可用恢复材料"
                }
            }
        }
        require(!remote.inProgress && remote.activeKeyVersion == config.keyVersion) {
            "服务端密钥轮换状态尚未恢复一致"
        }
        val oldKey = requireNotNull(vault.get("sync.account_data_key")) { "同步尚未解锁" }
        SyncEngine(
            repository,
            api,
            AccountCipher(requireNotNull(config.accountId), oldKey, config.keyVersion),
            context,
        ).run(includeOnDemandMedia = true)
        val readiness = repository.rotationReadiness()
        require(readiness == Triple(0, 0, 0)) { "轮换前必须清空待上传、冲突和未知 schema 实体" }
        requireCompleteAssetInventory()
        val pendingMarker = RotationMarker(
            format = ROTATION_MARKER_FORMAT,
            version = ROTATION_MARKER_VERSION,
            accountId = binding.accountId,
            deviceId = binding.deviceId,
            serverUrl = binding.serverUrl,
            stage = ROTATION_STAGE_BEGIN_PENDING,
        )
        // This durable marker must precede the remote begin call. A process death or ambiguous
        // network failure therefore remains distinguishable from an untouched local state.
        vault.put(ROTATION_MARKER, json.encodeToString(pendingMarker).toByteArray())
        val begun = api.authorized("/v1/key-rotations", "POST", JsonObject(emptyMap()))
        val target = begun.getValue("target_key_version").jsonPrimitive.content.toInt()
        require(target > config.keyVersion) { "同步服务没有返回有效轮换版本" }
        val material = createRecoveryMaterial(binding.accountId, target)
        val stagedMarker = pendingMarker.copy(
            stage = ROTATION_STAGE_STAGED,
            targetKeyVersion = target,
        )
        // SharedPreferences.commit() makes this five-value transition atomic in SecretVault.
        vault.putAll(
            mapOf(
                ROTATION_MARKER to json.encodeToString(stagedMarker).toByteArray(),
                ROTATION_DATA_KEY to material.accountDataKey,
                ROTATION_VERSION to target.toString().toByteArray(),
                ROTATION_MATERIAL to json.encodeToString(material).toByteArray(),
                ROTATION_RECOVERY_KEY to material.recoveryKey.toByteArray(),
            )
        )
        return material.recoveryKey
    }

    fun pendingRecovery(): String? =
        (readStaging() as? RotationStagingState.Complete)?.staging?.recoveryKey

    suspend fun confirm(typedRecoveryKey: String): JsonObject =
        repository.withMutationGate { confirmLocked(typedRecoveryKey) }

    private suspend fun confirmLocked(typedRecoveryKey: String): JsonObject {
        val config = requireNotNull(repository.syncConfiguration()).also { require(it.enabled) }
        val binding = config.rotationBinding()
        val staging = (readStaging(binding) as? RotationStagingState.Complete)?.staging
            ?: error("没有完整且属于当前账户与设备的待确认轮换")
        val recovery = staging.recoveryKey
        require(typedRecoveryKey.trim().uppercase() == recovery) { "新恢复密钥确认失败" }
        val accountId = binding.accountId
        val target = staging.targetKeyVersion
        val material = staging.material
        val dataKey = staging.accountDataKey
        require(config.keyVersion <= target) { "本地同步密钥版本高于暂存轮换版本" }
        val cipher = AccountCipher(accountId, dataKey, target)
        val api = SyncApi(binding.serverUrl, vault)
        val state = api.authorized("/v1/key-rotations/current").rotationState()
        val inProgress = state.inProgress
        if (inProgress) {
            require(state.ownedByCurrentDevice && state.targetKeyVersion == target) {
                "服务端轮换锁不属于当前设备或版本不一致"
            }
        } else {
            require(state.activeKeyVersion == target) { "服务端轮换状态与本机暂存密钥不一致" }
            updateMarker(staging, ROTATION_STAGE_REMOTE_COMMITTED)
        }
        val localAlreadyActivated = config.keyVersion == target
        val committed: JsonObject
        if (localAlreadyActivated) {
            require(!inProgress) { "本地激活后服务端轮换仍未完成" }
            require(state.activeKeyVersion == target)
            require(vault.get("sync.account_data_key")?.contentEquals(dataKey) == true) {
                "本地活动密钥与已提交的密钥版本不匹配"
            }
            committed = buildJsonObject {
                put("active_key_version", target)
                put("already_committed", true)
            }
        } else {
            if (!inProgress) discardRecoverableRotationUnknowns(cipher, target)
            val readiness = repository.rotationReadiness()
            require(readiness.second == 0 && readiness.third == 0) {
                "存在未解决冲突或未知记录，无法继续密钥轮换"
            }
            val revisions = repository.headRevisions()
            val assets = requireCompleteAssetInventory()
            // Also upload the current snapshot after a completed remote commit. That is the
            // recovery path for a process death before local key activation.
            pushRevisions(api, cipher, revisions)
            uploadAssets(api, cipher, assets)
            committed = if (inProgress) {
                updateMarker(staging, ROTATION_STAGE_COMMIT_PENDING)
                val result = api.authorized(
                    "/v1/key-rotations/current/commit",
                    "POST",
                    buildJsonObject {
                        put("key_version", target)
                        put("recovery_envelope", buildJsonObject {
                            put("version", 1)
                            put("key_version", target)
                            put("nonce", material.envelopeNonce)
                            put("ciphertext", material.envelopeCiphertext)
                        })
                        put("entity_count", revisions.size)
                        put("blob_count", assets.size)
                    },
                )
                updateMarker(staging, ROTATION_STAGE_REMOTE_COMMITTED)
                result
            } else {
                buildJsonObject {
                    put("active_key_version", target)
                    put("already_committed", true)
                }
            }
            // Keep staging until both local stores agree. If confirm is retried after this point,
            // finalizeKeyRotation must not run again because it clears synchronization state.
            vault.put("sync.account_data_key", dataKey)
            repository.finalizeKeyRotation(target)
        }
        val summary = SyncEngine(repository, api, cipher, context).run()
        clearStaging()
        return buildJsonObject {
            put("key_version", target)
            put("other_devices_revoked", committed["revoked_devices"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0)
            put("cursor", summary.cursor)
            put("completed_at", Instant.now().toString())
        }
    }

    suspend fun abort() = repository.withMutationGate { abortLocked() }

    private suspend fun abortLocked() {
        val initial = readStaging()
        val config = repository.syncConfiguration()
        if (config == null || !config.enabled || config.serverUrl == null) {
            if (initial === RotationStagingState.Absent) {
                clearStaging()
                return
            }
            error("同步凭据不可用；已保留本地轮换暂存")
        }
        val binding = config.rotationBinding()
        val api = SyncApi(binding.serverUrl, vault)
        val state = api.authorized("/v1/key-rotations/current").rotationState()
        val local = migrateLegacyStagingIfSafe(readStaging(binding), binding, state)
        if (state.inProgress) {
            require(state.ownedByCurrentDevice) { "不能中止另一台设备发起的轮换" }
            val target = when (local) {
                is RotationStagingState.Complete -> local.staging.targetKeyVersion
                is RotationStagingState.Invalid -> local.targetKeyVersion
                RotationStagingState.Absent -> null
            }
            require(target == null || target == state.targetKeyVersion) {
                "服务端轮换锁与本机暂存版本不一致"
            }
            api.authorized("/v1/key-rotations/current", "DELETE")
        } else {
            val target = when (local) {
                is RotationStagingState.Complete -> local.staging.targetKeyVersion
                is RotationStagingState.Invalid -> local.targetKeyVersion
                RotationStagingState.Absent -> null
            }
            require(
                rotationStagingCanBeCleared(state.activeKeyVersion, config.keyVersion)
            ) { "服务端已提交本次轮换；请确认轮换以完成本地恢复" }
        }
        // A failed/ambiguous status or DELETE request exits before this point and preserves all
        // evidence. Clearing is safe only after a confirmed abort or an unchanged active epoch.
        clearStaging()
    }

    private suspend fun pushRevisions(api: SyncApi, cipher: AccountCipher, revisions: List<DomainRevision>) {
        val limit = minOf(100, api.capabilities()["max_batch"]?.jsonPrimitive?.content?.toIntOrNull() ?: 100)
        require(limit in 1..100) { "密钥轮换批次上限无效" }
        revisions.chunked(limit).forEach { batch ->
            val byOperation = linkedMapOf<String, DomainRevision>()
            val remoteByOperation = linkedMapOf<String, String>()
            val operations = batch.map { revision ->
                val envelope = cipher.seal(revision)
                val opId = operationId(cipher.keyVersion, revision.revisionId, 0)
                byOperation[opId] = revision
                remoteByOperation[opId] = envelope.remoteId
                buildJsonObject {
                    put("op_id", opId); put("remote_id", envelope.remoteId)
                    put("base_server_version", 0); put("key_version", cipher.keyVersion)
                    put("envelope", json.encodeToJsonElement(EncryptedEnvelope.serializer(), envelope))
                }
            }
            val result = api.push(buildJsonObject { put("operations", JsonArray(operations)) })
            val replacements = mutableListOf<JsonObject>()
            val results = result.getValue("results").jsonArray
            require(results.size == byOperation.size)
            val resultIds = results.map { it.jsonObject.getValue("op_id").jsonPrimitive.content }
            require(resultIds.toSet().size == resultIds.size && resultIds.toSet() == byOperation.keys) {
                "密钥轮换推送响应与请求不匹配"
            }
            results.forEach { element ->
                val item = element.jsonObject
                val opId = item.getValue("op_id").jsonPrimitive.content
                val status = item.getValue("status").jsonPrimitive.content
                require(status == "accepted" || status == "conflict")
                val local = requireNotNull(byOperation[opId])
                val remoteId = item.getValue("remote_id").jsonPrimitive.content
                require(remoteId == remoteByOperation.getValue(opId) && ROTATION_REMOTE_ID.matches(remoteId))
                if (status == "accepted") return@forEach
                val remoteEnvelope = json.decodeFromJsonElement<EncryptedEnvelope>(item.getValue("envelope"))
                val remote = cipher.open(remoteId, remoteEnvelope)
                if (remote.revisionId == local.revisionId || local.sameContent(remote)) return@forEach
                val version = item.getValue("server_version").jsonPrimitive.content.toLong()
                require(version > 0)
                val envelope = cipher.seal(local)
                replacements += buildJsonObject {
                    put("op_id", operationId(cipher.keyVersion, local.revisionId, version))
                    put("remote_id", envelope.remoteId); put("base_server_version", version)
                    put("key_version", cipher.keyVersion)
                    put("envelope", json.encodeToJsonElement(EncryptedEnvelope.serializer(), envelope))
                }
            }
            if (replacements.isNotEmpty()) {
                val retried = api.push(buildJsonObject { put("operations", JsonArray(replacements)) })
                val expected = replacements.associate {
                    it.getValue("op_id").jsonPrimitive.content to it.getValue("remote_id").jsonPrimitive.content
                }
                val retryResults = retried.getValue("results").jsonArray
                require(retryResults.size == expected.size)
                val retriedIds = retryResults.map { it.jsonObject.getValue("op_id").jsonPrimitive.content }
                require(retriedIds.toSet().size == retriedIds.size && retriedIds.toSet() == expected.keys)
                require(retryResults.all {
                    val item = it.jsonObject
                    item.getValue("status").jsonPrimitive.content == "accepted" &&
                        item.getValue("remote_id").jsonPrimitive.content ==
                        expected.getValue(item.getValue("op_id").jsonPrimitive.content)
                }) { "密钥轮换替换请求未被接受" }
            }
        }
    }

    private suspend fun uploadAssets(api: SyncApi, cipher: AccountCipher, assets: List<ManagedAssetEntity>) {
        assets.forEach { asset ->
            val file = localAssetFile(asset)
            require(file.isFile && file.length() == asset.byteCount && file.sha256() == asset.sha256) {
                "轮换资源 ${asset.id} 与已验证元数据不一致"
            }
            val count = maxOf(1, ceil(file.length().toDouble() / ROTATION_CHUNK).toInt())
            val blobId = cipher.blobId(asset.id)
            val state = api.createBlob(buildJsonObject {
                put("blob_id", blobId); put("byte_count", file.length())
                put("chunk_count", count); put("key_version", cipher.keyVersion)
            })
            if (state["complete"]?.jsonPrimitive?.content != "true") {
                file.inputStream().use { input ->
                    repeat(count) { index ->
                        api.uploadChunk(blobId, index, cipher.sealBlobChunk(blobId, index, count, input.readUpTo(ROTATION_CHUNK)))
                    }
                }
                api.completeBlob(blobId)
            }
        }
    }

    private suspend fun discardRecoverableRotationUnknowns(cipher: AccountCipher, target: Int) {
        repository.unknownEntities().filter { it.keyVersion == target }.forEach { unknown ->
            val recovered = runCatching {
                val envelope = json.decodeFromString<EncryptedEnvelope>(unknown.encryptedEnvelope)
                cipher.open(unknown.remoteId, envelope)
            }.isSuccess
            if (recovered) repository.deleteUnknown(unknown.remoteId)
        }
    }

    private fun localAssetFile(asset: ManagedAssetEntity): File {
        require(ROTATION_ASSET_SHA256.matches(asset.sha256))
        require(asset.byteCount in 0L..MAX_MANAGED_ASSET_BYTES.toLong())
        require(asset.extension in ROTATION_ASSET_MEDIA_EXTENSIONS[asset.mediaType].orEmpty()) {
            "轮换资源扩展名与媒体类型不匹配"
        }
        val filesRoot = context.filesDir.canonicalFile
        val root = File(filesRoot, "assets").canonicalFile
        val expected = File(root, "${asset.sha256}${asset.extension}").canonicalFile
        val configured = File(context.filesDir, requireNotNull(asset.relativePath)).canonicalFile
        require(root.isDirectory && root.parentFile == filesRoot && expected.parentFile == root && configured == expected) {
            "轮换资源路径位于受管目录之外"
        }
        return configured
    }

    private fun operationId(keyVersion: Int, revisionId: String, version: Long): String {
        val value = "rotation:$keyVersion:$revisionId:$version".toByteArray()
        return "op_" + MessageDigest.getInstance("SHA-256").digest(value).joinToString("") { "%02x".format(it) }
    }

    private suspend fun requireCompleteAssetInventory(): List<ManagedAssetEntity> {
        val assets = classifyManagedAssetsByReachability(
            repository.assets(),
            referencedAssetIds(repository.revisions()),
        ).allForKeyRotation
        val byId = assets.associateBy { it.id }
        val liveHeads = repository.headRevisions().filter {
            it.entityKind == org.mealcircuit.app.domain.EntityKind.ASSET && !it.deleted
        }
        require(liveHeads.map { it.entityId }.distinct().size == liveHeads.size)
        liveHeads.forEach { revision ->
            val asset = requireNotNull(byId[revision.entityId]) {
                "资源 ${revision.entityId} 缺少本地索引，无法安全轮换"
            }
            val payload = revision.payload
            require(
                asset.sha256 == payload.getValue("sha256").jsonPrimitive.content &&
                    asset.mediaType == payload.getValue("media_type").jsonPrimitive.content &&
                    asset.extension == payload.getValue("extension").jsonPrimitive.content &&
                    asset.byteCount == payload.getValue("byte_count").jsonPrimitive.content.toLong()
            ) { "资源 ${revision.entityId} 的索引与活动修订不一致" }
        }
        assets.forEach { asset ->
            require(!asset.unresolved && asset.relativePath != null) {
                "资源 ${asset.id} 尚未完整下载，无法安全轮换"
            }
            val file = localAssetFile(asset)
            require(file.isFile && file.length() == asset.byteCount && file.sha256() == asset.sha256) {
                "资源 ${asset.id} 与本地文件不一致，无法安全轮换"
            }
        }
        return assets
    }

    private fun readStaging(binding: RotationBinding? = null): RotationStagingState =
        decodeRotationStaging(readStagingValues(), json, binding)

    private fun readStagingValues() = RotationStagingValues(
        marker = vault.get(ROTATION_MARKER),
        accountDataKey = vault.get(ROTATION_DATA_KEY),
        recoveryKey = vault.get(ROTATION_RECOVERY_KEY),
        material = vault.get(ROTATION_MATERIAL),
        keyVersion = vault.get(ROTATION_VERSION),
    )

    private fun migrateLegacyStagingIfSafe(
        state: RotationStagingState,
        binding: RotationBinding,
        remote: RotationRemoteState,
    ): RotationStagingState {
        if (state !is RotationStagingState.Invalid) return state
        val values = readStagingValues()
        if (values.marker != null || listOf(
                values.accountDataKey,
                values.recoveryKey,
                values.material,
                values.keyVersion,
            ).any { it == null }
        ) return state
        val target = values.keyVersion?.decodeToString()?.toIntOrNull() ?: return state
        val remoteMatches = if (remote.inProgress) {
            remote.ownedByCurrentDevice && remote.targetKeyVersion == target
        } else {
            remote.activeKeyVersion == target
        }
        if (!remoteMatches) return state
        val marker = RotationMarker(
            format = ROTATION_MARKER_FORMAT,
            version = ROTATION_MARKER_VERSION,
            accountId = binding.accountId,
            deviceId = binding.deviceId,
            serverUrl = binding.serverUrl,
            stage = if (remote.inProgress) ROTATION_STAGE_STAGED else ROTATION_STAGE_REMOTE_COMMITTED,
            targetKeyVersion = target,
        )
        val markerBytes = json.encodeToString(marker).toByteArray()
        val migrated = decodeRotationStaging(values.copy(marker = markerBytes), json, binding)
        if (migrated !is RotationStagingState.Complete) return state
        vault.putAll(
            mapOf(
                ROTATION_MARKER to markerBytes,
                ROTATION_DATA_KEY to requireNotNull(values.accountDataKey),
                ROTATION_RECOVERY_KEY to requireNotNull(values.recoveryKey),
                ROTATION_MATERIAL to requireNotNull(values.material),
                ROTATION_VERSION to requireNotNull(values.keyVersion),
            )
        )
        return migrated
    }

    private fun updateMarker(staging: RotationStaging, stage: String) {
        val marker = staging.marker.copy(stage = stage)
        vault.put(ROTATION_MARKER, json.encodeToString(marker).toByteArray())
    }

    private fun clearStaging() {
        vault.deleteAll(
            listOf(
                ROTATION_MARKER,
                ROTATION_DATA_KEY,
                ROTATION_RECOVERY_KEY,
                ROTATION_MATERIAL,
                ROTATION_VERSION,
            )
        )
    }
}

@Serializable
internal data class RotationMarker(
    val format: String,
    val version: Int,
    val accountId: String,
    val deviceId: String,
    val serverUrl: String,
    val stage: String,
    val targetKeyVersion: Int? = null,
)

internal data class RotationBinding(
    val accountId: String,
    val deviceId: String,
    val serverUrl: String,
)

internal data class RotationStagingValues(
    val marker: ByteArray?,
    val accountDataKey: ByteArray?,
    val recoveryKey: ByteArray?,
    val material: ByteArray?,
    val keyVersion: ByteArray?,
)

internal data class RotationStaging(
    val marker: RotationMarker,
    val accountDataKey: ByteArray,
    val recoveryKey: String,
    val material: RecoveryMaterial,
    val targetKeyVersion: Int,
)

internal sealed class RotationStagingState {
    object Absent : RotationStagingState()
    data class Invalid(val targetKeyVersion: Int?) : RotationStagingState()
    data class Complete(val staging: RotationStaging) : RotationStagingState()
}

private data class RotationRemoteState(
    val inProgress: Boolean,
    val ownedByCurrentDevice: Boolean,
    val activeKeyVersion: Int,
    val targetKeyVersion: Int?,
)

internal fun decodeRotationStaging(
    values: RotationStagingValues,
    json: kotlinx.serialization.json.Json,
    expectedBinding: RotationBinding? = null,
): RotationStagingState {
    val present = listOf(
        values.marker,
        values.accountDataKey,
        values.recoveryKey,
        values.material,
        values.keyVersion,
    )
    if (present.all { it == null }) return RotationStagingState.Absent
    val rawVersion = values.keyVersion?.takeIf { it.size <= 16 }?.decodeToString()
    val decodedMarker = values.marker?.takeIf { it.size <= 8 * 1024 }?.let { encoded ->
        runCatching { json.decodeFromString<RotationMarker>(encoded.decodeToString()) }.getOrNull()
    }
    val targetHint = decodedMarker?.targetKeyVersion ?: rawVersion?.toIntOrNull()
    return runCatching {
        require(present.all { it != null })
        val markerBytes = requireNotNull(values.marker)
        val dataKey = requireNotNull(values.accountDataKey)
        val recoveryBytes = requireNotNull(values.recoveryKey)
        val materialBytes = requireNotNull(values.material)
        val versionBytes = requireNotNull(values.keyVersion)
        require(markerBytes.size <= 8 * 1024 && materialBytes.size <= 64 * 1024)
        require(recoveryBytes.size <= 256 && versionBytes.size <= 16 && dataKey.size == 32)
        val markerElement = json.parseToJsonElement(markerBytes.decodeToString()).jsonObject
        require(
            markerElement.keys == setOf(
                "format",
                "version",
                "accountId",
                "deviceId",
                "serverUrl",
                "stage",
                "targetKeyVersion",
            )
        )
        val marker = json.decodeFromJsonElement<RotationMarker>(markerElement)
        require(marker.format == ROTATION_MARKER_FORMAT && marker.version == ROTATION_MARKER_VERSION)
        require(marker.stage in setOf(
            ROTATION_STAGE_STAGED,
            ROTATION_STAGE_COMMIT_PENDING,
            ROTATION_STAGE_REMOTE_COMMITTED,
        ))
        require(requireSafePathId(marker.accountId, "账户标识") == marker.accountId)
        require(requireSafePathId(marker.deviceId, "设备标识") == marker.deviceId)
        require(validateServerUrl(marker.serverUrl) == marker.serverUrl)
        expectedBinding?.let { require(it == RotationBinding(marker.accountId, marker.deviceId, marker.serverUrl)) }
        val target = requireNotNull(marker.targetKeyVersion).also { require(it > 0) }
        val versionText = versionBytes.decodeToString()
        require(versionText == target.toString())
        val recovery = recoveryBytes.decodeToString()
        require(recovery == recovery.trim().uppercase())
        parseRecoveryKey(recovery)
        val material = json.decodeFromString<RecoveryMaterial>(materialBytes.decodeToString())
        require(material.keyVersion == target && material.recoveryKey == recovery)
        require(material.accountDataKey.contentEquals(dataKey))
        require(
            recoverAccountDataKey(
                marker.accountId,
                recovery,
                material.envelopeNonce,
                material.envelopeCiphertext,
                target,
            ).contentEquals(dataKey)
        )
        RotationStagingState.Complete(
            RotationStaging(marker, dataKey, recovery, material, target)
        )
    }.getOrElse { RotationStagingState.Invalid(targetHint) }
}

internal fun rotationStagingCanBeCleared(activeKeyVersion: Int, localKeyVersion: Int): Boolean {
    require(activeKeyVersion > 0 && localKeyVersion > 0)
    return activeKeyVersion == localKeyVersion
}

private fun SyncConfigurationEntity.rotationBinding(): RotationBinding {
    require(enabled) { "同步尚未启用" }
    val accountId = requireSafePathId(requireNotNull(accountId), "账户标识")
    val deviceId = requireSafePathId(requireNotNull(remoteDeviceId), "设备标识")
    val serverUrl = validateServerUrl(requireNotNull(serverUrl))
    return RotationBinding(accountId, deviceId, serverUrl)
}

private fun JsonObject.rotationState(): RotationRemoteState {
    fun requiredBoolean(name: String): Boolean = when (this[name]?.jsonPrimitive?.content) {
        "true" -> true
        "false" -> false
        else -> error("同步服务返回的轮换状态无效")
    }
    val inProgress = requiredBoolean("in_progress")
    val active = this["active_key_version"]?.jsonPrimitive?.content?.toIntOrNull()
    require(active != null && active > 0) { "同步服务返回的活动密钥版本无效" }
    val target = this["target_key_version"]?.jsonPrimitive?.content?.toIntOrNull()
    require(!inProgress || target != null && target > active) { "同步服务返回的目标密钥版本无效" }
    return RotationRemoteState(
        inProgress = inProgress,
        ownedByCurrentDevice = requiredBoolean("owned_by_current_device"),
        activeKeyVersion = active,
        targetKeyVersion = target,
    )
}

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
    return digest.digest().joinToString("") { "%02x".format(it) }
}

private val ROTATION_REMOTE_ID = Regex("^[0-9a-f]{64}$")
private val ROTATION_ASSET_SHA256 = Regex("^[0-9a-f]{64}$")
private val ROTATION_ASSET_MEDIA_EXTENSIONS = mapOf(
    "image/jpeg" to setOf(".jpg", ".jpeg"),
    "image/png" to setOf(".png"),
    "image/gif" to setOf(".gif"),
    "image/webp" to setOf(".webp"),
)
