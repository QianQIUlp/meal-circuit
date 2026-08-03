package org.mealcircuit.app

import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.domain.ResultValidator
import org.mealcircuit.app.domain.threeWayMerge
import org.mealcircuit.app.domain.preferenceId
import org.mealcircuit.app.domain.CheckinContract
import org.mealcircuit.app.domain.STATE_TRANSITIONS
import org.mealcircuit.app.domain.normalize
import org.mealcircuit.app.domain.validateStateChange
import org.mealcircuit.app.data.MaterializedRecordEntity
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.io.MAX_MANAGED_ASSET_BYTES
import org.mealcircuit.app.io.readUpTo
import org.mealcircuit.app.io.readBounded
import org.mealcircuit.app.portable.bindPortableAssetDescriptors
import org.mealcircuit.app.portable.portableStorageRevision
import org.mealcircuit.app.portable.requirePortableAssetReferences
import org.mealcircuit.app.portable.validatePortableRevisionGraph
import org.mealcircuit.app.sync.AccountCipher
import org.mealcircuit.app.sync.SyncBudget
import org.mealcircuit.app.sync.PermanentAssetException
import org.mealcircuit.app.sync.classifyManagedAssetsByReachability
import org.mealcircuit.app.sync.referencedAssetIds
import org.mealcircuit.app.sync.shouldPauseSyncForUnknownCount
import org.mealcircuit.app.sync.shouldEvictUnknown
import org.mealcircuit.app.sync.allowsAssetDownload
import org.mealcircuit.app.sync.shouldDeferAssetTransfer
import org.mealcircuit.app.ui.visibleCheckinInput
import org.mealcircuit.app.sync.formatRecoveryKey
import org.mealcircuit.app.sync.isRevisionDescendant
import org.mealcircuit.app.sync.safelyProcessAssets
import org.mealcircuit.app.sync.parseRecoveryKey
import org.mealcircuit.app.sync.parsePairingPayload
import org.mealcircuit.app.sync.requireMatchingPairingServer
import org.mealcircuit.app.sync.SyncFailureDisposition
import org.mealcircuit.app.sync.SyncHttpException
import org.mealcircuit.app.sync.readBoundedBytes
import org.mealcircuit.app.sync.readBoundedText
import org.mealcircuit.app.sync.syncFailureDisposition
import org.mealcircuit.app.sync.validateServerUrl
import org.mealcircuit.app.sync.deriveSafePullLimit
import org.mealcircuit.app.sync.PendingRegistration
import org.mealcircuit.app.sync.RecoveryMaterial
import org.mealcircuit.app.sync.accountNeedsRecoverySetup
import java.io.IOException
import org.mealcircuit.app.ui.CAMERA_FAILURE_MESSAGE
import org.mealcircuit.app.ui.finalizeCameraResult
import org.mealcircuit.app.ui.mealModeLabel
import org.mealcircuit.app.ui.publishedPlan
import java.util.Base64
import java.io.ByteArrayInputStream
import java.nio.file.Files
import java.security.MessageDigest
import java.time.LocalDate
import okhttp3.MediaType
import okhttp3.ResponseBody
import okio.Buffer
import okio.BufferedSource

class DomainContractTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun managedAssetReuseRepairsLegacyPartialRowsAndTombstones() {
        val filesDir = Files.createTempDirectory("managed-asset-reuse-").toFile()
        try {
            val bytes = "synthetic managed asset".toByteArray()
            val digest = MessageDigest.getInstance("SHA-256")
                .digest(bytes)
                .joinToString("") { "%02x".format(it) }
            val relative = "assets/$digest.jpg"
            val target = filesDir.resolve(relative)
            target.parentFile?.mkdirs()
            target.writeBytes(bytes)
            val asset = ManagedAssetEntity(
                id = "asset_$digest",
                sha256 = digest,
                mediaType = "image/jpeg",
                extension = ".jpg",
                byteCount = bytes.size.toLong(),
                relativePath = relative,
                unresolved = false,
                createdAt = "2026-08-01T00:00:00Z",
            )
            val payload = buildJsonObject {
                put("sha256", digest)
                put("media_type", "image/jpeg")
                put("extension", ".jpg")
                put("byte_count", bytes.size)
            }.toString()
            fun record(deleted: Boolean) = MaterializedRecordEntity(
                entityId = asset.id,
                entityKind = "asset",
                payloadJson = payload,
                deleted = deleted,
                sortKey = asset.id,
                updatedAt = "2026-08-01T00:00:00Z",
            )

            assertFalse(canReuseManagedAsset(
                filesDir, asset, null, digest, "image/jpeg", ".jpg", bytes.size.toLong()
            ))
            assertFalse(canReuseManagedAsset(
                filesDir, asset, record(deleted = true), digest, "image/jpeg", ".jpg", bytes.size.toLong()
            ))
            assertTrue(canReuseManagedAsset(
                filesDir, asset, record(deleted = false), digest, "image/jpeg", ".jpg", bytes.size.toLong()
            ))
            assertTrue(runCatching {
                canReuseManagedAsset(
                    filesDir,
                    asset,
                    record(deleted = false).copy(payloadJson = payload.replace(digest, "0".repeat(64))),
                    digest,
                    "image/jpeg",
                    ".jpg",
                    bytes.size.toLong(),
                )
            }.isFailure)
        } finally {
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun portableRevisionGraphHandlesTwentyThousandDeepChainIteratively() {
        val template = json.decodeFromString<DomainRevision>(resource("fixtures/domain-revision.json"))
        val depth = 20_000
        val revisions = (0 until depth).map { index ->
            template.copy(
                revisionId = "rev_deep_$index",
                parentRevisionIds = if (index == 0) emptyList() else listOf("rev_deep_${index - 1}"),
            )
        }

        validatePortableRevisionGraph(revisions)

        val cyclic = revisions.toMutableList()
        cyclic[0] = cyclic[0].copy(parentRevisionIds = listOf(cyclic.last().revisionId))
        assertTrue(runCatching { validatePortableRevisionGraph(cyclic) }.isFailure)

        val foreignParent = revisions[0].copy(
            entityId = "entity_foreign",
            revisionId = "rev_foreign",
        )
        val crossEntityChild = revisions[1].copy(parentRevisionIds = listOf(foreignParent.revisionId))
        assertTrue(runCatching {
            validatePortableRevisionGraph(listOf(foreignParent, crossEntityChild))
        }.isFailure)
    }

    @Test
    fun managedAssetReachabilityTraversesIterativelyAndPreservesRotationInventory() {
        val template = json.decodeFromString<DomainRevision>(resource("fixtures/domain-revision.json"))
        var deeplyNested: JsonElement = buildJsonObject {
            put("photo_asset_id", "asset_deep")
        }
        repeat(20_000) {
            deeplyNested = JsonArray(listOf(deeplyNested))
        }
        val current = template.copy(
            entityKind = EntityKind.TASK_INPUT,
            payload = buildJsonObject {
                put("cover_asset_id", "asset_top")
                put("numeric_asset_id", 7)
                put("asset_ids", "asset_plural_is_not_a_reference")
                put("nested", buildJsonObject {
                    put("meal_photo_asset_id", "asset_nested")
                    put("deep", deeplyNested)
                })
            },
        )
        val deletedHistorical = template.copy(
            entityKind = EntityKind.DAILY_RECORD,
            deleted = true,
            payload = buildJsonObject {
                put("receipt_asset_id", "asset_historical")
            },
        )
        val assetMetadata = template.copy(
            entityKind = EntityKind.ASSET,
            payload = buildJsonObject {
                put("preview_asset_id", "asset_metadata_only")
            },
        )

        val referencedIds = referencedAssetIds(listOf(current, deletedHistorical, assetMetadata))
        assertEquals(
            setOf("asset_top", "asset_nested", "asset_deep", "asset_historical"),
            referencedIds,
        )

        fun asset(id: String) = ManagedAssetEntity(
            id = id,
            sha256 = id.hashCode().toUInt().toString(16).padStart(64, '0'),
            mediaType = "image/jpeg",
            extension = ".jpg",
            byteCount = 0,
            relativePath = "assets/$id.jpg",
            unresolved = false,
            createdAt = "2026-08-01T00:00:00Z",
        )
        val assets = listOf(
            asset("asset_orphan"),
            asset("asset_nested"),
            asset("asset_top"),
            asset("asset_deep"),
            asset("asset_historical"),
            asset("asset_metadata_only"),
        )
        val reachability = classifyManagedAssetsByReachability(assets, referencedIds)

        assertEquals(
            listOf("asset_nested", "asset_top", "asset_deep", "asset_historical"),
            reachability.referenced.map { it.id },
        )
        assertEquals(
            listOf("asset_orphan", "asset_metadata_only"),
            reachability.unreferenced.map { it.id },
        )
        assertEquals(assets.map { it.id }, reachability.allForKeyRotation.map { it.id })
    }

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
        listOf(401, 403, 404, 409, 413, 422, 426).forEach { status ->
            assertEquals(
                "HTTP $status must not be retried",
                SyncFailureDisposition.FAILURE,
                syncFailureDisposition(SyncHttpException(status, "permanent")),
            )
        }
        listOf(408, 425, 429, 503).forEach { status ->
            assertEquals(
                "HTTP $status must be retried",
                SyncFailureDisposition.RETRY,
                syncFailureDisposition(SyncHttpException(status, "transient")),
            )
        }
        assertEquals(SyncFailureDisposition.RETRY, syncFailureDisposition(java.io.IOException("network")))
        assertEquals(SyncFailureDisposition.FAILURE, syncFailureDisposition(IllegalStateException("schema mismatch")))
        assertEquals(SyncFailureDisposition.FAILURE, syncFailureDisposition(javax.crypto.AEADBadTagException("tampered")))
        assertEquals(
            SyncFailureDisposition.FAILURE,
            syncFailureDisposition(PermanentAssetException("资源 asset_x 缺少分块 2")),
        )
        assertEquals(
            SyncFailureDisposition.FAILURE,
            syncFailureDisposition(PermanentAssetException("资源 asset_x 与认证元数据不一致")),
        )
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
    fun pendingRegistrationSurvivesSerializationAndCheckinDateStaysExplicit() {
        val pending = PendingRegistration(
            serverUrl = "https://sync.example",
            accountId = "account_fixture",
            deviceId = "device_fixture",
            deviceName = "测试设备",
            material = RecoveryMaterial(
                accountDataKey = ByteArray(32) { it.toByte() },
                recoveryKey = formatRecoveryKey(ByteArray(32) { it.toByte() }),
                envelopeNonce = "nonce",
                envelopeCiphertext = "ciphertext",
                keyVersion = 1,
            ),
        )
        val restored = json.decodeFromString<PendingRegistration>(json.encodeToString(pending))
        assertEquals(json.encodeToString(pending), json.encodeToString(restored))
        assertTrue(pending.material.accountDataKey.contentEquals(restored.material.accountDataKey))
        assertEquals("2026-08-01", requireCheckinDate("2026-08-01"))
        runCatching { requireCheckinDate("2026-08-01T00:00:00") }
            .onSuccess { error("date-time was accepted as a check-in date") }
        assertTrue(accountNeedsRecoverySetup("false"))
        assertTrue(!accountNeedsRecoverySetup("true"))
        assertTrue(!accountNeedsRecoverySetup(null))
        runCatching { accountNeedsRecoverySetup("unknown") }
            .onSuccess { error("invalid recovery setup state was accepted") }
    }

    @Test
    fun boundedHttpBodiesCheckDeclaredAndActualLength() {
        assertEquals("1234", responseBody("1234".encodeToByteArray()).readBoundedText(4, "test body"))
        runCatching { responseBody(ByteArray(5)).readBoundedBytes(4, "declared body") }
            .onSuccess { error("oversized declared body accepted") }
        runCatching { responseBody(ByteArray(5), declaredLength = -1).readBoundedBytes(4, "streamed body") }
            .onSuccess { error("oversized streamed body accepted") }
    }

    @Test
    fun syncPullLimitRespectsEntityAndResponseByteBudgets() {
        assertEquals(
            31,
            deriveSafePullLimit(
                serverMaxPull = 500,
                maxEntityBytes = 1024L * 1024L,
                maxPullResponseBytes = 32L * 1024L * 1024L,
            ),
        )
        assertEquals(
            1,
            deriveSafePullLimit(
                serverMaxPull = 500,
                maxEntityBytes = 16L * 1024L * 1024L,
                maxPullResponseBytes = 32L * 1024L * 1024L,
            ),
        )
        assertTrue(runCatching {
            deriveSafePullLimit(
                serverMaxPull = 7,
                maxEntityBytes = 8L * 1024L,
                maxPullResponseBytes = 8L * 1024L,
            )
        }.isFailure)
    }

    @Test
    fun synchronizationUrlsAreCanonicalAndRejectCredentialRoutingTricks() {
        assertEquals("https://example.com/sync", validateServerUrl("HTTPS://Example.COM:443/sync/"))
        assertEquals("http://127.0.0.1:18080", validateServerUrl("http://127.0.0.1:18080/"))
        listOf(
            "http://example.com",
            "https://user@example.com",
            "https://example.com/path?next=https://attacker.invalid",
            "https://example.com/path#fragment",
            "https://example.com/%2e%2e/private",
            "https://example.com/path\\escape",
        ).forEach { malicious ->
            runCatching { validateServerUrl(malicious) }
                .onSuccess { error("unsafe synchronization URL accepted: $malicious") }
        }
    }

    @Test
    fun pairingQrCannotSilentlyChooseWhereCredentialsAreSent() {
        val claimToken = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32) { it.toByte() })
        val payload = buildJsonObject {
            put("format", "mealcircuit.pairing")
            put("version", 1)
            put("server_url", "https://Sync.Example:443/base/")
            put("account_id", "account_00000000-0000-4000-8000-000000000001")
            put("pairing_id", "pairing_00000000-0000-4000-8000-000000000002")
            put("claim_token", claimToken)
        }
        val pairing = parsePairingPayload(payload.toString(), json)
        assertEquals("https://sync.example/base", pairing.serverUrl)
        assertEquals(
            "https://sync.example/base",
            requireMatchingPairingServer(pairing, "https://sync.example:443/base/"),
        )
        runCatching { requireMatchingPairingServer(pairing, "https://attacker.invalid") }
            .onSuccess { error("pairing server substitution accepted") }

