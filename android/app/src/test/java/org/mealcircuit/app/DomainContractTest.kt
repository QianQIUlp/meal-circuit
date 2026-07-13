package org.mealcircuit.app

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.ResultValidator
import org.mealcircuit.app.domain.threeWayMerge
import org.mealcircuit.app.domain.preferenceId
import org.mealcircuit.app.domain.CheckinContract
import org.mealcircuit.app.domain.STATE_TRANSITIONS
import org.mealcircuit.app.domain.normalize
import org.mealcircuit.app.io.readUpTo
import org.mealcircuit.app.io.readBounded
import org.mealcircuit.app.sync.AccountCipher
import org.mealcircuit.app.sync.formatRecoveryKey
import org.mealcircuit.app.sync.parseRecoveryKey
import org.mealcircuit.app.sync.SyncFailureDisposition
import org.mealcircuit.app.sync.SyncHttpException
import org.mealcircuit.app.sync.syncFailureDisposition
import org.mealcircuit.app.ui.CAMERA_FAILURE_MESSAGE
import org.mealcircuit.app.ui.finalizeCameraResult
import java.util.Base64
import java.io.ByteArrayInputStream
import java.nio.file.Files
import java.time.LocalDate

class DomainContractTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun pythonAndAndroidShareTheSameEncryptedEnvelopeVector() {
        val revisionText = resource("fixtures/domain-revision.json")
        val vector = Json.parseToJsonElement(resource("fixtures/crypto-v1.json")).jsonObject
        val revision = json.decodeFromString<DomainRevision>(revisionText)
        val key = vector.getValue("account_data_key_hex").jsonPrimitive.content.hexBytes()
        val cipher = AccountCipher(vector.getValue("account_id").jsonPrimitive.content, key)
        val remoteId = vector.getValue("remote_id").jsonPrimitive.content
        val envelope = cipher.sealWithNonce(
            revision,
            remoteId,
            vector.getValue("nonce_hex").jsonPrimitive.content.hexBytes(),
        )
        assertEquals(remoteId, cipher.remoteId("food_item", revision.entityId))
        assertEquals(vector.getValue("ciphertext_base64").jsonPrimitive.content, envelope.ciphertext)
        assertEquals(revision, cipher.open(remoteId, envelope))
    }

    @Test
    fun authenticatedEnvelopeRejectsWrongKeyNonceAadTruncationAndVersion() {
        val revision = json.decodeFromString<DomainRevision>(resource("fixtures/domain-revision.json"))
        val vector = Json.parseToJsonElement(resource("fixtures/crypto-v1.json")).jsonObject
        val key = vector.getValue("account_data_key_hex").jsonPrimitive.content.hexBytes()
        val cipher = AccountCipher(vector.getValue("account_id").jsonPrimitive.content, key)
        val remoteId = vector.getValue("remote_id").jsonPrimitive.content
        val envelope = cipher.sealWithNonce(
            revision,
            remoteId,
            vector.getValue("nonce_hex").jsonPrimitive.content.hexBytes(),
        )
        val nonce = Base64.getDecoder().decode(envelope.nonce).also { it[0] = (it[0].toInt() xor 1).toByte() }
        val ciphertext = Base64.getDecoder().decode(envelope.ciphertext)
        val tampered = ciphertext.copyOf().also { it[it.lastIndex] = (it.last().toInt() xor 1).toByte() }
        val cases = listOf(
            cipher to (remoteId to envelope.copy(nonce = Base64.getEncoder().encodeToString(nonce))),
            cipher to (remoteId to envelope.copy(ciphertext = Base64.getEncoder().encodeToString(tampered))),
            cipher to (remoteId to envelope.copy(ciphertext = Base64.getEncoder().encodeToString(ciphertext.dropLast(1).toByteArray()))),
            cipher to (("0".repeat(64)) to envelope),
            cipher to (remoteId to envelope.copy(keyVersion = 2)),
            AccountCipher(vector.getValue("account_id").jsonPrimitive.content, ByteArray(32) { 127 }) to (remoteId to envelope),
        )
        cases.forEach { (opener, value) ->
            runCatching { opener.open(value.first, value.second) }
                .onSuccess { error("tampered envelope accepted") }
        }

        val blobId = cipher.blobId("asset_fixture")
        val chunk = cipher.sealBlobChunk(blobId, 0, 1, "photo canary".encodeToByteArray())
        assertEquals("photo canary", cipher.openBlobChunk(blobId, 0, 1, chunk).decodeToString())
        runCatching { cipher.openBlobChunk(blobId, 1, 1, chunk) }
            .onSuccess { error("blob AAD substitution accepted") }
        runCatching { cipher.openBlobChunk(blobId, 0, 1, chunk.dropLast(1).toByteArray()) }
            .onSuccess { error("truncated blob accepted") }
    }

    @Test
    fun disjointFieldsMergeAndSameFieldConflicts() {
        val base = buildJsonObject { put("name", "燕麦"); put("protein", 10) }
        val local = buildJsonObject { put("name", "全谷燕麦"); put("protein", 10) }
        val remote = buildJsonObject { put("name", "燕麦"); put("protein", 13) }
        val merged = threeWayMerge(base, local, remote)
        assertTrue(merged.conflicts.isEmpty())
        assertEquals("全谷燕麦", merged.value.getValue("name").jsonPrimitive.content)
        val conflict = threeWayMerge(base, buildJsonObject { put("name", "A") }, buildJsonObject { put("name", "B") })
        assertEquals(listOf("name"), conflict.conflicts)
    }

    @Test
    fun domainRevisionRejectsPayloadIdentitySubstitution() {
        val original = json.decodeFromString<DomainRevision>(resource("fixtures/domain-revision.json"))
        val food = JsonObject(original.payload.getValue("food").jsonObject + ("id" to Json.parseToJsonElement("\"food_another\"")))
        val substituted = original.copy(payload = JsonObject(original.payload + ("food" to food)))
        runCatching(substituted::validate).onSuccess { error("payload identity substitution accepted") }
    }

    @Test
    fun recoveryKeyHasChecksum() {
        val secret = ByteArray(32) { it.toByte() }
        val encoded = formatRecoveryKey(secret)
        assertTrue(secret.contentEquals(parseRecoveryKey(encoded)))
        runCatching { parseRecoveryKey(encoded.dropLast(1) + "A") }.onSuccess { error("tamper accepted") }
    }

    @Test
    fun backgroundSyncRetriesOnlyTransientFailures() {
        assertEquals(SyncFailureDisposition.FAILURE, syncFailureDisposition(SyncHttpException(401, "expired")))
        assertEquals(SyncFailureDisposition.FAILURE, syncFailureDisposition(SyncHttpException(409, "conflict")))
        assertEquals(SyncFailureDisposition.RETRY, syncFailureDisposition(SyncHttpException(503, "offline")))
        assertEquals(SyncFailureDisposition.RETRY, syncFailureDisposition(java.io.IOException("network")))
        assertEquals(SyncFailureDisposition.FAILURE, syncFailureDisposition(IllegalStateException("schema mismatch")))
        assertEquals(SyncFailureDisposition.FAILURE, syncFailureDisposition(javax.crypto.AEADBadTagException("tampered")))
    }

    @Test
    fun api26CompatibleStreamReadReturnsAvailableBytes() {
        val stream = ByteArrayInputStream(byteArrayOf(1, 2, 3))
        assertTrue(stream.readUpTo(2).contentEquals(byteArrayOf(1, 2)))
        assertTrue(stream.readUpTo(2).contentEquals(byteArrayOf(3)))
        assertTrue(stream.readUpTo(2).isEmpty())
        assertTrue(ByteArrayInputStream(ByteArray(8)).readBounded(8).contentEquals(ByteArray(8)))
        runCatching { ByteArrayInputStream(ByteArray(9)).readBounded(8) }
            .onSuccess { error("oversized stream accepted") }
    }

    @Test
    fun failedCameraResultDeletesTemporaryFileAndReturnsActionableError() {
        val failed = Files.createTempFile("mealcircuit-camera-failed", ".jpg").toFile()
        failed.writeBytes(byteArrayOf(1, 2, 3))
        assertEquals(CAMERA_FAILURE_MESSAGE, finalizeCameraResult(false, true, failed))
        assertTrue(!failed.exists())

        val missingUri = Files.createTempFile("mealcircuit-camera-uri", ".jpg").toFile()
        assertEquals(CAMERA_FAILURE_MESSAGE, finalizeCameraResult(true, false, missingUri))
        assertTrue(!missingUri.exists())

        val success = Files.createTempFile("mealcircuit-camera-success", ".jpg").toFile()
        try {
            assertEquals(null, finalizeCameraResult(true, true, success))
            assertTrue(success.exists())
        } finally {
            success.delete()
        }
    }

    @Test
    fun stablePreferenceIdsMatchPython() {
        assertEquals("preferences_715164dc-80a4-5e64-9aae-dbdf7937c67f", preferenceId("profile"))
        assertEquals("preferences_d2214c66-051b-53a6-802b-bae578bb7730", preferenceId("doctrine"))
    }

    @Test
    fun sharedAdaptiveCheckinContractValidatesBranches() {
        val contract = json.decodeFromString<CheckinContract>(resource("checkin-modules-v1.json"))
        assertEquals(listOf("weight", "training", "hunger", "sleep", "gut"), contract.modules.map { it.key })
        val training = contract.module("training")
        val partial = mapOf(
            "trained" to Json.parseToJsonElement("\"yes\""),
            "training_types" to Json.parseToJsonElement("[\"strength\"]"),
        )
        assertTrue(training.questions.first { it.id == "body_parts" }.applicable(partial))
        runCatching { training.normalize(partial, emptyMap(), true) }
            .onSuccess { error("incomplete published branch accepted") }
        assertEquals(2, training.normalize(partial, emptyMap(), false).size)
        val machines = Json.parseToJsonElement(resource("state-machines-v1.json")).jsonObject
        assertEquals("pending", machines.getValue("machines").jsonObject
            .getValue("task").jsonObject.getValue("initial").jsonPrimitive.content)
        STATE_TRANSITIONS.forEach { (machine, states) ->
            val contractStates = machines.getValue("machines").jsonObject.getValue(machine).jsonObject
                .getValue("transitions").jsonObject
            states.forEach { (before, after) ->
                assertEquals(
                    contractStates.getValue(before).jsonArray.map { it.jsonPrimitive.content }.toSet(),
                    after,
                )
            }
        }
    }

    @Test
    fun pythonAndAndroidShareResultContextAndMergeContract() {
        val fixture = Json.parseToJsonElement(resource("fixtures/contract-v1.json")).jsonObject
        val context = fixture.getValue("context").jsonObject
        assertEquals(14, context.getValue("window_days").jsonPrimitive.content.toInt())
        assertTrue(context.getValue("required_task_keys").jsonArray.any {
            it.jsonPrimitive.content == "result_schema"
        })
        assertTrue(context.getValue("required_daily_keys").jsonArray.any {
            it.jsonPrimitive.content == "ingredient_carryover_obligations"
        })

        ResultValidator.task("photo", fixture.getValue("photo_result").jsonObject)
        ResultValidator.task("material", fixture.getValue("material_result").jsonObject)
        val daily = fixture.getValue("daily").jsonObject
        val settings = daily.getValue("settings").jsonObject
        ResultValidator.daily(
            daily.getValue("result").jsonObject,
            LocalDate.parse(daily.getValue("tomorrow").jsonPrimitive.content),
            expectedPriorityFoodIds = daily.getValue("priority_food_ids").jsonArray
                .map { it.jsonPrimitive.content }.toSet(),
            expectedEnvironment = settings.getValue("meal_environment").jsonPrimitive.content,
            expectedProteinTarget = settings.getValue("protein_target_g").jsonArray,
            expectedCarryoverIds = daily.getValue("carryovers").jsonArray
                .map { it.jsonObject.getValue("id").jsonPrimitive.content }.toSet(),
            homeCooking = settings.getValue("home_cooking").jsonObject,
        )

        fixture.getValue("merge_cases").jsonArray.forEach { element ->
            val case = element.jsonObject
            val merged = threeWayMerge(
                case.getValue("base").jsonObject,
                case.getValue("local").jsonObject,
                case.getValue("remote").jsonObject,
            )
            assertEquals(case.getValue("expected").jsonObject, merged.value)
            assertEquals(case.getValue("conflicts").jsonArray.map { it.jsonPrimitive.content }, merged.conflicts)
        }

        val invalidPhoto = buildJsonObject {
            fixture.getValue("photo_result").jsonObject.forEach { (key, value) -> put(key, value) }
            put("unknowns", Json.parseToJsonElement("[\"\"]"))
        }
        runCatching { ResultValidator.task("photo", invalidPhoto) }
            .onSuccess { error("blank result string accepted") }
    }

    private fun resource(name: String) =
        checkNotNull(javaClass.classLoader?.getResourceAsStream(name)).bufferedReader().use { it.readText() }

    private fun String.hexBytes() = chunked(2).map { it.toInt(16).toByte() }.toByteArray()
}
