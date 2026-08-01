package org.mealcircuit.app.sync

import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.SyncConfigurationEntity
import java.time.Instant
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

@Serializable
data class PendingRegistration(
    val serverUrl: String,
    val accountId: String,
    val deviceId: String,
    val deviceName: String,
    val material: RecoveryMaterial,
)

data class PairingDescriptor(
    val serverUrl: String,
    val accountId: String,
    val pairingId: String,
    val claimToken: String,
)

class SyncAccountManager(
    private val repository: DomainRepository,
    private val vault: SecretVault,
) {
    fun pendingRegistration(): PendingRegistration? = vault.get(PENDING_REGISTRATION)?.let { encoded ->
        runCatching { repository.json.decodeFromString<PendingRegistration>(encoded.decodeToString()) }
            .getOrNull()
    }

    suspend fun beginRegistration(
        serverUrl: String,
        loginName: String,
        password: String,
        deviceName: String,
    ): PendingRegistration = repository.withMutationGate {
        val api = SyncApi(serverUrl, vault)
        val session = api.anonymous(
            "/v1/accounts",
            "POST",
            buildJsonObject {
                put("login_name", loginName)
                put("password", password)
                put("device_name", deviceName)
            },
        )
        val accessToken = session.accessTokenOrNull()
        try {
            storeTokens(session)
            val accountId = session.getValue("account_id").jsonPrimitive.content
            val pending = PendingRegistration(
                api.baseUrl,
                accountId,
                requireSafePathId(session.getValue("device_id").jsonPrimitive.content, "设备标识"),
                deviceName,
                createRecoveryMaterial(accountId),
            )
            storePendingRegistration(pending)
            pending
        } catch (error: Throwable) {
            throwAfterSessionCleanup(
                original = error,
                revokeRemote = {
                    api.deleteCreatedAccount(requireNotNull(accessToken), password)
                },
                clearLocal = ::clearLoginSecrets,
            )
        }
    }

    suspend fun confirmRegistration(pending: PendingRegistration, typedRecoveryKey: String) =
        repository.withMutationGate {
            require(typedRecoveryKey.trim().uppercase() == pending.material.recoveryKey)
            val api = SyncApi(pending.serverUrl, vault)
            api.authorized(
                "/v1/key-envelopes/recovery",
                "PUT",
                buildJsonObject {
                    put("envelope", buildJsonObject {
                        put("version", 1)
                        put("key_version", pending.material.keyVersion)
                        put("nonce", pending.material.envelopeNonce)
                        put("ciphertext", pending.material.envelopeCiphertext)
                    })
                },
            )
            vault.put("sync.account_data_key", pending.material.accountDataKey)
            enable(
                pending.serverUrl,
                pending.accountId,
                pending.deviceId,
                pending.deviceName,
                pending.material.keyVersion,
            )
            vault.delete(PENDING_REGISTRATION)
        }

    suspend fun login(
        serverUrl: String,
        loginName: String,
        password: String,
        deviceName: String,
        recoveryKey: String,
    ): PendingRegistration? = repository.withMutationGate {
        val api = SyncApi(serverUrl, vault)
        val session = api.anonymous(
            "/v1/sessions",
            "POST",
            buildJsonObject {
                put("login_name", loginName)
                put("password", password)
                put("device_name", deviceName)
            },
        )
        val accessToken = session.accessTokenOrNull()
        try {
            storeTokens(session)
            val accountId = session.getValue("account_id").jsonPrimitive.content
            val deviceId = requireSafePathId(
                session.getValue("device_id").jsonPrimitive.content,
                "设备标识",
            )
            val recoveryConfigured = session["recovery_configured"]?.jsonPrimitive?.content
            if (accountNeedsRecoverySetup(recoveryConfigured)) {
                val existing = pendingRegistration()?.takeIf {
                    it.serverUrl == api.baseUrl && it.accountId == accountId
                }?.copy(
                    deviceId = deviceId,
                    deviceName = deviceName,
                )
                val pending = existing ?: PendingRegistration(
                    api.baseUrl,
                    accountId,
                    deviceId,
                    deviceName,
                    createRecoveryMaterial(accountId),
                )
                storePendingRegistration(pending)
                return@withMutationGate pending
            }
            // A legacy server may omit recovery_configured. Fail closed by requiring the existing
            // recovery envelope instead of allowing password-only recovery replacement.
            require(recoveryKey.isNotBlank()) { "请输入恢复密钥" }
            val response = api.authorized("/v1/key-envelopes/recovery")
            val envelope = response.getValue("envelope").jsonObject
            val keyVersion = envelope.getValue("key_version").jsonPrimitive.content.toInt()
            val dataKey = recoverAccountDataKey(
                accountId,
                recoveryKey,
                envelope.getValue("nonce").jsonPrimitive.content,
                envelope.getValue("ciphertext").jsonPrimitive.content,
                keyVersion,
            )
            vault.put("sync.account_data_key", dataKey)
            pendingRegistration()?.let { vault.delete(PENDING_REGISTRATION) }
            enable(
                api.baseUrl,
                accountId,
                deviceId,
                deviceName,
                keyVersion,
            )
            null
        } catch (error: Throwable) {
            throwAfterSessionCleanup(
                original = error,
                revokeRemote = {
                    api.revokeCreatedSession(requireNotNull(accessToken))
                },
                clearLocal = ::clearLoginSecrets,
            )
        }
    }

    suspend fun devices(): JsonObject = repository.withMutationGate {
        val config = repository.syncConfiguration() ?: error("尚未配置同步")
        SyncApi(config.serverUrl ?: error("缺少同步服务地址"), vault).authorized("/v1/devices")
    }

    suspend fun revokeDevice(deviceId: String) = repository.withMutationGate {
        val config = repository.syncConfiguration() ?: error("尚未配置同步")
        SyncApi(config.serverUrl ?: error("缺少同步服务地址"), vault)
            .authorized("/v1/devices/${requireSafePathId(deviceId, "设备标识")}", "DELETE")
    }

    suspend fun deleteAccount(password: String) = repository.withMutationGate {
        require(password.isNotBlank())
        requireNoUnresolvedConflicts(repository.rotationReadiness().second)
        val config = repository.syncConfiguration() ?: error("尚未配置同步")
        SyncApi(config.serverUrl ?: error("缺少同步服务地址"), vault).authorized(
            "/v1/account",
            "DELETE",
            buildJsonObject { put("password", password) },
        )
        unlink()
    }

    suspend fun createPairingQr(): String = repository.withMutationGate {
        val config = repository.syncConfiguration() ?: error("尚未配置同步")
        val accountId = config.accountId ?: error("缺少同步账户 ID")
        val dataKey = vault.get("sync.account_data_key") ?: error("同步凭据已锁定")
        val claimToken = Base64.getUrlEncoder().withoutPadding().encodeToString(
            ByteArray(32).also(SecureRandom()::nextBytes)
        )
        val claimHash = MessageDigest.getInstance("SHA-256")
            .digest(claimToken.toByteArray()).hex()
        val key = hkdf(
            claimToken.toByteArray(),
            MessageDigest.getInstance("SHA-256").digest(accountId.toByteArray()),
            "mealcircuit-pairing-wrap-v1".toByteArray(),
        )
        val nonce = ByteArray(12).also(SecureRandom()::nextBytes)
        val ciphertext = crypt(
            Cipher.ENCRYPT_MODE,
            key,
            nonce,
            "MealCircuit Pairing v1\u0000$accountId".toByteArray(),
            dataKey,
        )
        val api = SyncApi(config.serverUrl ?: error("缺少同步服务地址"), vault)
        val response = api.authorized(
            "/v1/pairings",
            "POST",
            buildJsonObject {
                put("claim_token_hash", claimHash)
                put("envelope", buildJsonObject {
                    put("version", 1)
                    put("key_version", config.keyVersion)
                    put("nonce", Base64.getEncoder().encodeToString(nonce))
                    put("ciphertext", Base64.getEncoder().encodeToString(ciphertext))
                })
            },
        )
        val pairingId = requireSafePathId(
            response.getValue("pairing_id").jsonPrimitive.content,
            "配对标识",
        )
        buildJsonObject {
            put("format", "mealcircuit.pairing")
            put("version", 1)
            put("server_url", api.baseUrl)
            put("account_id", accountId)
            put("pairing_id", pairingId)
            put("claim_token", claimToken)
        }.toString()
    }

    suspend fun claimPairing(
        qrPayload: String,
        expectedServerUrl: String,
        loginName: String,
        password: String,
        deviceName: String,
    ) = repository.withMutationGate {
        val pairing = parsePairingPayload(qrPayload, repository.json)
        val trustedServerUrl = requireMatchingPairingServer(pairing, expectedServerUrl)
        val accountId = pairing.accountId
        val claimToken = pairing.claimToken
        val api = SyncApi(trustedServerUrl, vault)
        val session = api.anonymous(
            "/v1/sessions",
            "POST",
            buildJsonObject {
                put("login_name", loginName)
                put("password", password)
                put("device_name", deviceName)
            },
        )
        val accessToken = session.accessTokenOrNull()
        try {
            storeTokens(session)
            require(session.getValue("account_id").jsonPrimitive.content == accountId)
            val deviceId = requireSafePathId(
                session.getValue("device_id").jsonPrimitive.content,
                "设备标识",
            )
            val response = api.authorized(
                "/v1/pairings/${pairing.pairingId}/claim",
                "POST",
                buildJsonObject { put("claim_token", claimToken) },
            )
            val envelope = response.getValue("envelope").jsonObject
            val keyVersion = envelope.getValue("key_version").jsonPrimitive.content.toInt()
            val key = hkdf(
                claimToken.toByteArray(),
                MessageDigest.getInstance("SHA-256").digest(accountId.toByteArray()),
                "mealcircuit-pairing-wrap-v1".toByteArray(),
            )
            val dataKey = crypt(
                Cipher.DECRYPT_MODE,
                key,
                Base64.getDecoder().decode(envelope.getValue("nonce").jsonPrimitive.content),
                "MealCircuit Pairing v1\u0000$accountId".toByteArray(),
                Base64.getDecoder().decode(envelope.getValue("ciphertext").jsonPrimitive.content),
            )
            vault.put("sync.account_data_key", dataKey)
            pendingRegistration()?.let { vault.delete(PENDING_REGISTRATION) }
            enable(
                api.baseUrl,
                accountId,
                deviceId,
                deviceName,
                keyVersion,
            )
        } catch (error: Throwable) {
            throwAfterSessionCleanup(
                original = error,
                revokeRemote = {
                    api.revokeCreatedSession(requireNotNull(accessToken))
                },
                clearLocal = ::clearLoginSecrets,
            )
        }
    }

    suspend fun unlink() = repository.withMutationGate {
        requireNoUnresolvedConflicts(repository.rotationReadiness().second)
        val current = repository.syncConfiguration() ?: SyncConfigurationEntity(updatedAt = Instant.now().toString())
        repository.disableSync(
            current.copy(
                enabled = false,
                serverUrl = null,
                accountId = null,
                remoteDeviceId = null,
                cursor = 0,
                updatedAt = Instant.now().toString(),
            )
        )
        clearLoginSecrets()
    }

    private fun storeTokens(session: JsonObject) {
        try {
            vault.putAll(
                mapOf(
                    "sync.access_token" to session.getValue("access_token").jsonPrimitive.content.toByteArray(),
                    "sync.refresh_token" to session.getValue("refresh_token").jsonPrimitive.content.toByteArray(),
                )
            )
        } catch (error: Throwable) {
            clearLoginSecrets()
            throw error
        }
    }

    private fun clearLoginSecrets() {
        vault.deleteAll(listOf("sync.account_data_key", "sync.access_token", "sync.refresh_token"))
    }

    private fun storePendingRegistration(pending: PendingRegistration) {
        vault.put(PENDING_REGISTRATION, repository.json.encodeToString(pending).toByteArray())
    }

    private suspend fun enable(
        serverUrl: String,
        accountId: String,
        deviceId: String,
        deviceName: String,
        keyVersion: Int = 1,
    ) {
        repository.configureSync(
            SyncConfigurationEntity(
                enabled = true,
                serverUrl = serverUrl,
                accountId = accountId,
                remoteDeviceId = deviceId,
                deviceName = deviceName,
                keyVersion = keyVersion,
                updatedAt = Instant.now().toString(),
            )
        )
    }

    private fun crypt(mode: Int, key: ByteArray, nonce: ByteArray, aad: ByteArray, value: ByteArray) =
        Cipher.getInstance("AES/GCM/NoPadding").run {
            init(mode, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce)); updateAAD(aad); doFinal(value)
        }

    private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }
}

