package org.mealcircuit.app.portable

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import org.mealcircuit.app.data.DomainRepository
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.data.SyncConflictEntity
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.domain.threeWayMerge
import org.mealcircuit.app.io.readUpTo
import org.mealcircuit.app.io.readBounded
import org.mealcircuit.app.io.copyToBounded
import org.mealcircuit.app.io.MAX_MANAGED_ASSET_BYTES
import org.mealcircuit.app.sync.formatRecoveryKey
import org.mealcircuit.app.sync.hkdf
import org.mealcircuit.app.sync.parseRecoveryKey
import org.mealcircuit.app.sync.referencedAssetIds
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
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
internal data class PortableImportStaging(val source: File, val preview: ImportPreview)

internal data class PortableAssetBinding(
    val id: String,
    val sha256: String,
    val path: String,
    val byteCount: Long,
    val mediaType: String,
    val extension: String,
    val revision: DomainRevision,
)

private enum class RevisionVisitState { VISITING, VISITED }

private data class RevisionGraphFrame(
    val revisionId: String,
    val exiting: Boolean,
)

/** Validates the complete portable revision graph without consuming the thread stack. */
internal fun validatePortableRevisionGraph(revisions: List<DomainRevision>) {
    val byRevision = revisions.associateBy(DomainRevision::revisionId)
    require(byRevision.size == revisions.size) { "Portable Data 包含重复 revision" }
    val revisionIds = byRevision.keys
    revisions.forEach { revision ->
        require(revision.parentRevisionIds.all { parentId ->
            byRevision[parentId]?.let { parent ->
                parent.entityId == revision.entityId && parent.entityKind == revision.entityKind
            } == true
        }) {
            "revision 父版本缺失或属于其他实体"
        }
    }

    val states = HashMap<String, RevisionVisitState>(byRevision.size)
    for (startId in revisionIds) {
        if (states[startId] == RevisionVisitState.VISITED) continue
        val stack = ArrayDeque<RevisionGraphFrame>()
        stack.addLast(RevisionGraphFrame(startId, exiting = false))
        while (stack.isNotEmpty()) {
            val frame = stack.removeLast()
            if (frame.exiting) {
                states[frame.revisionId] = RevisionVisitState.VISITED
                continue
            }
            when (states[frame.revisionId]) {
                RevisionVisitState.VISITED -> continue
                RevisionVisitState.VISITING -> throw IllegalArgumentException("修订关系图包含循环")
                null -> Unit
            }
            states[frame.revisionId] = RevisionVisitState.VISITING
            stack.addLast(RevisionGraphFrame(frame.revisionId, exiting = true))
            val parents = byRevision.getValue(frame.revisionId).parentRevisionIds
            for (index in parents.indices.reversed()) {
                val parentId = parents[index]
                when (states[parentId]) {
                    RevisionVisitState.VISITING -> throw IllegalArgumentException("修订关系图包含循环")
                    RevisionVisitState.VISITED -> Unit
                    null -> stack.addLast(RevisionGraphFrame(parentId, exiting = false))
                }
            }
        }
    }
}

internal fun portableStorageRevision(revision: DomainRevision): DomainRevision {
    if (revision.entityKind != EntityKind.ASSET || "archive_path" !in revision.payload) return revision
    return revision.copy(payload = JsonObject(revision.payload - "archive_path")).validate()
}

/**
 * Binds every manifest asset descriptor to the authoritative ASSET head it represents.
 * Keeping this validation pure makes the archive boundary independently testable.
 */
