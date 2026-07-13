package org.mealcircuit.app.portable

import android.content.Context
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.data.SyncConflictEntity
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.threeWayMerge
import org.mealcircuit.app.io.readUpTo
import org.mealcircuit.app.io.readBounded
import org.mealcircuit.app.io.copyToBounded
import org.mealcircuit.app.io.MAX_MANAGED_ASSET_BYTES
import org.mealcircuit.app.sync.formatRecoveryKey
import org.mealcircuit.app.sync.hkdf
import org.mealcircuit.app.sync.parseRecoveryKey
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.security.SecureRandom
import java.time.Instant
import java.util.Base64
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

enum class ImportMode { RESTORE, MERGE }
data class ImportPreview(val entities: Int, val revisions: Int, val assets: Int, val conflicts: Int)

class PortableData(
    private val context: Context,
    private val repository: DomainRepository,
    private val json: Json = repository.json,
) {
    suspend fun export(output: OutputStream, encrypted: Boolean = true): String? {
        val plain = File.createTempFile("mealcircuit-", ".zip", context.cacheDir)
        try {
            buildZip(plain)
            if (!encrypted) {
                plain.inputStream().use { it.copyTo(output) }
                return null
            }
            val secret = ByteArray(32).also(SecureRandom()::nextBytes)
            encryptMcx(plain, output, secret)
            return formatRecoveryKey(secret)
        } finally {
            plain.delete()
        }
    }

    suspend fun preview(input: InputStream, recoveryKey: String?, mode: ImportMode): ImportPreview {
        val archive = materializeArchive(input, recoveryKey)
        try {
            val parsed = readValidated(archive)
            return calculatePreview(parsed, mode)
        } finally {
            archive.delete()
        }
    }

    suspend fun import(input: InputStream, recoveryKey: String?, mode: ImportMode): ImportPreview {
        val archive = materializeArchive(input, recoveryKey)
        val createdFiles = mutableListOf<File>()
        try {
            val parsed = readValidated(archive)
            val preview = calculatePreview(parsed, mode)
            repository.importTransaction {
                createdFiles += extractAssets(archive, parsed)
                parsed.revisions.forEach { repository.storeRevision(it, materialize = false) }
                val byId = parsed.revisions.associateBy { it.revisionId }
                for ((entityId, revisionId) in parsed.heads) {
                    val remote = byId.getValue(revisionId)
                    val localHead = repository.heads().firstOrNull { it.entityId == entityId }
                    if (localHead == null) {
                        repository.commitRevision(remote, queue = false)
                        continue
                    }
                    val local = repository.revision(localHead.revisionId) ?: continue
                    if (local.payload == remote.payload && local.deleted == remote.deleted) continue
                    val base = commonAncestor(local, remote, byId)
                    if (base == null) {
                        recordConflict(local, remote, null, listOf("$"))
                        continue
                    }
                    val merged = threeWayMerge(base.payload, local.payload, remote.payload)
                    val deleteEdit = local.deleted != remote.deleted &&
                        ((local.deleted != base.deleted && remote.payload != base.payload) ||
                            (remote.deleted != base.deleted && local.payload != base.payload))
                    val paths = merged.conflicts + if (deleteEdit) listOf("\$deleted") else emptyList()
                    if (paths.isNotEmpty()) {
                        recordConflict(local, remote, base, paths)
                        continue
                    }
                    repository.commitRevision(
                        DomainRevision.create(
                            local.entityKind,
                            entityId,
                            listOf(local.revisionId, remote.revisionId),
                            repository.deviceId,
                            merged.value,
                            if (local.deleted == base.deleted) remote.deleted else local.deleted,
                        )
                    )
                }
                val storedIds = repository.revisions().map { it.revisionId }.toSet()
                require(storedIds.containsAll(parsed.revisions.map { it.revisionId }))
                if (mode == ImportMode.RESTORE) {
                    val roundTrip = File.createTempFile("mealcircuit-roundtrip-", ".zip", context.cacheDir)
                    try {
                        buildZip(roundTrip)
                        val restored = readValidated(roundTrip)
                        require(restored.heads == parsed.heads)
                        require(restored.revisions.map { it.revisionId }.toSet() == parsed.revisions.map { it.revisionId }.toSet())
                        require(restored.assets.map { it.getValue("sha256").jsonPrimitive.content }.toSet() ==
                            parsed.assets.map { it.getValue("sha256").jsonPrimitive.content }.toSet())
                    } finally {
                        roundTrip.delete()
                    }
                }
            }
            return preview
        } catch (error: Throwable) {
            createdFiles.forEach(File::delete)
            throw error
        } finally {
            archive.delete()
        }
    }

    private suspend fun calculatePreview(parsed: Parsed, mode: ImportMode): ImportPreview {
        val local = repository.heads().associateBy { it.entityId }
        val conflicts = parsed.heads.count { (entityId, revisionId) ->
            val current = local[entityId] ?: return@count false
            current.revisionId != revisionId
        }
        require(mode == ImportMode.MERGE || local.isEmpty()) { "Restore target is not empty" }
        return ImportPreview(parsed.heads.size, parsed.revisions.size, parsed.assets.size, conflicts)
    }

    private suspend fun buildZip(target: File) {
        val rawRevisions = repository.revisions()
        val heads = repository.heads().associate { it.entityId to it.revisionId }
        val assets = repository.assets()
        val assetsById = assets.associateBy { it.id }
        val revisions = rawRevisions.map { revision ->
            val asset = assetsById[revision.entityId]
            if (asset == null) revision else revision.copy(
                payload = JsonObject(
                    revision.payload + ("archive_path" to json.parseToJsonElement(
                        json.encodeToString("assets/${asset.sha256}${asset.extension}")
                    ))
                )
            )
        }
        val grouped = revisions.groupBy { it.entityKind.name.lowercase() }
        val content = grouped.mapValues { (_, values) ->
            (values.joinToString("\n") { json.encodeToString(it) } + "\n").toByteArray()
        }
        val manifest = buildJsonObject {
            put("format", "mealcircuit.portable")
            put("format_version", 1)
            put("domain_schema_version", 1)
            put("application_version", "0.3.0")
            put("created_at", Instant.now().toString())
            put("entity_heads", json.parseToJsonElement(json.encodeToString(heads)))
            put("content", buildJsonObject {
                content.forEach { (kind, bytes) ->
                    put("entities/$kind.jsonl", buildJsonObject {
                        put("count", grouped.getValue(kind).size)
                        put("sha256", bytes.sha256())
                    })
                }
            })
            put("assets", buildJsonArray {
                assets.forEach { asset ->
                    add(buildJsonObject {
                        put("id", asset.id)
                        put("sha256", asset.sha256)
                        put("path", "assets/${asset.sha256}${asset.extension}")
                        put("bytes", asset.byteCount)
                        put("media_type", asset.mediaType)
                    })
                }
            })
        }
        ZipOutputStream(target.outputStream().buffered()).use { zip ->
            zip.putNextEntry(ZipEntry("manifest.json"))
            zip.write(manifest.toString().toByteArray())
            zip.closeEntry()
            content.forEach { (kind, bytes) ->
                zip.putNextEntry(ZipEntry("entities/$kind.jsonl"))
                zip.write(bytes)
                zip.closeEntry()
            }
            assets.forEach { asset ->
                val relative = asset.relativePath ?: error("Asset ${asset.id} is not downloaded")
                val file = context.filesDir.resolve(relative)
                require(file.isFile && file.readBytes().sha256() == asset.sha256)
                zip.putNextEntry(ZipEntry("assets/${asset.sha256}${asset.extension}"))
                file.inputStream().use { it.copyTo(zip) }
                zip.closeEntry()
            }
        }
    }

    private data class Parsed(
        val revisions: List<DomainRevision>,
        val heads: Map<String, String>,
        val assets: List<JsonObject>,
    )

    private fun readValidated(file: File): Parsed = ZipFile(file).use { zip ->
        val entries = zip.entries().toList()
        require(entries.size <= 100_000)
        require(entries.map { it.name }.distinct().size == entries.size)
        require(entries.sumOf { it.size.coerceAtLeast(0) } <= MAX_ARCHIVE_BYTES)
        entries.forEach { entry ->
            require(!entry.name.startsWith('/') && ".." !in entry.name.split('/') && '\\' !in entry.name)
            val entryLimit = if (entry.name.startsWith("assets/")) {
                MAX_MANAGED_ASSET_BYTES.toLong()
            } else {
                MAX_METADATA_ENTRY_BYTES.toLong()
            }
            require(entry.size in 0..entryLimit)
            require(entry.compressedSize <= 0 || entry.size <= entry.compressedSize * 1000)
        }
        val manifest = zip.getInputStream(zip.getEntry("manifest.json") ?: error("Missing manifest")).use {
            json.parseToJsonElement(it.readBounded(MAX_MANIFEST_BYTES).decodeToString()).jsonObject
        }
        require(manifest["format"]?.jsonPrimitive?.content == "mealcircuit.portable")
        require(manifest["format_version"]?.jsonPrimitive?.content == "1")
        val content = manifest.getValue("content").jsonObject
        val revisions = mutableListOf<DomainRevision>()
        content.forEach { (path, descriptorValue) ->
            val bytes = zip.getInputStream(zip.getEntry(path) ?: error("Missing $path")).use {
                it.readBounded(MAX_METADATA_ENTRY_BYTES)
            }
            require(bytes.sha256() == descriptorValue.jsonObject.getValue("sha256").jsonPrimitive.content)
            val lines = bytes.decodeToString().lineSequence().filter(String::isNotBlank).toList()
            require(lines.size == descriptorValue.jsonObject.getValue("count").jsonPrimitive.content.toInt())
            lines.forEach {
                revisions += json.decodeFromString<DomainRevision>(it).validate()
            }
        }
        require(revisions.map { it.revisionId }.distinct().size == revisions.size)
        val revisionIds = revisions.map { it.revisionId }.toSet()
        require(revisions.all { revisionIds.containsAll(it.parentRevisionIds) })
        val byRevision = revisions.associateBy { it.revisionId }
        val visiting = mutableSetOf<String>()
        val visited = mutableSetOf<String>()
        fun visit(id: String) {
            if (id in visited) return
            require(visiting.add(id)) { "Revision graph contains a cycle" }
            byRevision.getValue(id).parentRevisionIds.forEach(::visit)
            visiting.remove(id)
            visited.add(id)
        }
        revisionIds.forEach(::visit)
        val heads = json.decodeFromJsonElement<Map<String, String>>(manifest.getValue("entity_heads"))
        require(heads.all { (entity, revision) -> revisions.any { it.entityId == entity && it.revisionId == revision } })
        val assets = manifest.getValue("assets") as kotlinx.serialization.json.JsonArray
        val assetDescriptors = assets.map { it.jsonObject }
        require(assetDescriptors.map { it.getValue("id").jsonPrimitive.content }.distinct().size == assetDescriptors.size)
        require(assetDescriptors.map { it.getValue("path").jsonPrimitive.content }.distinct().size == assetDescriptors.size)
        assetDescriptors.forEach { descriptor ->
            val path = descriptor.getValue("path").jsonPrimitive.content
            require(path.startsWith("assets/") && zip.getEntry(path) != null)
            val bytes = zip.getInputStream(zip.getEntry(path)).use { it.readBounded(MAX_MANAGED_ASSET_BYTES) }
            require(bytes.size.toLong() == descriptor.getValue("bytes").jsonPrimitive.content.toLong())
            require(bytes.sha256() == descriptor.getValue("sha256").jsonPrimitive.content)
        }
        val assetIds = revisions.filter { it.entityKind.name == "ASSET" }.map { it.entityId }.toSet()
        fun referencedAssets(value: kotlinx.serialization.json.JsonElement): Sequence<String> = sequence {
            when (value) {
                is JsonObject -> for ((key, child) in value) {
                    if (key.endsWith("asset_id") && child is kotlinx.serialization.json.JsonPrimitive && child.isString) {
                        yield(child.content)
                    }
                    yieldAll(referencedAssets(child))
                }
                is kotlinx.serialization.json.JsonArray -> for (child in value) yieldAll(referencedAssets(child))
                else -> Unit
            }
        }
        require(revisions.flatMap { referencedAssets(it.payload).toList() }.all { it in assetIds })
        Parsed(revisions, heads, assetDescriptors)
    }

    private suspend fun extractAssets(file: File, parsed: Parsed): List<File> = ZipFile(file).use { zip ->
        val created = mutableListOf<File>()
        val assetRevisions = parsed.revisions.filter { it.entityKind.name == "ASSET" }
        parsed.assets.forEach { descriptor ->
            val path = descriptor.getValue("path").jsonPrimitive.content
            val digest = descriptor.getValue("sha256").jsonPrimitive.content
            val bytes = zip.getInputStream(zip.getEntry(path) ?: error("Missing asset")).use {
                it.readBounded(MAX_MANAGED_ASSET_BYTES)
            }
            require(bytes.sha256() == digest)
            val extension = path.substringAfterLast(digest)
            val relative = "assets/$digest$extension"
            val target = context.filesDir.resolve(relative)
            target.parentFile?.mkdirs()
            if (target.exists()) require(target.readBytes().sha256() == digest)
            else {
                target.writeBytes(bytes)
                created += target
            }
            val revision = assetRevisions.firstOrNull {
                it.payload["archive_path"]?.jsonPrimitive?.content == path
            }
            repository.putAsset(
                ManagedAssetEntity(
                    descriptor["id"]?.jsonPrimitive?.content ?: revision?.entityId ?: "asset_$digest",
                    digest,
                    descriptor["media_type"]?.jsonPrimitive?.content
                        ?: revision?.payload?.get("media_type")?.jsonPrimitive?.content
                        ?: "application/octet-stream",
                    extension,
                    bytes.size.toLong(),
                    relative,
                    false,
                    revision?.createdAt ?: Instant.now().toString(),
                )
            )
        }
        created
    }

    private suspend fun commonAncestor(
        local: DomainRevision,
        remote: DomainRevision,
        incoming: Map<String, DomainRevision>,
    ): DomainRevision? {
        val graph = incoming.toMutableMap()
        repository.revisions().forEach { graph[it.revisionId] = it }
        fun distances(start: String): Map<String, Int> {
            val result = mutableMapOf(start to 0)
            val queue = ArrayDeque(listOf(start))
            while (queue.isNotEmpty()) {
                val current = queue.removeFirst()
                graph[current]?.parentRevisionIds.orEmpty().forEach { parent ->
                    if (parent !in result) { result[parent] = result.getValue(current) + 1; queue.add(parent) }
                }
            }
            return result
        }
        val left = distances(local.revisionId)
        val right = distances(remote.revisionId)
        return (left.keys intersect right.keys).minByOrNull { left.getValue(it) + right.getValue(it) }?.let(graph::get)
    }

    private suspend fun recordConflict(
        local: DomainRevision,
        remote: DomainRevision,
        base: DomainRevision?,
        paths: List<String>,
    ) {
        repository.commitSyncConflict(
            SyncConflictEntity(
                DomainRevision.id("conflict"), local.entityId, local.entityKind.name.lowercase(),
                base?.let { json.encodeToString(it) }, json.encodeToString(local), json.encodeToString(remote),
                json.encodeToString(paths), "unresolved", Instant.now().toString(), null,
            ),
            local.entityId,
        )
    }

    private fun materializeArchive(input: InputStream, recoveryKey: String?): File {
        val source = File.createTempFile("mealcircuit-source-", ".bin", context.cacheDir)
        input.use { stream ->
            source.outputStream().use { output ->
                stream.copyToBounded(output, MAX_ARCHIVE_BYTES + MAX_ENCRYPTED_OVERHEAD_BYTES)
            }
        }
        if (!source.inputStream().use { it.readUpTo(5).contentEquals("MCX1\n".toByteArray()) }) return source
        require(recoveryKey != null) { "Recovery key required" }
        val target = File.createTempFile("mealcircuit-plain-", ".zip", context.cacheDir)
        decryptMcx(source, target, parseRecoveryKey(recoveryKey))
        source.delete()
        return target
    }

    private fun encryptMcx(source: File, output: OutputStream, secret: ByteArray) {
        val salt = ByteArray(32).also(SecureRandom()::nextBytes)
        val key = hkdf(secret, salt, "mealcircuit-portable-v1".toByteArray())
        val header = buildJsonObject {
            put("format", "mealcircuit.mcx"); put("version", 1); put("algorithm", "AES-256-GCM")
            put("kdf", "HKDF-SHA256"); put("salt", Base64.getEncoder().encodeToString(salt)); put("chunk_bytes", CHUNK)
        }.toString().toByteArray()
        DataOutputStream(output.buffered()).use { data ->
            data.write("MCX1\n".toByteArray()); data.write(header); data.writeByte('\n'.code)
            source.inputStream().buffered().use { stream ->
                var index = 0L
                while (true) {
                    val block = stream.readUpTo(CHUNK)
                    if (block.isEmpty()) break
                    val nonce = ByteArray(12).also(SecureRandom()::nextBytes)
                    val cipher = crypt(Cipher.ENCRYPT_MODE, key, nonce, blobAad(header, index), block)
                    data.writeInt(cipher.size); data.write(nonce); data.write(cipher); index += 1
                }
            }
            data.writeInt(0)
        }
    }

    private fun decryptMcx(source: File, target: File, secret: ByteArray) {
        DataInputStream(source.inputStream().buffered()).use { data ->
            require(data.readUpTo(5).contentEquals("MCX1\n".toByteArray()))
            val headerBytes = mutableListOf<Byte>()
            while (true) {
                val value = data.read()
                require(value >= 0) { "truncated MCX header" }
                if (value == '\n'.code) break
                require(headerBytes.size < MAX_MCX_HEADER_BYTES) { "MCX header too large" }
                headerBytes += value.toByte()
            }
            val header = headerBytes.toByteArray()
            val headerJson = json.parseToJsonElement(header.decodeToString()).jsonObject
            val salt = Base64.getDecoder().decode(headerJson.getValue("salt").jsonPrimitive.content)
            val key = hkdf(secret, salt, "mealcircuit-portable-v1".toByteArray())
            target.outputStream().buffered().use { output ->
                var index = 0L
                var total = 0L
                while (true) {
                    val size = data.readInt()
                    if (size == 0) break
                    require(size in 17..CHUNK + 16)
                    val nonce = data.readUpTo(12)
                    val cipher = data.readUpTo(size)
                    require(nonce.size == 12 && cipher.size == size) { "truncated MCX chunk" }
                    val plain = crypt(Cipher.DECRYPT_MODE, key, nonce, blobAad(header, index), cipher)
                    total += plain.size
                    require(total <= MAX_ARCHIVE_BYTES) { "decrypted archive too large" }
                    output.write(plain)
                    index += 1
                }
                require(data.read() == -1)
            }
        }
    }

    private fun blobAad(header: ByteArray, index: Long): ByteArray =
        "MealCircuit Portable v1\u0000".toByteArray() + header + ByteArray(8) { shift ->
            (index ushr (56 - shift * 8)).toByte()
        }

    private fun crypt(mode: Int, key: ByteArray, nonce: ByteArray, aad: ByteArray, value: ByteArray) =
        Cipher.getInstance("AES/GCM/NoPadding").run {
            init(mode, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce)); updateAAD(aad); doFinal(value)
        }

    private fun ByteArray.sha256() = MessageDigest.getInstance("SHA-256").digest(this).joinToString("") { "%02x".format(it) }

    companion object {
        private const val CHUNK = 4 * 1024 * 1024
        private const val MAX_MANIFEST_BYTES = 8 * 1024 * 1024
        private const val MAX_METADATA_ENTRY_BYTES = 64 * 1024 * 1024
        private const val MAX_MCX_HEADER_BYTES = 64 * 1024
        private const val MAX_ARCHIVE_BYTES = 10L * 1024 * 1024 * 1024
        private const val MAX_ENCRYPTED_OVERHEAD_BYTES = 64L * 1024 * 1024
    }
}
