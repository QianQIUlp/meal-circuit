package org.mealcircuit.app.sync

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.Json
import org.mealcircuit.app.domain.DomainRevision
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

@Serializable
data class EncryptedEnvelope(
    @SerialName("envelope_version") val envelopeVersion: Int = 1,
    @SerialName("key_version") val keyVersion: Int,
    val nonce: String,
    val ciphertext: String,
    @SerialName("remote_id") val remoteId: String,
)

class AccountCipher(
    private val accountId: String,
    private val accountDataKey: ByteArray,
    val keyVersion: Int = 1,
    private val json: Json = Json { ignoreUnknownKeys = true; encodeDefaults = true },
) {
    init {
        require(accountDataKey.size == 32)
        require(keyVersion > 0)
    }

    private val salt = sha256(accountId.toByteArray())
    private val contentKey = hkdf(accountDataKey, salt, "mealcircuit-content-v1".toByteArray())
    private val indexKey = hkdf(accountDataKey, salt, "mealcircuit-index-v1".toByteArray())
    private val assetKey = hkdf(accountDataKey, salt, "mealcircuit-assets-v1".toByteArray())

    fun remoteId(kind: String, entityId: String): String =
        hmac(indexKey, "$kind\u0000$entityId".toByteArray()).hex()

    fun blobId(assetId: String): String = remoteId("asset_blob", assetId)

    fun seal(revision: DomainRevision): EncryptedEnvelope {
        val remoteId = remoteId(revision.entityKind.serializedName(), revision.entityId)
        val nonce = ByteArray(12).also(SecureRandom()::nextBytes)
        return sealWithNonce(revision, remoteId, nonce)
    }

    fun sealWithNonce(revision: DomainRevision, remoteId: String, nonce: ByteArray): EncryptedEnvelope {
        val encrypted = aesGcm(
            Cipher.ENCRYPT_MODE,
            contentKey,
            nonce,
            aad(remoteId),
            canonicalJson(json.encodeToJsonElement(DomainRevision.serializer(), revision)).toString().toByteArray(),
        )
        return EncryptedEnvelope(
            keyVersion = keyVersion,
            nonce = Base64.getEncoder().encodeToString(nonce),
            ciphertext = Base64.getEncoder().encodeToString(encrypted),
            remoteId = remoteId,
        )
    }

    fun open(remoteId: String, envelope: EncryptedEnvelope): DomainRevision {
        require(envelope.envelopeVersion == 1 && envelope.keyVersion == keyVersion)
        val plaintext = aesGcm(
            Cipher.DECRYPT_MODE,
            contentKey,
            Base64.getDecoder().decode(envelope.nonce),
            aad(remoteId),
            Base64.getDecoder().decode(envelope.ciphertext),
        )
        val revision = json.decodeFromString<DomainRevision>(plaintext.decodeToString()).validate()
        require(remoteId(revision.entityKind.serializedName(), revision.entityId) == remoteId)
        return revision
    }

    fun sealBlobChunk(blobId: String, index: Int, count: Int, plaintext: ByteArray): ByteArray {
        val nonce = ByteArray(12).also(SecureRandom()::nextBytes)
        return nonce + aesGcm(Cipher.ENCRYPT_MODE, assetKey, nonce, blobAad(blobId, index, count), plaintext)
    }

    fun openBlobChunk(blobId: String, index: Int, count: Int, value: ByteArray): ByteArray {
        require(value.size >= 28)
        return aesGcm(
            Cipher.DECRYPT_MODE,
            assetKey,
            value.copyOfRange(0, 12),
            blobAad(blobId, index, count),
            value.copyOfRange(12, value.size),
        )
    }

    private fun aad(remoteId: String) =
        "MealCircuit Sync v1\u0000$accountId\u0000$remoteId\u0000$keyVersion".toByteArray()

    private fun blobAad(blobId: String, index: Int, count: Int) =
        "MealCircuit Blob v1\u0000$accountId\u0000$blobId\u0000$keyVersion\u0000$index\u0000$count".toByteArray()
}

@Serializable
data class RecoveryMaterial(
    val accountDataKey: ByteArray,
    val recoveryKey: String,
    val envelopeNonce: String,
    val envelopeCiphertext: String,
    val keyVersion: Int,
)

fun createRecoveryMaterial(accountId: String, keyVersion: Int = 1): RecoveryMaterial {
    require(keyVersion > 0)
    val random = SecureRandom()
    val dataKey = ByteArray(32).also(random::nextBytes)
    val recoverySecret = ByteArray(32).also(random::nextBytes)
    val wrappingKey = hkdf(
        recoverySecret,
        sha256(accountId.toByteArray()),
        "mealcircuit-recovery-wrap-v1".toByteArray(),
    )
    val nonce = ByteArray(12).also(random::nextBytes)
    val aad = "MealCircuit Recovery v1\u0000$accountId\u0000$keyVersion".toByteArray()
    val ciphertext = aesGcm(Cipher.ENCRYPT_MODE, wrappingKey, nonce, aad, dataKey)
    return RecoveryMaterial(
        dataKey,
        formatRecoveryKey(recoverySecret),
        Base64.getEncoder().encodeToString(nonce),
        Base64.getEncoder().encodeToString(ciphertext),
        keyVersion,
    )
}