internal fun bindPortableAssetDescriptors(
    revisions: List<DomainRevision>,
    heads: Map<String, String>,
    descriptors: List<JsonObject>,
): List<PortableAssetBinding> {
    val revisionsById = revisions.associateBy(DomainRevision::revisionId)
    require(revisionsById.size == revisions.size) { "Portable Data 包含重复 revision" }
    val assetHeads = heads.mapNotNull { (entityId, revisionId) ->
        val revision = revisionsById[revisionId]
            ?: throw IllegalArgumentException("Portable Data 的 head revision 不存在：$revisionId")
        require(revision.entityId == entityId) { "Portable Data 的 head 与实体不匹配：$entityId" }
        revision.takeIf { it.entityKind == EntityKind.ASSET }
    }.associateBy(DomainRevision::entityId)

    fun JsonElement.requiredString(field: String): String {
        val primitive = this as? JsonPrimitive
            ?: throw IllegalArgumentException("资产字段 $field 必须是字符串")
        require(primitive.isString) { "资产字段 $field 必须是字符串" }
        return primitive.content
    }

    val seenIds = mutableSetOf<String>()
    val seenPaths = mutableSetOf<String>()
    val bindings = descriptors.map { descriptor ->
        val legacy = descriptor.keys == LEGACY_PORTABLE_ASSET_DESCRIPTOR_FIELDS
        require(legacy || descriptor.keys == PORTABLE_ASSET_DESCRIPTOR_FIELDS) {
            "资产 descriptor 字段必须精确匹配受支持的协议"
        }
        val sha256 = descriptor.getValue("sha256").requiredString("sha256")
        val path = descriptor.getValue("path").requiredString("path")
        val bytesPrimitive = descriptor.getValue("bytes") as? JsonPrimitive
            ?: throw IllegalArgumentException("资产字段 bytes 必须是整数")
        require(!bytesPrimitive.isString) { "资产字段 bytes 必须是整数" }
        val byteCount = bytesPrimitive.longOrNull
            ?: throw IllegalArgumentException("资产字段 bytes 必须是整数")
        val revision = if (legacy) {
            val candidates = assetHeads.values.filter { candidate ->
                val payload = candidate.payload
                payload["sha256"]?.jsonPrimitive?.content == sha256 &&
                    payload["byte_count"]?.jsonPrimitive?.longOrNull == byteCount &&
                    payload["archive_path"]?.jsonPrimitive?.content == path
            }
            require(candidates.size == 1) {
                "旧版资产 descriptor 无法唯一绑定到 ASSET head：$path"
            }
            candidates.single()
        } else {
            val id = descriptor.getValue("id").requiredString("id")
            assetHeads[id] ?: throw IllegalArgumentException("资产 descriptor 没有对应的 ASSET head：$id")
        }
        val id = revision.entityId
        val mediaType = if (legacy) {
            revision.payload.getValue("media_type").requiredString("media_type")
        } else {
            descriptor.getValue("media_type").requiredString("media_type")
        }
        require(seenIds.add(id)) { "Portable Data 包含重复资产 ID：$id" }
        require(seenPaths.add(path)) { "Portable Data 包含重复资产路径：$path" }

        val payload = revision.payload
        val payloadSha256 = payload.getValue("sha256").requiredString("sha256")
        val payloadMediaType = payload.getValue("media_type").requiredString("media_type")
        val extension = payload.getValue("extension").requiredString("extension")
        val payloadByteCount = (payload.getValue("byte_count") as? JsonPrimitive)
            ?.takeUnless { it.isString }
            ?.longOrNull
            ?: throw IllegalArgumentException("ASSET head 的 byte_count 必须是整数")
        val archivePath = payload["archive_path"]?.requiredString("archive_path")
            ?: throw IllegalArgumentException("ASSET head 缺少 archive_path：$id")
        val expectedPath = "assets/$payloadSha256$extension"

        require(sha256 == payloadSha256) { "资产 descriptor 的 sha256 与 ASSET head 不一致：$id" }
        require(mediaType == payloadMediaType) { "资产 descriptor 的 media_type 与 ASSET head 不一致：$id" }
        require(byteCount == payloadByteCount) { "资产 descriptor 的 bytes 与 ASSET head 不一致：$id" }
        require(path == expectedPath && archivePath == expectedPath) {
            "资产 descriptor 的 path/archive_path 与 ASSET head 不一致：$id"
        }
        PortableAssetBinding(id, sha256, path, byteCount, mediaType, extension, revision)
    }
    require(seenIds == assetHeads.keys) { "资产 descriptor 与 ASSET heads 不是一一对应关系" }
    return bindings
}

internal fun requirePortableAssetReferences(
    revisions: Iterable<DomainRevision>,
    availableAssetIds: Set<String>,
) {
    val missing = referencedAssetIds(revisions) - availableAssetIds
    require(missing.isEmpty()) { "领域实体引用了缺失资产：${missing.sorted()}" }
}

private val PORTABLE_ASSET_DESCRIPTOR_FIELDS = setOf("id", "sha256", "path", "bytes", "media_type")
private val LEGACY_PORTABLE_ASSET_DESCRIPTOR_FIELDS = setOf("sha256", "path", "bytes")