internal fun parsePairingPayload(payload: String, json: Json): PairingDescriptor {
    require(payload.length in 1..MAX_PAIRING_PAYLOAD_CHARS) { "配对二维码内容无效" }
    val value = runCatching { json.parseToJsonElement(payload).jsonObject }
        .getOrElse { throw IllegalArgumentException("配对二维码内容无效", it) }
    require(value["format"]?.jsonPrimitive?.content == "mealcircuit.pairing") { "配对二维码格式无效" }
    require(value["version"]?.jsonPrimitive?.content == "1") { "不支持该配对二维码版本" }
    val serverUrl = validateServerUrl(value.getValue("server_url").jsonPrimitive.content)
    val accountId = requireSafePathId(value.getValue("account_id").jsonPrimitive.content, "账户标识")
    val pairingId = requireSafePathId(value.getValue("pairing_id").jsonPrimitive.content, "配对标识")
    val claimToken = value.getValue("claim_token").jsonPrimitive.content
    val decodedToken = runCatching { Base64.getUrlDecoder().decode(claimToken) }
        .getOrElse { throw IllegalArgumentException("配对二维码令牌无效", it) }
    require(decodedToken.size == 32 && Base64.getUrlEncoder().withoutPadding().encodeToString(decodedToken) == claimToken) {
        "配对二维码令牌无效"
    }
    return PairingDescriptor(serverUrl, accountId, pairingId, claimToken)
}