fun recoverAccountDataKey(
    accountId: String,
    recoveryKey: String,
    nonce: String,
    ciphertext: String,
    keyVersion: Int = 1,
): ByteArray {
    require(keyVersion > 0)
    val secret = parseRecoveryKey(recoveryKey)
    val key = hkdf(secret, sha256(accountId.toByteArray()), "mealcircuit-recovery-wrap-v1".toByteArray())
    return aesGcm(
        Cipher.DECRYPT_MODE,
        key,
        Base64.getDecoder().decode(nonce),
        "MealCircuit Recovery v1\u0000$accountId\u0000$keyVersion".toByteArray(),
        Base64.getDecoder().decode(ciphertext),
    )
}

fun hkdf(material: ByteArray, salt: ByteArray, info: ByteArray, length: Int = 32): ByteArray {
    val pseudoRandomKey = hmac(salt, material)
    val output = ArrayList<Byte>()
    var previous = ByteArray(0)
    var counter = 1
    while (output.size < length) {
        previous = hmac(pseudoRandomKey, previous + info + byteArrayOf(counter.toByte()))
        output.addAll(previous.toList())
        counter += 1
    }
    return output.take(length).toByteArray()
}

private fun aesGcm(
    mode: Int,
    key: ByteArray,
    nonce: ByteArray,
    aad: ByteArray,
    value: ByteArray,
): ByteArray = Cipher.getInstance("AES/GCM/NoPadding").run {
    init(mode, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
    updateAAD(aad)
    doFinal(value)
}

private fun hmac(key: ByteArray, value: ByteArray): ByteArray =
    Mac.getInstance("HmacSHA256").run {
        init(SecretKeySpec(key, "HmacSHA256"))
        doFinal(value)
    }

private fun sha256(value: ByteArray) = MessageDigest.getInstance("SHA-256").digest(value)
private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }

private const val BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

fun formatRecoveryKey(secret: ByteArray): String {
    require(secret.size == 32)
    var buffer = 0
    var bits = 0
    val encoded = StringBuilder()
    secret.forEach { byte ->
        buffer = (buffer shl 8) or (byte.toInt() and 0xff)
        bits += 8
        while (bits >= 5) {
            encoded.append(BASE32[(buffer shr (bits - 5)) and 31])
            bits -= 5
        }
    }
    if (bits > 0) encoded.append(BASE32[(buffer shl (5 - bits)) and 31])
    val groups = encoded.chunked(4).joinToString("-")
    return "MC1-$groups-${sha256(secret).hex().take(8).uppercase()}"
}

fun parseRecoveryKey(value: String): ByteArray {
    val compact = value.trim().uppercase().removePrefix("MC1-").replace("-", "")
    require(compact.length == 60)
    val encoded = compact.dropLast(8)
    val checksum = compact.takeLast(8)
    var buffer = 0
    var bits = 0
    val output = ArrayList<Byte>()
    encoded.forEach { character ->
        val index = BASE32.indexOf(character)
        require(index >= 0)
        buffer = (buffer shl 5) or index
        bits += 5
        if (bits >= 8) {
            output += ((buffer shr (bits - 8)) and 0xff).toByte()
            bits -= 8
        }
    }
    val secret = output.toByteArray()
    require(secret.size == 32 && sha256(secret).hex().take(8).uppercase() == checksum)
    return secret
}

private fun org.mealcircuit.app.domain.EntityKind.serializedName() =
    name.lowercase().let {
        when (this) {
            org.mealcircuit.app.domain.EntityKind.FOOD_ITEM -> "food_item"
            org.mealcircuit.app.domain.EntityKind.DAILY_RECORD -> "daily_record"
            org.mealcircuit.app.domain.EntityKind.CHECKIN_DAY -> "checkin_day"
            org.mealcircuit.app.domain.EntityKind.DAILY_REVIEW -> "daily_review"
            org.mealcircuit.app.domain.EntityKind.ANALYSIS_RESULT -> "analysis_result"
            org.mealcircuit.app.domain.EntityKind.TASK_INPUT -> "task_input"
            else -> it
        }
    }

private fun canonicalJson(value: JsonElement): JsonElement = when (value) {
    is JsonObject -> JsonObject(value.entries.sortedBy { it.key }.associate { it.key to canonicalJson(it.value) })
    is JsonArray -> JsonArray(value.map(::canonicalJson))
    else -> value
}
