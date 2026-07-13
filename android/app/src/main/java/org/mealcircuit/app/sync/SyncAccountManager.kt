package org.mealcircuit.app.sync

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.SyncConfigurationEntity
import java.time.Instant
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

data class PendingRegistration(
    val serverUrl: String,
    val accountId: String,
    val deviceId: String,
    val deviceName: String,
    val material: RecoveryMaterial,
)

class SyncAccountManager(
    private val repository: DomainRepository,
    private val vault: SecretVault,
) {
    suspend fun beginRegistration(
        serverUrl: String,
        loginName: String,
        password: String,
        deviceName: String,
    ): PendingRegistration {
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
        storeTokens(session)
        val accountId = session.getValue("account_id").jsonPrimitive.content
        return PendingRegistration(
            api.baseUrl,
            accountId,
            session.getValue("device_id").jsonPrimitive.content,
            deviceName,
            createRecoveryMaterial(accountId),
        )
    }

    suspend fun confirmRegistration(pending: PendingRegistration, typedRecoveryKey: String) {
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
        enable(pending.serverUrl, pending.accountId, pending.deviceId, pending.deviceName, pending.material.keyVersion)
    }

    suspend fun login(
        serverUrl: String,
        loginName: String,
        password: String,
        deviceName: String,
        recoveryKey: String,
    ) {
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
        storeTokens(session)
        val accountId = session.getValue("account_id").jsonPrimitive.content
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
        enable(
            api.baseUrl,
            accountId,
            session.getValue("device_id").jsonPrimitive.content,
            deviceName,
            keyVersion,
        )
    }

    suspend fun devices(): JsonObject {
        val config = repository.syncConfiguration() ?: error("Synchronization is not configured")
        return SyncApi(config.serverUrl ?: error("Missing server URL"), vault).authorized("/v1/devices")
    }

    suspend fun revokeDevice(deviceId: String) {
        val config = repository.syncConfiguration() ?: error("Synchronization is not configured")
        SyncApi(config.serverUrl ?: error("Missing server URL"), vault)
            .authorized("/v1/devices/$deviceId", "DELETE")
    }

    suspend fun deleteAccount(password: String) {
        require(password.isNotBlank())
        val config = repository.syncConfiguration() ?: error("Synchronization is not configured")
        SyncApi(config.serverUrl ?: error("Missing server URL"), vault).authorized(
            "/v1/account",
            "DELETE",
            buildJsonObject { put("password", password) },
        )
        unlink()
    }

    suspend fun createPairingQr(): String {
        val config = repository.syncConfiguration() ?: error("Synchronization is not configured")
        val accountId = config.accountId ?: error("Missing account ID")
        val dataKey = vault.get("sync.account_data_key") ?: error("Synchronization is locked")
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
        val api = SyncApi(config.serverUrl ?: error("Missing server URL"), vault)
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
        return buildJsonObject {
            put("format", "mealcircuit.pairing")
            put("version", 1)
            put("server_url", api.baseUrl)
            put("account_id", accountId)
            put("pairing_id", response.getValue("pairing_id").jsonPrimitive.content)
            put("claim_token", claimToken)
        }.toString()
    }

    suspend fun claimPairing(
        qrPayload: String,
        loginName: String,
        password: String,
        deviceName: String,
    ) {
        val qr = repository.json.parseToJsonElement(qrPayload).jsonObject
        require(qr["format"]?.jsonPrimitive?.content == "mealcircuit.pairing")
        val serverUrl = qr.getValue("server_url").jsonPrimitive.content
        val accountId = qr.getValue("account_id").jsonPrimitive.content
        val claimToken = qr.getValue("claim_token").jsonPrimitive.content
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
        require(session.getValue("account_id").jsonPrimitive.content == accountId)
        storeTokens(session)
        val response = api.authorized(
            "/v1/pairings/${qr.getValue("pairing_id").jsonPrimitive.content}/claim",
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
        enable(
            api.baseUrl,
            accountId,
            session.getValue("device_id").jsonPrimitive.content,
            deviceName,
            keyVersion,
        )
    }

    suspend fun unlink() {
        val current = repository.syncConfiguration() ?: SyncConfigurationEntity(updatedAt = Instant.now().toString())
        repository.putSyncConfiguration(
            current.copy(
                enabled = false,
                serverUrl = null,
                accountId = null,
                remoteDeviceId = null,
                cursor = 0,
                updatedAt = Instant.now().toString(),
            )
        )
        listOf("sync.account_data_key", "sync.access_token", "sync.refresh_token").forEach(vault::delete)
    }

    private fun storeTokens(session: JsonObject) {
        vault.put("sync.access_token", session.getValue("access_token").jsonPrimitive.content.toByteArray())
        vault.put("sync.refresh_token", session.getValue("refresh_token").jsonPrimitive.content.toByteArray())
    }

    private suspend fun enable(
        serverUrl: String,
        accountId: String,
        deviceId: String,
        deviceName: String,
        keyVersion: Int = 1,
    ) {
        repository.putSyncConfiguration(
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