        val unsafeId = JsonObject(payload + ("pairing_id" to JsonPrimitive("../account")))
        runCatching { parsePairingPayload(unsafeId.toString(), json) }
            .onSuccess { error("pairing path injection accepted") }
        val unsafeToken = JsonObject(payload + ("claim_token" to JsonPrimitive("not-a-32-byte-token")))
        runCatching { parsePairingPayload(unsafeToken.toString(), json) }
            .onSuccess { error("malformed pairing token accepted") }
        runCatching { parsePairingPayload("x".repeat(8_193), json) }
            .onSuccess { error("oversized pairing payload accepted") }
    }

    @Test
    fun assetRevisionValidatesSafeMetadataAndRejectsPathOrSizeAbuse() {
        val valid = DomainRevision.create(
            kind = EntityKind.ASSET,
            entityId = "asset_fixture",
            deviceId = "device_fixture",
            payload = buildJsonObject {
                put("sha256", "0".repeat(64))
                put("media_type", "image/jpeg")
                put("extension", ".jpg")
                put("byte_count", 4)
            },
        )
        valid.copy(payload = JsonObject(valid.payload + ("extension" to JsonPrimitive(".jpeg")))).validate()
        valid.copy(payload = JsonObject(valid.payload + ("byte_count" to JsonPrimitive(0)))).validate()
        valid.copy(
            payload = JsonObject(valid.payload + ("byte_count" to JsonPrimitive(MAX_MANAGED_ASSET_BYTES))),
        ).validate()

        val invalidFields = listOf(
            "sha256" to JsonPrimitive("A".repeat(64)),
            "sha256" to JsonPrimitive("0".repeat(63)),
            "media_type" to JsonPrimitive("application/octet-stream"),
            "extension" to JsonPrimitive(".png"),
            "extension" to JsonPrimitive("/../../escape-canary"),
            "extension" to JsonPrimitive("..\\escape-canary.jpg"),
            "byte_count" to JsonPrimitive(-1),
            "byte_count" to JsonPrimitive(MAX_MANAGED_ASSET_BYTES + 1),
            "byte_count" to JsonPrimitive("4"),
        )
        invalidFields.forEachIndexed { index, (field, value) ->
            val malicious = valid.copy(payload = JsonObject(valid.payload + (field to value)))
            runCatching(malicious::validate)
                .onSuccess { error("invalid asset metadata case $index was accepted") }
        }
    }

    @Test
    fun portableAssetDescriptorsBindExactlyToAssetHeads() {
        val assetId = "asset_portable_fixture"
        val digest = "1".repeat(64)
        val path = "assets/$digest.jpg"
        fun assetRevision(
            id: String = assetId,
            sha256: String = digest,
            archivePath: String = "assets/$sha256.jpg",
        ) = DomainRevision.create(
            kind = EntityKind.ASSET,
            entityId = id,
            deviceId = "device_portable_fixture",
            payload = buildJsonObject {
                put("sha256", sha256)
                put("media_type", "image/jpeg")
                put("extension", ".jpg")
                put("byte_count", 4)
                put("archive_path", archivePath)
            },
        )
        fun descriptor(
            id: String = assetId,
            sha256: String = digest,
            descriptorPath: String = "assets/$sha256.jpg",
            bytes: JsonPrimitive = JsonPrimitive(4),
            mediaType: String = "image/jpeg",
        ) = buildJsonObject {
            put("id", id)
            put("sha256", sha256)
            put("path", descriptorPath)
            put("bytes", bytes)
            put("media_type", mediaType)
        }

        val head = assetRevision()
        val valid = descriptor()
        val binding = bindPortableAssetDescriptors(
            revisions = listOf(head),
            heads = mapOf(assetId to head.revisionId),
            descriptors = listOf(valid),
        ).single()
        assertEquals(assetId, binding.id)
        assertEquals(path, binding.path)
        assertEquals(head, binding.revision)
        val legacyBinding = bindPortableAssetDescriptors(
            revisions = listOf(head),
            heads = mapOf(assetId to head.revisionId),
            descriptors = listOf(buildJsonObject {
                put("sha256", digest)
                put("path", path)
                put("bytes", 4)
            }),
        ).single()
        assertEquals(assetId, legacyBinding.id)
        assertEquals("image/jpeg", legacyBinding.mediaType)
        val storageHead = portableStorageRevision(head)
        assertEquals(head.revisionId, storageHead.revisionId)
        assertTrue("archive_path" !in storageHead.payload)
        assertEquals(JsonObject(head.payload - "archive_path"), storageHead.payload)

        val taskInput = DomainRevision.create(
            kind = EntityKind.TASK_INPUT,
            entityId = "task_input_portable_fixture",
            deviceId = "device_portable_fixture",
            payload = buildJsonObject {
                put("task_id", "task_portable_fixture")
                put("task_type", "photo")
                put("input_version", 1)
                put("original_input", "")
                put("asset_id", assetId)
                put("input_history", JsonArray(emptyList()))
            },
        )
        requirePortableAssetReferences(listOf(taskInput, head.copy(deleted = true)), setOf(assetId))
        assertTrue(runCatching {
            requirePortableAssetReferences(listOf(taskInput), emptySet())
        }.isFailure)

        val extraId = "asset_portable_extra"
        val extraDigest = "2".repeat(64)
        val extraHead = assetRevision(extraId, extraDigest)
        val invalidCases = listOf(
            emptyList(),
            listOf(valid, valid),
            listOf(descriptor(id = extraId, sha256 = extraDigest)),
            listOf(descriptor(sha256 = "3".repeat(64))),
            listOf(descriptor(descriptorPath = "assets/$digest.png")),
            listOf(descriptor(bytes = JsonPrimitive(5))),
            listOf(descriptor(bytes = JsonPrimitive("4"))),
            listOf(descriptor(mediaType = "image/png")),
            listOf(JsonObject(valid + ("unexpected" to JsonPrimitive(true)))),
        )
        invalidCases.forEachIndexed { index, descriptors ->
            runCatching {
                bindPortableAssetDescriptors(listOf(head), mapOf(assetId to head.revisionId), descriptors)
            }.onSuccess { error("invalid portable asset descriptor case $index was accepted") }
        }

        val wrongArchiveHead = assetRevision(archivePath = "assets/$digest.png")
        runCatching {
            bindPortableAssetDescriptors(
                listOf(wrongArchiveHead),
                mapOf(assetId to wrongArchiveHead.revisionId),
                listOf(valid),
            )
        }.onSuccess { error("mismatched archive_path was accepted") }

        runCatching {
            bindPortableAssetDescriptors(
                listOf(head, extraHead),
                mapOf(assetId to head.revisionId, extraId to extraHead.revisionId),
                listOf(valid, descriptor(id = extraId)),
            )
        }.onSuccess { error("duplicate portable asset path was accepted") }
    }

    @Test
    fun remoteRevisionMustReachCurrentHeadThroughBoundedSameEntityAncestry() = runBlocking {
        val root = json.decodeFromString<DomainRevision>(resource("fixtures/domain-revision.json"))
        val child = DomainRevision.create(
            kind = root.entityKind,
            entityId = root.entityId,
            parents = listOf(root.revisionId),
            deviceId = root.authorDeviceId,
            payload = root.payload,
        )
        val grandchild = DomainRevision.create(
            kind = root.entityKind,
            entityId = root.entityId,
            parents = listOf(child.revisionId),
            deviceId = root.authorDeviceId,
            payload = root.payload,
        )
        val known = mapOf(root.revisionId to root, child.revisionId to child)
        assertTrue(isRevisionDescendant(grandchild, root, known::get))
        assertTrue(isRevisionDescendant(root, root, known::get))

        val replay = DomainRevision.create(
            kind = root.entityKind,
            entityId = root.entityId,
            deviceId = root.authorDeviceId,
            payload = root.payload,
        )
        assertTrue(!isRevisionDescendant(replay, root, known::get))
        assertTrue(!isRevisionDescendant(grandchild, root, known::get, maxVisited = 1))

        val foreign = child.copy(
            entityId = DomainRevision.id("food"),
            parentRevisionIds = listOf(root.revisionId),
        )
        val forged = grandchild.copy(parentRevisionIds = listOf(foreign.revisionId))
        assertTrue(!isRevisionDescendant(forged, root, mapOf(foreign.revisionId to foreign)::get))
        assertTrue(!isRevisionDescendant(foreign, root, emptyMap<String, DomainRevision>()::get))
    }

    @Test
    fun unknownCapacityChargesOnlyNewRowsAndForcedMediaIgnoresPolicy() {
        val full = SyncBudget(storedAtStart = 2_000)
        assertTrue(shouldPauseSyncForUnknownCount(2_000))
        assertTrue(!shouldPauseSyncForUnknownCount(1_999))
        full.reserve(isNew = false)
        assertEquals(0, full.addedThisRun)
        runCatching { full.reserve(isNew = true) }
            .onSuccess { error("new unknown row was accepted above the storage cap") }
        assertTrue(!full.tryReserve(isNew = true))

        val perRun = SyncBudget(storedAtStart = 0, addedThisRun = 500)
        perRun.reserve(isNew = false)
        runCatching { perRun.reserve(isNew = true) }
            .onSuccess { error("new unknown row was accepted above the per-run cap") }
        assertTrue(!perRun.tryReserve(isNew = true))

        val available = SyncBudget(storedAtStart = 1_999)
        assertTrue(available.tryReserve(isNew = true))
        assertEquals(1, available.addedThisRun)
        assertTrue(!available.tryReserve(isNew = true))

        assertTrue(allowsAssetDownload("on_demand", unmetered = false, includeOnDemandMedia = true))
        assertTrue(allowsAssetDownload("all_wifi", unmetered = false, includeOnDemandMedia = true))
        assertTrue(!allowsAssetDownload("all_wifi", unmetered = false, includeOnDemandMedia = false))
        assertTrue(allowsAssetDownload("all_wifi", unmetered = true, includeOnDemandMedia = false))

        assertTrue(shouldDeferAssetTransfer("all_wifi", unmetered = false, hasPendingAssets = true))
        assertTrue(!shouldDeferAssetTransfer("all_wifi", unmetered = true, hasPendingAssets = true))
        assertTrue(!shouldDeferAssetTransfer("all_wifi", unmetered = false, hasPendingAssets = false))
        assertTrue(!shouldDeferAssetTransfer("all", unmetered = false, hasPendingAssets = true))
        assertTrue(!shouldDeferAssetTransfer("on_demand", unmetered = false, hasPendingAssets = true))
    }

    @Test
    fun unknownReprocessAttemptsAreBoundedBeforeEviction() {
        assertTrue(!shouldEvictUnknown(9))
        assertTrue(shouldEvictUnknown(10))
        assertTrue(shouldEvictUnknown(99))
    }

    @Test
    fun oneAssetFailureDoesNotBlockLaterAssetsButCancellationStillStops() = runBlocking {
        fun asset(id: String) = ManagedAssetEntity(
            id = id,
            sha256 = "0".repeat(64),
            mediaType = "image/jpeg",
            extension = ".jpg",
            byteCount = 0,
            relativePath = null,
            unresolved = true,
            createdAt = "2026-08-01T00:00:00Z",
        )
        val assets = listOf(asset("asset_one"), asset("asset_bad"), asset("asset_three"))
        val visited = mutableListOf<String>()
        val errors = mutableListOf<String>()
        val result = safelyProcessAssets(assets, errors) { value ->
            visited += value.id
            if (value.id == "asset_bad") error("fixture failure")
        }
        assertEquals(listOf("asset_one", "asset_bad", "asset_three"), visited)
        assertEquals(1, errors.size)
        assertTrue(errors.single().startsWith("asset_bad:"))
        assertEquals(0, result.transientFailures)
        assertEquals(1, result.permanentFailures)

        val transient = safelyProcessAssets(listOf(asset("asset_network")), mutableListOf()) {
            throw IOException("temporary")
        }
        assertEquals(1, transient.transientFailures)
        assertEquals(0, transient.permanentFailures)

        val missing = safelyProcessAssets(listOf(asset("asset_missing")), mutableListOf()) {
            throw PermanentAssetException("资源 asset_missing 缺少分块 0")
        }
        assertEquals(0, missing.transientFailures)
        assertEquals(1, missing.permanentFailures)

        val cancelledVisits = mutableListOf<String>()
        val cancellation = runCatching {
            safelyProcessAssets(assets, mutableListOf()) { value ->
                cancelledVisits += value.id
                if (value.id == "asset_bad") throw CancellationException("stop")
            }
        }
        assertTrue(cancellation.exceptionOrNull() is CancellationException)
        assertEquals(listOf("asset_one", "asset_bad"), cancelledVisits)
    }

    @Test
    fun unknownEnvelopeBudgetLimitsSingleRunAndPersistentBytes() {
        val single = SyncBudget(storedAtStart = 0)
        assertTrue(runCatching {
            single.reserve(isNew = true, previousBytes = 0, newBytes = 16 * 1024 * 1024 + 1)
        }.isFailure)

        val perRun = SyncBudget(storedAtStart = 0)
        perRun.reserve(isNew = true, previousBytes = 0, newBytes = 16 * 1024 * 1024)
        perRun.reserve(isNew = true, previousBytes = 0, newBytes = 16 * 1024 * 1024)
        assertTrue(runCatching {
            perRun.reserve(isNew = true, previousBytes = 0, newBytes = 1)
        }.isFailure)
        assertTrue(!perRun.tryReserve(isNew = true, previousBytes = 0, newBytes = 1))

        val persistent = SyncBudget(storedAtStart = 1, storedBytes = 63L * 1024L * 1024L)
        assertTrue(runCatching {
            persistent.reserve(isNew = true, previousBytes = 0, newBytes = 2 * 1024 * 1024)
        }.isFailure)
        assertTrue(!persistent.tryReserve(isNew = true, previousBytes = 0, newBytes = 2 * 1024 * 1024))
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
    fun checkinSubmissionIncludesOnlyVisibleModules() {
        val visible = visibleCheckinInput(
            enabledModules = setOf("training"),
            answers = mapOf(
                "weight" to mapOf("weight_kg" to JsonPrimitive("70")),
                "training" to mapOf("trained" to JsonPrimitive("yes")),
            ),
            other = mapOf(
                "gut" to mapOf("symptoms" to "隐藏说明"),
                "training" to mapOf("training_types" to "力量"),
            ),
            skipped = setOf("sleep", "training"),
        )

        assertEquals(setOf("training"), visible.answers.keys)
        assertEquals(setOf("training"), visible.other.keys)
        assertEquals(setOf("training"), visible.skipped)
        assertTrue(visible.hasContent)

        val hiddenOnly = visibleCheckinInput(
            enabledModules = setOf("sleep"),
            answers = mapOf("weight" to mapOf("weight_kg" to JsonPrimitive("70"))),
            other = mapOf("gut" to mapOf("symptoms" to "隐藏说明")),
            skipped = setOf("training"),
        )
        assertFalse(hiddenOnly.hasContent)
        assertTrue(hiddenOnly.answers.isEmpty())
        assertTrue(hiddenOnly.other.isEmpty())
        assertTrue(hiddenOnly.skipped.isEmpty())
    }

    @Test
    fun invalidStateTransitionUsesNaturalChineseMessage() {
        val before = buildJsonObject {
            put("task", buildJsonObject { put("status", "completed") })
        }
        val after = buildJsonObject {
            put("task", buildJsonObject { put("status", "pending") })
        }

        val error = runCatching {
            validateStateChange(EntityKind.TASK, before, after)
        }.exceptionOrNull()

        assertTrue(error is IllegalArgumentException)
        assertEquals("任务状态不允许从「completed」变为「pending」", error?.message)
    }

    @Test
    fun completedTaskPayloadCannotBeOverwritten() {
        val before = buildJsonObject {
            put("task", buildJsonObject {
                put("status", "completed")
                put("result_version", 1)
                put("result_json", buildJsonObject { put("summary", "原结果") })
            })
        }
        val after = buildJsonObject {
            put("task", buildJsonObject {
                put("status", "completed")
                put("result_version", 2)
                put("result_json", buildJsonObject { put("summary", "被覆盖") })
            })
        }

        val error = runCatching { validateStateChange(EntityKind.TASK, before, after) }.exceptionOrNull()

        assertTrue(error is IllegalArgumentException)
        assertEquals("已完成任务的结果不可直接覆盖", error?.message)
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

    @Test
    fun publishedPythonPlanKeepsExecutionDetailsVisibleOnAndroid() {
        val record = MaterializedRecordEntity(
            entityId = "review_fixture",
            entityKind = "daily_review",
            payloadJson = """
                {
                  "review": {
                    "id": "review_fixture", "review_date": "2026-07-18", "status": "completed",
                    "source_record_ids_json": [], "result_version": 1,
                    "created_at": "2026-07-18T00:00:00Z", "updated_at": "2026-07-18T00:00:00Z",
                    "result_json": {
                      "case_summary": "优先保证饱腹和执行性",
                      "core_advice": ["晚餐先保证主蛋白"],
                      "problems_to_solve": ["晚间容易饿"],
                      "selected_strategy": "balanced",
                      "strategy_tradeoffs": ["不压低主食"],
                      "day_nutrition": {"protein_g": [110, 130], "confidence": "medium"},
                      "adjustment_conditions": ["睡眠不足时不继续减量"],
                      "tomorrow_menu": {
                        "date": "2026-07-19",
                        "meals": [{
                          "name": "午餐", "mode": "eat_out", "foods": ["鱼", "米饭", "蔬菜"],
                          "purpose": "稳定下午精力", "whole_day_role": "保留训练前主食",
                          "portion_contracts": [{
                            "item": "鱼", "gram_range": [120, 160], "measurement_basis": "cooked",
                            "household_measure": "一掌", "increase_if": "仍饿时加半拳主食",
                            "decrease_if": "食欲低时先减主食"
                          }],
                          "eat_out_guidance": {"protein_anchor": "优先清蒸鱼", "sauce_rule": "酱汁分开"},
                          "adjustment_logic": {"if_gut_unwell": "改软烂少油"}
                        }]
                      }
                    }
                  }, "history": []
                }
            """.trimIndent(),
            deleted = false,
            sortKey = "2026-07-18",
            updatedAt = "2026-07-18T00:00:00Z",
        )

        val plan = requireNotNull(publishedPlan(record))
        assertEquals("2026-07-19", plan.planDate)
        assertEquals("110–130 g 蛋白质 · 中等把握", plan.nutrition)
        assertEquals("在家下厨", mealModeLabel("home_cook"))
        val lunch = plan.meals.single()
        assertEquals("午餐", lunch.name)
        assertEquals("120–160 g · 熟重 · 一掌", lunch.portions.single().amount)
        assertTrue(lunch.eatOutGuidance.any { it.contains("清蒸鱼") })
        assertTrue(lunch.adjustments.any { it.contains("少油") })
    }

    private fun resource(name: String) =
        checkNotNull(javaClass.classLoader?.getResourceAsStream(name)).bufferedReader().use { it.readText() }

    private fun String.hexBytes() = chunked(2).map { it.toInt(16).toByte() }.toByteArray()

    private fun responseBody(value: ByteArray, declaredLength: Long = value.size.toLong()) =
        object : ResponseBody() {
            override fun contentType(): MediaType? = null
            override fun contentLength(): Long = declaredLength
            override fun source(): BufferedSource = Buffer().write(value)
        }
}