internal fun requireMatchingPairingServer(pairing: PairingDescriptor, expectedServerUrl: String): String {
    val trustedServerUrl = validateServerUrl(expectedServerUrl)
    require(pairing.serverUrl == trustedServerUrl) {
        "二维码中的同步服务与手动确认的 URL 不一致"
    }
    return trustedServerUrl
}

private const val MAX_PAIRING_PAYLOAD_CHARS = 8_192
private const val PENDING_REGISTRATION = "sync.pending_registration"

internal fun accountNeedsRecoverySetup(recoveryConfigured: String?): Boolean {
    require(recoveryConfigured == null || recoveryConfigured in setOf("true", "false")) {
        "同步服务返回的恢复配置状态无效"
    }
    return recoveryConfigured == "false"
}

internal fun requireNoUnresolvedConflicts(conflictCount: Int) {
    require(conflictCount >= 0) { "同步冲突计数无效" }
    require(conflictCount == 0) { "存在未解决的同步冲突，解决后才能解除账户关联" }
}

internal suspend fun throwAfterSessionCleanup(
    original: Throwable,
    revokeRemote: suspend () -> Unit,
    clearLocal: () -> Unit,
): Nothing {
    try {
        withContext(NonCancellable) {
            runCatching { revokeRemote() }.exceptionOrNull()?.let { cleanupError ->
                if (cleanupError !== original) original.addSuppressed(cleanupError)
            }
            runCatching { clearLocal() }.exceptionOrNull()?.let { cleanupError ->
                if (cleanupError !== original) original.addSuppressed(cleanupError)
            }
        }
    } catch (cleanupError: Throwable) {
        if (cleanupError !== original) original.addSuppressed(cleanupError)
    }
    throw original
}

private fun JsonObject.accessTokenOrNull(): String? =
    runCatching { this["access_token"]?.jsonPrimitive?.content }.getOrNull()