class PortableData(
    private val context: Context,
    private val repository: DomainRepository,
    private val json: Json = repository.json,
) {
    init {
        context.cacheDir.listFiles()?.filter { file ->
            file.name.startsWith("mealcircuit-source-") || file.name.startsWith("mealcircuit-plain-")
        }?.forEach(File::delete)
    }

    fun createRecoveryKey(): String = formatRecoveryKey(ByteArray(32).also(SecureRandom()::nextBytes))

    internal suspend fun previewAndStage(
        input: InputStream,
        recoveryKey: String?,
        mode: ImportMode,
    ): PortableImportStaging = withContext(Dispatchers.IO) {
        val source = File.createTempFile("mealcircuit-source-", ".portable", context.cacheDir)
        try {
            FileOutputStream(source).use { output ->
                input.copyToBounded(output, MAX_ARCHIVE_BYTES + MAX_ENCRYPTED_OVERHEAD_BYTES)
                output.fd.sync()
            }
            val preview = source.inputStream().use { staged ->
                preview(staged, recoveryKey, mode)
            }
            PortableImportStaging(source, preview)
        } catch (error: Throwable) {
            source.delete()
            throw error
        }
    }

    internal suspend fun importStaged(
        staging: PortableImportStaging,
        recoveryKey: String?,
        mode: ImportMode,
    ): ImportPreview = withContext(Dispatchers.IO) {
        val source = requireStagedSource(staging.source)
        source.inputStream().use { input -> import(input, recoveryKey, mode) }
    }

    internal fun discardStaged(staging: PortableImportStaging) {
        val source = requireStagedSource(staging.source, requireExists = false)
        check(!source.exists() || source.delete()) { "临时导入数据包清理失败" }
    }

    suspend fun export(
        output: OutputStream,
        encrypted: Boolean = true,
        recoveryKey: String? = null,
    ): String? = withContext(Dispatchers.IO) {
        repository.withMutationGate {
            val plain = File.createTempFile("mealcircuit-", ".zip", context.cacheDir)
            try {
                buildZip(plain)
                if (!encrypted) {
                    plain.inputStream().use { it.copyTo(output) }
                    null
                } else {
                    val key = recoveryKey ?: createRecoveryKey()
                    val secret = parseRecoveryKey(key)
                    encryptMcx(plain, output, secret)
                    key
                }
            } finally {
                plain.delete()
            }
        }
    }

    suspend fun preview(input: InputStream, recoveryKey: String?, mode: ImportMode): ImportPreview = withContext(Dispatchers.IO) {
        repository.withMutationGate {
            val archive = materializeArchive(input, recoveryKey)
            try {
                val parsed = readValidated(archive)
                calculatePreview(parsed, mode)
            } finally {
                archive.delete()
            }
        }
    }

    suspend fun import(input: InputStream, recoveryKey: String?, mode: ImportMode): ImportPreview = withContext(Dispatchers.IO) {
        repository.withMutationGate {
            val archive = materializeArchive(input, recoveryKey)
            val createdFiles = mutableListOf<File>()
            try {
                val parsed = readValidated(archive)
                val preview = calculatePreview(parsed, mode)
                val storageRevisions = parsed.revisions.map(::portableStorageRevision)
                repository.importTransaction {
                    createdFiles += extractAssets(archive, parsed)
                    storageRevisions.forEach { repository.storeRevision(it, materialize = false) }
                    val byId = storageRevisions.associateBy { it.revisionId }
                    for ((entityId, revisionId) in parsed.heads) {
                        val remote = byId.getValue(revisionId)
                        val localHead = repository.heads().firstOrNull { it.entityId == entityId }
                        if (localHead == null) {
                            repository.commitRevision(remote, queue = true)
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
                    require(storedIds.containsAll(storageRevisions.map { it.revisionId }))
                    if (mode == ImportMode.RESTORE) {
                        val roundTrip = File.createTempFile("mealcircuit-roundtrip-", ".zip", context.cacheDir)
                        try {
                            buildZip(roundTrip)
                            val restored = readValidated(roundTrip)
                            require(restored.heads == parsed.heads)
                            require(restored.revisions.map { it.revisionId }.toSet() == parsed.revisions.map { it.revisionId }.toSet())
                            require(restored.assets.map { it.sha256 }.toSet() == parsed.assets.map { it.sha256 }.toSet())
                        } finally {
                            roundTrip.delete()
                        }
                    }
                }
                preview
            } catch (error: Throwable) {
                try {
                    withContext(NonCancellable) {
                        repository.cleanupUnreferencedAssetFiles(context.filesDir, createdFiles)
                    }
                } catch (cleanupError: Throwable) {
                    if (cleanupError !== error) error.addSuppressed(cleanupError)
                }
                throw error
            } finally {
                archive.delete()
            }
        }
    }

    private suspend fun calculatePreview(parsed: Parsed, mode: ImportMode): ImportPreview {
        val local = repository.heads().associateBy { it.entityId }
        val conflicts = parsed.heads.count { (entityId, revisionId) ->
            val current = local[entityId] ?: return@count false
            current.revisionId != revisionId
        }
        require(mode == ImportMode.MERGE || local.isEmpty()) { "恢复目标并非空目录" }
        return ImportPreview(parsed.heads.size, parsed.revisions.size, parsed.assets.size, conflicts)
    }

    private fun requireStagedSource(source: File, requireExists: Boolean = true): File {
        val cacheRoot = context.cacheDir.canonicalFile
        val canonical = source.canonicalFile
        require(
            canonical.parentFile == cacheRoot && canonical.name.startsWith("mealcircuit-source-")
        ) { "临时导入数据包路径无效" }
        if (requireExists) require(canonical.isFile) { "临时导入数据包已失效，请重新预检" }
        return canonical
    }

    private suspend fun buildZip(target: File) {
        val rawRevisions = repository.revisions()
        val heads = repository.heads().associate { it.entityId to it.revisionId }
        val rawByRevisionId = rawRevisions.associateBy(DomainRevision::revisionId)
        val storedAssetsById = repository.assets().associateBy(ManagedAssetEntity::id)
        val assetHeadIds = heads.entries.mapNotNull { (entityId, revisionId) ->
            rawByRevisionId[revisionId]?.takeIf { it.entityKind == EntityKind.ASSET }?.let { entityId }
        }.sorted()
        val assets = assetHeadIds.map { id ->
            storedAssetsById[id] ?: error("资产 $id 缺少受管文件记录")
        }
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
        val assetDescriptors = assets.map { asset ->
            buildJsonObject {
                put("id", asset.id)
                put("sha256", asset.sha256)
                put("path", "assets/${asset.sha256}${asset.extension}")
                put("bytes", asset.byteCount)
                put("media_type", asset.mediaType)
            }
        }
        bindPortableAssetDescriptors(revisions, heads, assetDescriptors)
        requirePortableAssetReferences(revisions, assetDescriptors.mapTo(mutableSetOf()) {
            it.getValue("id").jsonPrimitive.content
        })
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
            put("assets", buildJsonArray { assetDescriptors.forEach(::add) })
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
                val relative = asset.relativePath ?: error("资源 ${asset.id} 尚未下载")
                val filesRoot = context.filesDir.canonicalFile
                val managedRoot = File(filesRoot, "assets").canonicalFile
                require(managedRoot.parentFile == filesRoot) { "受管资产目录不可用" }
                val expected = File(managedRoot, "${asset.sha256}${asset.extension}").canonicalFile
                val file = File(filesRoot, relative).canonicalFile
                require(file == expected && file.parentFile == managedRoot) { "资产路径逃逸受管目录" }
                require(
                    file.isFile && file.length() == asset.byteCount && file.sha256() == asset.sha256
                ) { "资源 ${asset.id} 的文件与认证元数据不一致" }
                zip.putNextEntry(ZipEntry("assets/${asset.sha256}${asset.extension}"))
                file.inputStream().use { it.copyTo(zip) }
                zip.closeEntry()
            }
        }
    }

    private data class Parsed(
        val revisions: List<DomainRevision>,
        val heads: Map<String, String>,
        val assets: List<PortableAssetBinding>,
    )

    private fun readValidated(file: File): Parsed = ZipFile(file).use { zip ->
        val entries = zip.entries().toList()
        require(entries.size <= MAX_ARCHIVE_ENTRIES)
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
            require(entry.compressedSize <= 0 || entry.size <= entry.compressedSize * MAX_COMPRESSION_RATIO)
        }
        val manifest = zip.getInputStream(zip.getEntry("manifest.json") ?: error("导入包缺少清单")).use {
            json.parseToJsonElement(it.readBounded(MAX_MANIFEST_BYTES).decodeToString()).jsonObject
        }
        require(manifest["format"]?.jsonPrimitive?.content == "mealcircuit.portable")
        require(manifest["format_version"]?.jsonPrimitive?.content == "1")
        val content = manifest.getValue("content").jsonObject
        val revisions = mutableListOf<DomainRevision>()
        content.forEach { (path, descriptorValue) ->
            val bytes = zip.getInputStream(zip.getEntry(path) ?: error("导入包缺少 $path")).use {
                it.readBounded(MAX_METADATA_ENTRY_BYTES)
            }
            require(bytes.sha256() == descriptorValue.jsonObject.getValue("sha256").jsonPrimitive.content)
            val lines = bytes.decodeToString().lineSequence().filter(String::isNotBlank).toList()
            require(lines.size == descriptorValue.jsonObject.getValue("count").jsonPrimitive.content.toInt())
            require(revisions.size + lines.size <= MAX_REVISIONS)
            lines.forEach {
                revisions += json.decodeFromString<DomainRevision>(it).validate()
            }
        }
        validatePortableRevisionGraph(revisions)
        val heads = json.decodeFromJsonElement<Map<String, String>>(manifest.getValue("entity_heads"))
        require(heads.size <= MAX_ENTITIES)
        val revisionsById = revisions.associateBy(DomainRevision::revisionId)
        require(heads.all { (entity, revisionId) -> revisionsById[revisionId]?.entityId == entity })
        val assets = manifest.getValue("assets") as kotlinx.serialization.json.JsonArray
        val assetDescriptors = assets.map { it.jsonObject }
        require(assetDescriptors.size <= MAX_ASSETS)
        val assetBindings = bindPortableAssetDescriptors(revisions, heads, assetDescriptors)
        val archiveAssetPaths = entries.asSequence()
            .filterNot { it.isDirectory }
            .map { it.name }
            .filter { it.startsWith("assets/") }
            .toSet()
        require(archiveAssetPaths == assetBindings.map(PortableAssetBinding::path).toSet()) {
            "归档中的资产文件必须与 manifest descriptor 精确对应"
        }
        assetBindings.forEach { binding ->
            val path = binding.path
            require(zip.getEntry(path) != null)
            val bytes = zip.getInputStream(zip.getEntry(path)).use { it.readBounded(MAX_MANAGED_ASSET_BYTES) }
            require(bytes.size.toLong() == binding.byteCount)
            require(bytes.sha256() == binding.sha256)
        }
        requirePortableAssetReferences(revisions, assetBindings.mapTo(mutableSetOf(), PortableAssetBinding::id))
        Parsed(revisions, heads, assetBindings)
    }

    private suspend fun extractAssets(file: File, parsed: Parsed): List<File> = ZipFile(file).use { zip ->
        val created = mutableListOf<File>()
        val localAssets = repository.assets()
        val localById = localAssets.associateBy(ManagedAssetEntity::id)
        val localByDigest = localAssets.associateBy(ManagedAssetEntity::sha256)
        val localAssetHeads = repository.heads().mapNotNull { head ->
            repository.revision(head.revisionId)?.takeIf { it.entityKind == EntityKind.ASSET }
        }.associateBy(DomainRevision::entityId)
        parsed.assets.forEach { binding ->
            localById[binding.id]?.let { existing ->
                require(
                    existing.sha256 == binding.sha256 &&
                        existing.mediaType == binding.mediaType &&
                        existing.extension == binding.extension &&
                        existing.byteCount == binding.byteCount
                ) { "本机资产 ${binding.id} 的元数据与导入包不一致" }
            }
            localByDigest[binding.sha256]?.let { existing ->
                require(existing.id == binding.id) {
                    "导入资产 ${binding.id} 的哈希已属于另一项本机资产"
                }
            }
            localAssetHeads[binding.id]?.let { existing ->
                val payload = existing.payload
                require(
                    payload["sha256"]?.jsonPrimitive?.content == binding.sha256 &&
                        payload["media_type"]?.jsonPrimitive?.content == binding.mediaType &&
                        payload["extension"]?.jsonPrimitive?.content == binding.extension &&
                        payload["byte_count"]?.jsonPrimitive?.longOrNull == binding.byteCount
                ) { "本机资产 ${binding.id} 的活动修订与导入包元数据不一致" }
            }
        }

        val filesRoot = context.filesDir.canonicalFile
        val root = File(filesRoot, "assets").apply { mkdirs() }.canonicalFile
        require(root.isDirectory && root.parentFile == filesRoot) { "受管资产目录不可用" }
        try {
            parsed.assets.forEach { binding ->
                val relative = binding.path
                val target = File(filesRoot, relative).canonicalFile
                require(target.parentFile == root && target.name == "${binding.sha256}${binding.extension}") {
                    "资产路径逃逸受管目录"
                }
                if (target.exists()) {
                    require(
                        target.isFile && target.length() == binding.byteCount && target.sha256() == binding.sha256
                    ) { "本机资产文件与导入包冲突：${binding.id}" }
                } else {
                    val temporary = File.createTempFile(".${binding.sha256}.", ".part", root)
                    try {
                        val bytes = zip.getInputStream(
                            zip.getEntry(binding.path) ?: error("导入包缺少资产：${binding.path}")
                        ).use { it.readBounded(MAX_MANAGED_ASSET_BYTES) }
                        require(bytes.size.toLong() == binding.byteCount && bytes.sha256() == binding.sha256) {
                            "导入资产校验失败：${binding.id}"
                        }
                        FileOutputStream(temporary).use { output ->
                            output.write(bytes)
                            output.fd.sync()
                        }
                        Files.move(
                            temporary.toPath(),
                            target.toPath(),
                            StandardCopyOption.ATOMIC_MOVE,
                            StandardCopyOption.REPLACE_EXISTING,
                        )
                        created += target
                    } finally {
                        temporary.delete()
                    }
                }
                repository.putAsset(
                    ManagedAssetEntity(
                        binding.id,
                        binding.sha256,
                        binding.mediaType,
                        binding.extension,
                        binding.byteCount,
                        relative,
                        false,
                        binding.revision.createdAt,
                    )
                )
            }
            created
        } catch (error: Throwable) {
            created.forEach(File::delete)
            throw error
        }
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
        var target: File? = null
        var completed = false
        try {
            input.use { stream ->
                source.outputStream().use { output ->
                    stream.copyToBounded(output, MAX_ARCHIVE_BYTES + MAX_ENCRYPTED_OVERHEAD_BYTES)
                }
            }
            if (!source.inputStream().use { it.readUpTo(5).contentEquals("MCX1\n".toByteArray()) }) {
                completed = true
                return source
            }
            require(recoveryKey != null) { "需要恢复密钥" }
            target = File.createTempFile("mealcircuit-plain-", ".zip", context.cacheDir)
            decryptMcx(source, target, parseRecoveryKey(recoveryKey))
            check(source.delete()) { "无法删除加密导入暂存文件" }
            completed = true
            return target
        } finally {
            if (!completed) {
                source.delete()
                target?.delete()
            }
        }
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
                require(value >= 0) { "MCX 文件头不完整" }
                if (value == '\n'.code) break
                require(headerBytes.size < MAX_MCX_HEADER_BYTES) { "MCX 文件头过大" }
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
                    require(nonce.size == 12 && cipher.size == size) { "MCX 加密分块不完整" }
                    val plain = crypt(Cipher.DECRYPT_MODE, key, nonce, blobAad(header, index), cipher)
                    total += plain.size
                    require(total <= MAX_ARCHIVE_BYTES) { "解密后的数据包过大" }
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

    private fun File.sha256(): String {
        val digest = MessageDigest.getInstance("SHA-256")
        inputStream().buffered().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    companion object {
        private const val CHUNK = 4 * 1024 * 1024
        private const val MAX_MANIFEST_BYTES = 2 * 1024 * 1024
        private const val MAX_METADATA_ENTRY_BYTES = 16 * 1024 * 1024
        private const val MAX_MCX_HEADER_BYTES = 64 * 1024
        private const val MAX_ARCHIVE_BYTES = 1024L * 1024 * 1024
        private const val MAX_ENCRYPTED_OVERHEAD_BYTES = 16L * 1024 * 1024
        private const val MAX_ARCHIVE_ENTRIES = 10_000
        private const val MAX_REVISIONS = 20_000
        private const val MAX_ENTITIES = 10_000
        private const val MAX_ASSETS = 5_000
        private const val MAX_COMPRESSION_RATIO = 100
    }
}
