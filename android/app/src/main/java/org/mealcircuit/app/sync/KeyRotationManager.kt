package org.mealcircuit.app.sync

import android.content.Context
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
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.io.readUpTo
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import kotlin.math.ceil

private const val ROTATION_DATA_KEY = "sync.rotation.account_data_key"
private const val ROTATION_RECOVERY_KEY = "sync.rotation.recovery_key"
private const val ROTATION_MATERIAL = "sync.rotation.material"
private const val ROTATION_VERSION = "sync.rotation.key_version"
private const val ROTATION_CHUNK = 4 * 1024 * 1024

class KeyRotationManager(
    private val context: Context,
    private val repository: DomainRepository,
    private val vault: SecretVault,
) {
    private val json = repository.json

    suspend fun prepare(): String {
        val config = requireNotNull(repository.syncConfiguration()).also { require(it.enabled) }
        val api = SyncApi(requireNotNull(config.serverUrl), vault)
        val status = api.authorized("/v1/key-rotations/current")
        if (status["in_progress"]?.jsonPrimitive?.content == "true") {
            require(status["owned_by_current_device"]?.jsonPrimitive?.content == "true") {
                "另一台设备正在执行安全轮换"
            }
            return requireNotNull(pendingRecovery()) { "本设备缺少暂存轮换密钥；请中止后重试" }
        }
        val staged = pendingRecovery()
        if (staged != null) {
            val target = requireNotNull(vault.get(ROTATION_VERSION)) { "轮换版本缺失" }.decodeToString().toInt()
            require(status.getValue("active_key_version").jsonPrimitive.content.toInt() == target)
            return staged
        }
        val oldKey = requireNotNull(vault.get("sync.account_data_key")) { "同步尚未解锁" }
        SyncEngine(repository, api, AccountCipher(requireNotNull(config.accountId), oldKey, config.keyVersion), context).run()
        val readiness = repository.rotationReadiness()
        require(readiness == Triple(0, 0, 0)) { "轮换前必须清空待上传、冲突和未知 schema 实体" }
        val begun = api.authorized("/v1/key-rotations", "POST", JsonObject(emptyMap()))
        val target = begun.getValue("target_key_version").jsonPrimitive.content.toInt()
        val material = createRecoveryMaterial(requireNotNull(config.accountId), target)
        vault.put(ROTATION_DATA_KEY, material.accountDataKey)
        vault.put(ROTATION_RECOVERY_KEY, material.recoveryKey.toByteArray())
        vault.put(ROTATION_VERSION, target.toString().toByteArray())
        vault.put(ROTATION_MATERIAL, json.encodeToString(material).toByteArray())
        return material.recoveryKey
    }

    fun pendingRecovery(): String? = vault.get(ROTATION_RECOVERY_KEY)?.decodeToString()

    suspend fun confirm(typedRecoveryKey: String): JsonObject {
        val recovery = requireNotNull(pendingRecovery()) { "没有待确认的轮换" }
        require(typedRecoveryKey.trim().uppercase() == recovery)
        val config = requireNotNull(repository.syncConfiguration())
        val accountId = requireNotNull(config.accountId)
        val target = requireNotNull(vault.get(ROTATION_VERSION)).decodeToString().toInt()
        val material = json.decodeFromString<RecoveryMaterial>(requireNotNull(vault.get(ROTATION_MATERIAL)).decodeToString())
        val dataKey = requireNotNull(vault.get(ROTATION_DATA_KEY))
        val cipher = AccountCipher(accountId, dataKey, target)
        val api = SyncApi(requireNotNull(config.serverUrl), vault)
        val state = api.authorized("/v1/key-rotations/current")
        val revisions = repository.headRevisions()
        val assets = repository.assets().filter { !it.unresolved && it.relativePath != null }
        val committed = if (state["in_progress"]?.jsonPrimitive?.content == "true") {
            require(state["owned_by_current_device"]?.jsonPrimitive?.content == "true")
            pushRevisions(api, cipher, revisions)
            uploadAssets(api, cipher, assets.map { it.id to context.filesDir.resolve(it.relativePath!!) })
            api.authorized(
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
        } else {
            require(state.getValue("active_key_version").jsonPrimitive.content.toInt() == target)
            buildJsonObject { put("active_key_version", target); put("already_committed", true) }
        }
        vault.put("sync.account_data_key", dataKey)
        repository.finalizeKeyRotation(target)
        clearStaging()
        val summary = SyncEngine(repository, api, cipher, context).run()
        return buildJsonObject {
            put("key_version", target)
            put("other_devices_revoked", committed["revoked_devices"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0)
            put("cursor", summary.cursor)
            put("completed_at", Instant.now().toString())
        }
    }

    suspend fun abort() {
        val config = repository.syncConfiguration()
        if (config?.enabled == true && config.serverUrl != null) {
            val api = SyncApi(config.serverUrl, vault)
            val state = api.authorized("/v1/key-rotations/current")
            if (state["in_progress"]?.jsonPrimitive?.content == "true") {
                require(state["owned_by_current_device"]?.jsonPrimitive?.content == "true")
                api.authorized("/v1/key-rotations/current", "DELETE")
            }
        }
        clearStaging()
    }

    private suspend fun pushRevisions(api: SyncApi, cipher: AccountCipher, revisions: List<DomainRevision>) {
        val limit = minOf(100, api.capabilities()["max_batch"]?.jsonPrimitive?.content?.toIntOrNull() ?: 100)
        revisions.chunked(limit).forEach { batch ->
            val byOperation = linkedMapOf<String, DomainRevision>()
            val operations = batch.map { revision ->
                val envelope = cipher.seal(revision)
                val opId = operationId(cipher.keyVersion, revision.revisionId, 0)
                byOperation[opId] = revision
                buildJsonObject {
                    put("op_id", opId); put("remote_id", envelope.remoteId)
                    put("base_server_version", 0); put("key_version", cipher.keyVersion)
                    put("envelope", json.encodeToJsonElement(EncryptedEnvelope.serializer(), envelope))
                }
            }
            val result = api.push(buildJsonObject { put("operations", JsonArray(operations)) })
            val replacements = mutableListOf<JsonObject>()
            result.getValue("results").jsonArray.forEach { element ->
                val item = element.jsonObject
                if (item.getValue("status").jsonPrimitive.content == "accepted") return@forEach
                val local = requireNotNull(byOperation[item.getValue("op_id").jsonPrimitive.content])
                val remoteId = item.getValue("remote_id").jsonPrimitive.content
                val remoteEnvelope = json.decodeFromJsonElement<EncryptedEnvelope>(item.getValue("envelope"))
                val remote = cipher.open(remoteId, remoteEnvelope)
                if (remote.revisionId == local.revisionId) return@forEach
                val version = item.getValue("server_version").jsonPrimitive.content.toLong()
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
                require(retried.getValue("results").jsonArray.all {
                    it.jsonObject.getValue("status").jsonPrimitive.content == "accepted"
                })
            }
        }
    }

    private suspend fun uploadAssets(api: SyncApi, cipher: AccountCipher, assets: List<Pair<String, File>>) {
        assets.forEach { (assetId, file) ->
            require(file.isFile)
            val count = maxOf(1, ceil(file.length().toDouble() / ROTATION_CHUNK).toInt())
            val blobId = cipher.blobId(assetId)
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

    private fun operationId(keyVersion: Int, revisionId: String, version: Long): String {
        val value = "rotation:$keyVersion:$revisionId:$version".toByteArray()
        return "op_" + MessageDigest.getInstance("SHA-256").digest(value).joinToString("") { "%02x".format(it) }
    }

    private fun clearStaging() {
        listOf(ROTATION_DATA_KEY, ROTATION_RECOVERY_KEY, ROTATION_MATERIAL, ROTATION_VERSION).forEach(vault::delete)
    }
}
