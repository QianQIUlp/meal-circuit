package org.mealcircuit.app

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.put
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.add
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import org.mealcircuit.app.ai.AiClient
import org.mealcircuit.app.ai.AiProvider
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.data.MaterializedRecordEntity
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.EntityKind
import org.mealcircuit.app.domain.CheckinContract
import org.mealcircuit.app.domain.normalize
import org.mealcircuit.app.domain.ResultSchemas
import org.mealcircuit.app.domain.canonicalizeLogicalPayload
import org.mealcircuit.app.domain.preferenceId
import org.mealcircuit.app.domain.taskInputId
import org.mealcircuit.app.sync.PendingRegistration
import org.mealcircuit.app.sync.KeyRotationManager
import org.mealcircuit.app.sync.SyncAccountManager
import org.mealcircuit.app.sync.SyncWorker
import org.mealcircuit.app.portable.ImportMode
import org.mealcircuit.app.portable.PortableData
import org.mealcircuit.app.portable.ImportPreview
import org.mealcircuit.app.io.readBounded
import org.mealcircuit.app.io.MAX_MANAGED_ASSET_BYTES
import java.security.MessageDigest
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

data class UiMessage(val text: String, val isError: Boolean = false)
data class DeviceUi(val id: String, val name: String, val current: Boolean, val revoked: Boolean)
data class PortableImportUi(
    val uri: Uri,
    val recoveryKey: String,
    val mode: ImportMode,
    val preview: ImportPreview,
)

private val ASSET_EXTENSIONS = mapOf(
    "image/jpeg" to ".jpg", "image/png" to ".png",
    "image/gif" to ".gif", "image/webp" to ".webp",
)

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application as MealCircuitApplication
    val repository = app.repository
    private val accounts = SyncAccountManager(repository, app.vault)
    private val keyRotation = KeyRotationManager(application, repository, app.vault)
    private val ai = AiClient(app.vault)
    private val portable = PortableData(application, repository)
    private val checkinContract = CheckinContract.load(application)
    private val _message = MutableStateFlow<UiMessage?>(null)
    val message: StateFlow<UiMessage?> = _message.asStateFlow()
    private val _pendingRegistration = MutableStateFlow<PendingRegistration?>(null)
    val pendingRegistration: StateFlow<PendingRegistration?> = _pendingRegistration.asStateFlow()
    private val _exportRecoveryKey = MutableStateFlow<String?>(null)
    val exportRecoveryKey: StateFlow<String?> = _exportRecoveryKey.asStateFlow()
    private val _portableImport = MutableStateFlow<PortableImportUi?>(null)
    val portableImport: StateFlow<PortableImportUi?> = _portableImport.asStateFlow()
    private val _pairingQr = MutableStateFlow<String?>(null)
    val pairingQr: StateFlow<String?> = _pairingQr.asStateFlow()
    private val _devices = MutableStateFlow<List<DeviceUi>>(emptyList())
    val devices: StateFlow<List<DeviceUi>> = _devices.asStateFlow()
    private val _pendingRotationRecovery = MutableStateFlow(keyRotation.pendingRecovery())
    val pendingRotationRecovery: StateFlow<String?> = _pendingRotationRecovery.asStateFlow()
    private val preferences = application.getSharedPreferences("user_settings", android.content.Context.MODE_PRIVATE)
    private val _timezone = MutableStateFlow(preferences.getString("timezone", ZoneId.systemDefault().id)!!)
    val timezone: StateFlow<String> = _timezone.asStateFlow()
    private val defaultCheckinModules = setOf("weight", "training", "hunger", "sleep", "gut")
    private val _checkinModules = MutableStateFlow(
        preferences.getStringSet("checkin_modules", defaultCheckinModules)?.toSet() ?: defaultCheckinModules
    )
    val checkinModules: StateFlow<Set<String>> = _checkinModules.asStateFlow()

    init {
        viewModelScope.launch {
            repository.observe(EntityKind.PREFERENCES).collect { records ->
                records.forEach { record ->
                    val payload = runCatching { Json.parseToJsonElement(record.payloadJson).jsonObject }
                        .getOrNull() ?: return@forEach
                    val content = payload["content"]?.jsonPrimitive?.content ?: return@forEach
                    when (payload["kind"]?.jsonPrimitive?.content) {
                        "settings" -> runCatching {
                            Json.parseToJsonElement(content).jsonObject["timezone"]?.jsonPrimitive?.content
                        }.getOrNull()?.let { timezone ->
                            ZoneId.of(timezone)
                            preferences.edit().putString("timezone", timezone).commit()
                            _timezone.value = timezone
                        }
                        "checkin_settings" -> runCatching {
                            Json.parseToJsonElement(content).jsonArray.mapNotNull { element ->
                                val item = element.jsonObject
                                val enabled = item["enabled"]?.jsonPrimitive?.content in setOf("1", "true")
                                item["module_key"]?.jsonPrimitive?.content?.takeIf { enabled }
                            }.toSet()
                        }.getOrNull()?.let { enabled ->
                            preferences.edit().putStringSet("checkin_modules", enabled).commit()
                            _checkinModules.value = enabled
                        }
                    }
                }
            }
        }
    }

    fun dismissMessage() { _message.value = null }

    fun addDailyRecord(text: String) = launchAction("记录已保存") {
        require(text.isNotBlank())
        val recordId = DomainRevision.id("record")
        val day = LocalDate.now(ZoneId.of(_timezone.value)).toString()
        repository.save(
            EntityKind.DAILY_RECORD,
            buildJsonObject {
                put("id", recordId)
                put("record_date", day)
                put("raw_input", text.trim())
                put("created_at", Instant.now().toString())
            },
            recordId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun updateDailyRecord(recordId: String, text: String) = launchAction("记录已更新") {
        require(text.isNotBlank())
        val existing = requireNotNull(repository.record(recordId)) { "要修改的记录不存在" }
        require(existing.entityKind == "daily_record") { "只能修改饮食记录" }
        val payload = Json.parseToJsonElement(existing.payloadJson).jsonObject
        val day = payload.getValue("record_date").jsonPrimitive.content
        repository.save(
            EntityKind.DAILY_RECORD,
            buildJsonObject {
                put("id", recordId)
                put("record_date", day)
                put("raw_input", text.trim())
                put("created_at", payload.getValue("created_at"))
            },
            recordId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun saveCheckinDraft(
        raw: Map<String, Map<String, JsonElement>>,
        other: Map<String, Map<String, String>>,
        skipped: Set<String>,
    ) = saveStructuredCheckin(raw, other, skipped, false)
    fun publishCheckin(
        raw: Map<String, Map<String, JsonElement>>,
        other: Map<String, Map<String, String>>,
        skipped: Set<String>,
    ) = saveStructuredCheckin(raw, other, skipped, true)

    private fun saveStructuredCheckin(
        raw: Map<String, Map<String, JsonElement>>,
        other: Map<String, Map<String, String>>,
        skipped: Set<String>,
        published: Boolean,
    ) =
        launchAction(if (published) "状态已发布" else "状态草稿已保存") {
            val day = LocalDate.now(ZoneId.of(_timezone.value)).toString()
            val answers = checkinContract.modules.associate { module ->
                val values = raw[module.key].orEmpty()
                module.key to module.normalize(
                    values,
                    other[module.key].orEmpty(),
                    requireComplete = published && module.key !in skipped && values.isNotEmpty(),
                )
            }
            require(answers.values.any { it.isNotEmpty() } || skipped.isNotEmpty())
            val existingRecord = repository.records(EntityKind.CHECKIN_DAY).firstOrNull { record ->
                runCatching {
                    Json.parseToJsonElement(record.payloadJson).jsonObject
                        .getValue("checkin").jsonObject.getValue("checkin_date").jsonPrimitive.content == day
                }.getOrDefault(false)
            }
            val existing = existingRecord?.let { Json.parseToJsonElement(it.payloadJson).jsonObject }
            val checkinId = existingRecord?.entityId ?: DomainRevision.id("checkin")
            val timestamp = Instant.now().toString()
            val previousModules = existing?.get("modules")?.jsonArray.orEmpty().associateBy {
                it.jsonObject.getValue("module").jsonObject.getValue("module_key").jsonPrimitive.content
            }
            repository.save(
                EntityKind.CHECKIN_DAY,
                buildJsonObject {
                    put("checkin", buildJsonObject {
                        put("id", checkinId)
                        put("checkin_date", day)
                        put("created_at", existing?.get("checkin")?.jsonObject?.get("created_at") ?: JsonPrimitive(timestamp))
                        put("updated_at", timestamp)
                    })
                    put("modules", buildJsonArray {
                        listOf("weight", "training", "hunger", "sleep", "gut").forEach { key ->
                            val previous = previousModules[key]?.jsonObject
                            val previousModule = previous?.get("module")?.jsonObject
                            val priorVersion = previousModule?.get("version")?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                            val supplied = answers[key]
                            val skip = key in skipped
                            val handled = published && (skip || !supplied.isNullOrEmpty())
                            val status = when {
                                handled && skip -> "skipped"
                                handled -> "completed"
                                !supplied.isNullOrEmpty() -> "in_progress"
                                else -> previousModule?.get("status")?.jsonPrimitive?.content ?: "not_started"
                            }
                            val nextVersion = if (handled) priorVersion + 1 else priorVersion
                            add(buildJsonObject {
                                put("module", buildJsonObject {
                                    put("id", previousModule?.get("id") ?: JsonPrimitive(DomainRevision.id("checkin_module")))
                                    put("checkin_id", checkinId)
                                    put("module_key", key)
                                    put("status", status)
                                    put("answers_json", when {
                                        handled && skip -> JsonObject(emptyMap())
                                        handled -> supplied ?: JsonObject(emptyMap())
                                        else -> previousModule?.get("answers_json") ?: JsonObject(emptyMap())
                                    })
                                    put("draft_json", when {
                                        handled -> JsonNull
                                        !published && !supplied.isNullOrEmpty() -> supplied
                                        else -> previousModule?.get("draft_json") ?: JsonNull
                                    })
                                    put("schema_version", 1)
                                    put("version", nextVersion)
                                    put("created_at", previousModule?.get("created_at") ?: JsonPrimitive(timestamp))
                                    put("updated_at", timestamp)
                                    put("completed_at", if (handled) JsonPrimitive(timestamp) else previousModule?.get("completed_at") ?: JsonNull)
                                })
                                put("history", buildJsonArray {
                                    previous?.get("history")?.jsonArray?.forEach { add(it) }
                                    if (handled && previousModule != null && priorVersion > 0) {
                                        add(buildJsonObject {
                                            put("id", DomainRevision.id("checkin_history"))
                                            put("module_id", previousModule.getValue("id"))
                                            put("version", priorVersion)
                                            put("status", previousModule.getValue("status"))
                                            put("answers_json", previousModule["answers_json"] ?: JsonObject(emptyMap()))
                                            put("archived_at", timestamp)
                                            put("archive_reason", "new_version")
                                        })
                                    }
                                })
                            })
                        }
                    })
                },
                checkinId,
            )
            SyncWorker.enqueue(getApplication())
        }

    fun addMaterialTask(materials: String) = launchAction("原材料任务已创建") {
        require(materials.isNotBlank())
        val taskId = DomainRevision.id("task")
        repository.save(
            EntityKind.TASK,
            buildJsonObject {
                put("task", buildJsonObject {
                    put("id", taskId); put("type", "material"); put("status", "pending")
                    put("created_at", Instant.now().toString()); put("result_version", 0)
                })
            },
            taskId,
        )
        repository.save(
            EntityKind.TASK_INPUT,
            buildJsonObject {
                put("task_id", taskId)
                put("task_type", "material")
                put("input_version", 1)
                put("original_input", materials.trim())
                put("input_history", buildJsonArray {})
            },
            taskInputId(taskId),
        )
        SyncWorker.enqueue(getApplication())
    }

    fun addPhotoTask(uri: Uri, note: String, afterRead: () -> Unit = {}) =
        launchAction("照片任务已保存在本机") {
        val asset = try {
            ingestAsset(uri)
        } finally {
            afterRead()
        }
        val taskId = DomainRevision.id("task")
        repository.save(
            EntityKind.TASK,
            buildJsonObject {
                put("task", buildJsonObject {
                    put("id", taskId); put("type", "photo"); put("status", "pending")
                    put("created_at", Instant.now().toString()); put("result_version", 0)
                })
            },
            taskId,
        )
        repository.save(
            EntityKind.TASK_INPUT,
            buildJsonObject {
                put("task_id", taskId)
                put("task_type", "photo")
                put("input_version", 1)
                put("original_input", note.trim())
                put("asset_id", asset.id)
                put("input_history", buildJsonArray {})
            },
            taskInputId(taskId),
        )
        SyncWorker.enqueue(getApplication())
    }

    private suspend fun ingestAsset(uri: Uri): ManagedAssetEntity {
        val application = getApplication<Application>()
        val resolver = application.contentResolver
        val bytes = try {
            resolver.openInputStream(uri)?.use { it.readBounded(MAX_MANAGED_ASSET_BYTES) }
                ?: error("无法读取照片")
        } catch (error: IllegalArgumentException) {
            throw IllegalArgumentException("照片不能超过 10 MiB", error)
        }
        val mediaType = resolver.getType(uri) ?: "image/jpeg"
        require(mediaType in ASSET_EXTENSIONS) { "不支持的图片类型" }
        val extension = ASSET_EXTENSIONS.getValue(mediaType)
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes).hex()
        val assetId = "asset_$digest"
        repository.asset(assetId)?.let { existing ->
            val file = existing.relativePath?.let(application.filesDir::resolve)
            if (file?.isFile == true && file.length() == bytes.size.toLong()) return existing
        }
        val relative = "assets/$digest$extension"
        val target = application.filesDir.resolve(relative)
        val parent = requireNotNull(target.parentFile)
        parent.mkdirs()
        val temporary = parent.resolve(".${target.name}.${DomainRevision.id("tmp")}")
        temporary.writeBytes(bytes)
        if (!temporary.renameTo(target)) {
            temporary.copyTo(target, overwrite = true)
            check(temporary.delete()) { "临时照片清理失败" }
        }
        val asset = ManagedAssetEntity(
            assetId, digest, mediaType, extension, bytes.size.toLong(), relative,
            unresolved = false, createdAt = Instant.now().toString(),
        )
        repository.putAsset(asset)
        repository.save(
            EntityKind.ASSET,
            buildJsonObject {
                put("sha256", digest); put("media_type", mediaType)
                put("extension", extension); put("byte_count", bytes.size)
            },
            assetId,
        )
        return asset
    }

    fun updateTaskInput(entityId: String, text: String) = launchAction("任务输入修订已保存") {
        val record = repository.record(entityId) ?: error("任务输入不存在")
        val payload = Json.parseToJsonElement(record.payloadJson).jsonObject
        val taskId = payload.getValue("task_id").jsonPrimitive.content
        val task = repository.record(taskId)?.let { Json.parseToJsonElement(it.payloadJson).jsonObject.getValue("task").jsonObject }
            ?: error("任务主体不存在")
        require(task.getValue("status").jsonPrimitive.content == "pending") { "已完成任务只能追加校正" }
        val previous = payload.getValue("original_input").jsonPrimitive.content
        val version = payload.getValue("input_version").jsonPrimitive.content.toInt()
        require(text.isNotBlank() || payload.getValue("task_type").jsonPrimitive.content == "photo")
        if (text.trim() == previous) return@launchAction
        repository.save(
            EntityKind.TASK_INPUT,
            JsonObject(payload + mapOf(
                "input_version" to JsonPrimitive(version + 1),
                "original_input" to JsonPrimitive(text.trim()),
                "input_history" to buildJsonArray {
                    payload["input_history"]?.jsonArray?.forEach { add(it) }
                    add(buildJsonObject {
                        put("id", DomainRevision.id("task_input_history")); put("task_id", taskId)
                        put("version", version); put("input_text", previous); put("archived_at", Instant.now().toString())
                    })
                },
            )),
            entityId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun addTaskCorrection(taskId: String, text: String) = launchAction("用户校正已追加") {
        require(text.isNotBlank())
        val task = repository.record(taskId)?.let { Json.parseToJsonElement(it.payloadJson).jsonObject.getValue("task").jsonObject }
            ?: error("任务不存在")
        require(task.getValue("status").jsonPrimitive.content == "completed") { "只能校正已完成任务" }
        val correctionId = DomainRevision.id("correction")
        repository.save(
            EntityKind.CORRECTION,
            buildJsonObject {
                put("id", correctionId); put("task_id", taskId)
                put("correction_json", buildJsonObject { put("text", text.trim()) })
                put("created_at", Instant.now().toString())
            },
            correctionId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun addFood(
        name: String,
        notes: String,
        energy: String,
        protein: String,
        carbs: String,
        fat: String,
        packagePhoto: Uri? = null,
    ) = launchAction("食品已加入本地营养库") {
        require(name.isNotBlank())
        val foodId = DomainRevision.id("food")
        val timestamp = Instant.now().toString()
        val packageAsset = packagePhoto?.let { ingestAsset(it) }
        repository.save(
            EntityKind.FOOD_ITEM,
            buildJsonObject {
                put("food", buildJsonObject {
                    put("id", foodId); put("name", name.trim()); put("brand", "")
                    put("basis", "100g")
                    put("energy_kcal", energy.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull)
                    put("protein_g", protein.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull)
                    put("carbs_g", carbs.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull)
                    put("fat_g", fat.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull)
                    put("category", "other"); put("menu_priority", "normal")
                    put("notes", notes.trim()); put("created_at", timestamp); put("updated_at", timestamp)
                    packageAsset?.let { put("package_photo_asset_id", it.id) }
                })
                put("history", buildJsonArray {})
            },
            foodId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun updateFood(
        entityId: String,
        name: String,
        notes: String,
        energy: String,
        protein: String,
        carbs: String,
        fat: String,
        packagePhoto: Uri? = null,
    ) = launchAction("食品修订已保存") {
        require(name.isNotBlank())
        val record = repository.record(entityId) ?: error("食品不存在")
        val payload = Json.parseToJsonElement(record.payloadJson).jsonObject
        val before = payload.getValue("food").jsonObject
        val timestamp = Instant.now().toString()
        val fields = mutableMapOf<String, JsonElement>(
            "name" to JsonPrimitive(name.trim()),
            "notes" to JsonPrimitive(notes.trim()),
            "energy_kcal" to (energy.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull),
            "protein_g" to (protein.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull),
            "carbs_g" to (carbs.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull),
            "fat_g" to (fat.toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonNull),
            "updated_at" to JsonPrimitive(timestamp),
        )
        packagePhoto?.let { fields["package_photo_asset_id"] = JsonPrimitive(ingestAsset(it).id) }
        val after = JsonObject(before + fields)
        repository.save(
            EntityKind.FOOD_ITEM,
            buildJsonObject {
                put("food", after)
                put("history", buildJsonArray {
                    payload["history"]?.jsonArray?.forEach { add(it) }
                    add(buildJsonObject {
                        put("id", DomainRevision.id("food_history")); put("food_id", entityId)
                        put("event", "update"); put("before_json", before); put("after_json", after)
                        put("created_at", timestamp)
                    })
                })
            },
            entityId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun deleteFood(entityId: String) = launchAction("食品已软删除，历史仍保留") {
        val record = repository.record(entityId) ?: error("食品不存在")
        val payload = Json.parseToJsonElement(record.payloadJson).jsonObject
        val before = payload.getValue("food").jsonObject
        val timestamp = Instant.now().toString()
        val after = JsonObject(before + mapOf("deleted_at" to JsonPrimitive(timestamp), "updated_at" to JsonPrimitive(timestamp)))
        repository.save(
            EntityKind.FOOD_ITEM,
            buildJsonObject {
                put("food", after)
                put("history", buildJsonArray {
                    payload["history"]?.jsonArray?.forEach { add(it) }
                    add(buildJsonObject {
                        put("id", DomainRevision.id("food_history")); put("food_id", entityId)
                        put("event", "delete"); put("before_json", before); put("after_json", after)
                        put("created_at", timestamp)
                    })
                })
            },
            entityId,
            deleted = true,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun addMemory(text: String) = launchAction("长期记忆已保存") {
        require(text.isNotBlank())
        val memoryId = DomainRevision.id("memory")
        val timestamp = Instant.now().toString()
        repository.save(
            EntityKind.MEMORY,
            buildJsonObject {
                put("id", memoryId); put("kind", "other"); put("content", text.trim())
                put("evidence", ""); put("active", 1)
                put("created_at", timestamp); put("updated_at", timestamp)
            },
            memoryId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun addAdjustment(text: String) = launchAction("当前调整已保存") {
        require(text.isNotBlank())
        val adjustmentId = DomainRevision.id("adjustment")
        val timestamp = Instant.now().toString()
        repository.save(
            EntityKind.ADJUSTMENT,
            buildJsonObject {
                put("id", adjustmentId); put("content", text.trim()); put("reason", "Android")
                put("active", 1); put("created_at", timestamp); put("updated_at", timestamp)
            },
            adjustmentId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun setActive(kind: EntityKind, entityId: String, active: Boolean) = launchAction(
        if (active) "条目已重新启用" else "条目已停用，历史仍保留"
    ) {
        require(kind in setOf(EntityKind.MEMORY, EntityKind.ADJUSTMENT))
        val record = repository.record(entityId) ?: error("条目不存在")
        val payload = Json.parseToJsonElement(record.payloadJson).jsonObject
        repository.save(
            kind,
            JsonObject(payload + mapOf(
                "active" to JsonPrimitive(if (active) 1 else 0),
                "updated_at" to JsonPrimitive(Instant.now().toString()),
            )),
            entityId,
            deleted = !active,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun savePreference(kind: String, content: String) = launchAction("$kind 已保存") {
        require(kind in setOf("profile", "doctrine"))
        repository.save(
            EntityKind.PREFERENCES,
            buildJsonObject { put("kind", kind); put("content", content) },
            preferenceId(kind),
        )
        SyncWorker.enqueue(getApplication())
    }

    fun saveSettings(content: String) = launchAction("settings 已保存") {
        val value = Json.parseToJsonElement(content).jsonObject
        require(value["schema_version"]?.jsonPrimitive?.content?.toIntOrNull() == 1)
        val timezone = ZoneId.of(value.getValue("timezone").jsonPrimitive.content).id
        require(value["meal_environment"]?.jsonPrimitive?.content?.isNotBlank() == true)
        val target = value["protein_target_g"]?.jsonArray ?: error("protein_target_g 缺失")
        val targetNumbers = target.map { it.jsonPrimitive.doubleOrNull ?: error("protein_target_g 必须是数字") }
        require(targetNumbers.size == 2 && targetNumbers[0] > 0 && targetNumbers[1] >= targetNumbers[0])
        require(value["portion_method"]?.jsonPrimitive?.content?.isNotBlank() == true)
        require(value["missing_training_default"]?.jsonPrimitive?.content?.isNotBlank() == true)
        require(value["compensation_boundary"]?.jsonPrimitive?.content?.isNotBlank() == true)
        val home = value["home_cooking"]?.jsonObject ?: error("home_cooking 缺失")
        val homeEnabled = home["enabled"]?.jsonPrimitive?.booleanOrNull ?: error("home_cooking.enabled 必须是布尔值")
        if (homeEnabled) {
            val required = setOf(
                "region", "meal_scope", "servings", "weekday_time_limit_minutes", "equipment",
                "recipe_detail", "rotation_window_days", "reuse_policy", "flavor_preferences",
                "online_purchase_mode", "food_exclusions",
            )
            require(required.all(home::containsKey)) { "home_cooking 开启时字段不完整" }
            require(home.getValue("region").jsonPrimitive.content == "china")
            require(home.getValue("meal_scope").jsonPrimitive.content == "dinner")
            require(home.getValue("servings").jsonPrimitive.content.toIntOrNull() == 1)
            val timeLimit = home.getValue("weekday_time_limit_minutes").jsonPrimitive.content.toIntOrNull() ?: 0
            val window = home.getValue("rotation_window_days").jsonPrimitive.content.toIntOrNull() ?: 0
            require(timeLimit in 10..60); require(window in 2..14)
        }
        repository.save(
            EntityKind.PREFERENCES,
            buildJsonObject { put("kind", "settings"); put("content", value.toString()) },
            preferenceId("settings"),
        )
        check(preferences.edit().putString("timezone", timezone).commit())
        _timezone.value = timezone
        SyncWorker.enqueue(getApplication())
    }

    fun saveCheckinModules(enabled: Set<String>) = launchAction("状态模块设置已保存") {
        require(enabled.all { it in defaultCheckinModules })
        val timestamp = Instant.now().toString()
        val content = buildJsonArray {
            defaultCheckinModules.sorted().forEachIndexed { index, key ->
                add(buildJsonObject {
                    put("module_key", key); put("enabled", if (key in enabled) 1 else 0)
                    put("sort_order", index); put("frequency", "daily"); put("updated_at", timestamp)
                })
            }
        }.toString()
        repository.save(
            EntityKind.PREFERENCES,
            buildJsonObject { put("kind", "checkin_settings"); put("content", content) },
            preferenceId("checkin_settings"),
        )
        check(preferences.edit().putStringSet("checkin_modules", enabled).commit())
        _checkinModules.value = enabled
        SyncWorker.enqueue(getApplication())
    }

    fun saveAiKey(provider: AiProvider, model: String, key: String) = launchAction("AI 配置已保存；API Key 已由 Android Keystore 包装") {
        require(model.isNotBlank())
        ai.saveKey(provider, key)
        check(preferences.edit().putString("ai_provider", provider.name).putString("ai_model", model.trim()).commit())
    }

    fun generateLatestTask() = launchAction("任务分析已保存到本机") {
        val input = repository.records(EntityKind.TASK_INPUT).firstOrNull() ?: error("没有任务输入")
        val inputPayload = Json.parseToJsonElement(input.payloadJson).jsonObject
        val taskId = inputPayload.getValue("task_id").jsonPrimitive.content
        val task = repository.record(taskId) ?: error("任务主体缺失")
        val taskPayload = Json.parseToJsonElement(task.payloadJson).jsonObject
        val taskRow = taskPayload.getValue("task").jsonObject
        if (taskRow["status"]?.jsonPrimitive?.content == "completed") error("最新任务已完成")
        val taskType = taskRow.getValue("type").jsonPrimitive.content
        val today = LocalDate.now(ZoneId.of(_timezone.value))
        val start = today.minusDays(13)
        val recentRecords = repository.records(EntityKind.DAILY_RECORD).filter { record ->
            runCatching { LocalDate.parse(Json.parseToJsonElement(record.payloadJson).jsonObject
                .getValue("record_date").jsonPrimitive.content) in start..today }.getOrDefault(false)
        }
        val recentCheckins = repository.records(EntityKind.CHECKIN_DAY).filter { record ->
            runCatching { LocalDate.parse(Json.parseToJsonElement(record.payloadJson).jsonObject
                .getValue("checkin").jsonObject.getValue("checkin_date").jsonPrimitive.content) in start..today }
                .getOrDefault(false) && record.publishedCheckinPayload() != null
        }
        val foodLibrary = repository.records(EntityKind.FOOD_ITEM).filterNot { it.deleted }
        val memories = repository.records(EntityKind.MEMORY).filter { it.activePayload() }
        val adjustments = repository.records(EntityKind.ADJUSTMENT).filter { it.activePayload() }
        val domainPreferences = repository.records(EntityKind.PREFERENCES)
        val settings = domainPreferences.preferenceContent("settings")?.let {
            runCatching { Json.parseToJsonElement(it).jsonObject }.getOrNull()
        }
        val doctrine = domainPreferences.preferenceContent("doctrine").orEmpty()
        val source = sourceSnapshot(
            setOf(taskId, input.entityId) + recentRecords.map { it.entityId } +
                recentCheckins.map { it.entityId } + foodLibrary.map { it.entityId } +
                memories.map { it.entityId } + adjustments.map { it.entityId } +
                domainPreferences.map { it.entityId }
        )
        val context = buildJsonObject {
            put("task", taskRow)
            put("task_input", inputPayload)
            put("recent_days", 14)
            put("doctrine", buildJsonObject {
                put("mode", if (doctrine.isBlank()) "public_core" else "private_override")
                put("sources", buildJsonArray { add(if (doctrine.isBlank()) "rules/core.md" else "doctrine.private.md") })
                put("content", doctrine)
            })
            settings?.let { put("settings", it) }
            put("source_revisions", source)
            putJsonArray("recent_records") { recentRecords.forEach { add(Json.parseToJsonElement(it.payloadJson)) } }
            putJsonArray("recent_checkins") { recentCheckins.forEach { add(requireNotNull(it.publishedCheckinPayload())) } }
            putJsonArray("food_library_matches") { foodLibrary.forEach { add(Json.parseToJsonElement(it.payloadJson)) } }
            putJsonArray("long_term_memories") { memories.forEach { add(Json.parseToJsonElement(it.payloadJson)) } }
            putJsonArray("current_adjustments") { adjustments.forEach { add(Json.parseToJsonElement(it.payloadJson)) } }
            putJsonArray("preferences") { domainPreferences.forEach { add(Json.parseToJsonElement(it.payloadJson)) } }
            put("result_schema", ResultSchemas.task(taskType))
            put("analysis_boundary", "照片与数量只能区间估算；不可伪造不可见油、酱汁、重量或品牌。")
        }
        val imageAsset = inputPayload["asset_id"]?.jsonPrimitive?.content?.let { repository.asset(it) }
        val image = imageAsset?.relativePath?.let { getApplication<Application>().filesDir.resolve(it).readBytes() }
        val result = ai.generate(aiConfiguration(), taskType, context, image, imageAsset?.mediaType)
        org.mealcircuit.app.domain.ResultValidator.task(
            taskType,
            result,
        )
        val provenance = provenance(source, domainPreferences)
        repository.save(
            EntityKind.ANALYSIS_RESULT,
            buildJsonObject {
                put("source_entity_id", taskId); put("source_kind", "task")
                put("result_version", 1); put("result", result); put("provenance", provenance)
            },
        )
        repository.save(
            EntityKind.TASK,
            buildJsonObject {
                put("task", JsonObject(taskRow + mapOf(
                    "status" to JsonPrimitive("completed"),
                    "result_json" to result,
                    "result_provenance_json" to provenance,
                    "result_version" to JsonPrimitive((taskRow["result_version"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0) + 1),
                    "completed_at" to JsonPrimitive(Instant.now().toString()),
                )))
            },
            taskId,
        )
        SyncWorker.enqueue(getApplication())
    }

    // Android records execution evidence and renders plans already published by
    // Windows. It must not create a competing daily review outside the shared
    // seven-stage workflow.
    @Deprecated("每日复盘仅由 Windows 的七阶段工作流发布", level = DeprecationLevel.ERROR)
    private fun generateDailyReview() = launchAction("每日复盘请在 Windows 完成") {
        val reviewDay = LocalDate.now(ZoneId.of(_timezone.value))
        val windowStart = reviewDay.minusDays(13)
        val records = repository.records(EntityKind.DAILY_RECORD).filter { record ->
            runCatching { LocalDate.parse(Json.parseToJsonElement(record.payloadJson).jsonObject
                .getValue("record_date").jsonPrimitive.content) in windowStart..reviewDay }.getOrDefault(false)
        }
        val checkins = repository.records(EntityKind.CHECKIN_DAY).filter { record ->
            runCatching { LocalDate.parse(Json.parseToJsonElement(record.payloadJson).jsonObject
                .getValue("checkin").jsonObject.getValue("checkin_date").jsonPrimitive.content) in windowStart..reviewDay }
                .getOrDefault(false) && record.publishedCheckinPayload() != null
        }
        val foods = repository.records(EntityKind.FOOD_ITEM)
        val memories = repository.records(EntityKind.MEMORY).filter { it.activePayload() }
        val adjustments = repository.records(EntityKind.ADJUSTMENT).filter { it.activePayload() }
        val preferences = repository.records(EntityKind.PREFERENCES)
        val previousReviews = repository.records(EntityKind.DAILY_REVIEW).filter { record ->
            runCatching {
                val value = LocalDate.parse(Json.parseToJsonElement(record.payloadJson).jsonObject
                    .getValue("review").jsonObject.getValue("review_date").jsonPrimitive.content)
                value >= windowStart && value < reviewDay
            }.getOrDefault(false)
        }
        val settings = preferences.firstNotNullOfOrNull { record ->
            runCatching {
                val outer = Json.parseToJsonElement(record.payloadJson).jsonObject
                if (outer["kind"]?.jsonPrimitive?.content != "settings") null
                else Json.parseToJsonElement(outer.getValue("content").jsonPrimitive.content).jsonObject
            }.getOrNull()
        }
        val doctrine = preferences.preferenceContent("doctrine").orEmpty()
        val priorityFoodIds = foods.mapNotNull { record ->
            val food = runCatching {
                Json.parseToJsonElement(record.payloadJson).jsonObject.getValue("food").jsonObject
            }.getOrNull() ?: return@mapNotNull null
            if (food["menu_priority"]?.jsonPrimitive?.content == "high") record.entityId else null
        }.toSet()
        val carryovers = carryoverObligations(previousReviews, reviewDay)
        val carryoverIds = carryovers.map { it.jsonObject.getValue("id").jsonPrimitive.content }.toSet()
        val recentHomeMenus = previousReviews.mapNotNull { record ->
            runCatching {
                Json.parseToJsonElement(record.payloadJson).jsonObject.getValue("review").jsonObject
                    .getValue("result_json").jsonObject.getValue("tomorrow_menu").jsonObject
            }.getOrNull()
        }
        val previousRotation = recentHomeMenus.firstNotNullOfOrNull { it["rotation"] as? JsonObject }
        val targetReview = repository.records(EntityKind.DAILY_REVIEW).firstNotNullOfOrNull { record ->
            runCatching {
                Json.parseToJsonElement(record.payloadJson).jsonObject.takeIf { payload ->
                    payload.getValue("review").jsonObject.getValue("review_date").jsonPrimitive.content == reviewDay.toString()
                }
            }.getOrNull()
        }
        require(targetReview != null) { "今天没有可复盘的饮食记录或已发布状态" }
        require(targetReview.getValue("review").jsonObject.getValue("status").jsonPrimitive.content == "pending") {
            "今日复盘已经完成；新增记录或发布新状态后才会重新排队"
        }
        val targetCheckin = checkins.firstNotNullOfOrNull { record ->
            record.publishedCheckinPayload()?.takeIf { payload ->
                payload.getValue("checkin").jsonObject.getValue("checkin_date").jsonPrimitive.content == reviewDay.toString()
            }
        }
        val targetModules = targetCheckin?.get("modules")?.jsonArray.orEmpty()
        val publishedKeys = targetModules.map { element ->
            element.jsonObject.getValue("module").jsonObject.getValue("module_key").jsonPrimitive.content
        }.toSet()
        val dueModules = _checkinModules.value.sorted()
        val sourceCheckinVersions = buildJsonObject {
            targetModules.forEach { element ->
                val module = element.jsonObject.getValue("module").jsonObject
                put(module.getValue("module_key").jsonPrimitive.content, module.getValue("version"))
            }
        }
        val targetModuleSummaries = buildJsonArray {
            targetModules.forEach { element ->
                val module = element.jsonObject.getValue("module").jsonObject
                val status = module.getValue("status").jsonPrimitive.content
                add(buildJsonObject {
                    put("module_key", module.getValue("module_key")); put("status", status)
                    put("version", module.getValue("version"))
                    put("answers", module["answers_json"] ?: JsonNull)
                    put(
                        "summary",
                        if (status == "skipped") "用户选择今天不提供"
                        else module["answers_json"]?.toString().orEmpty(),
                    )
                })
            }
        }
        val source = sourceSnapshot(
            records.map { it.entityId }.toSet() + checkins.map { it.entityId } + foods.map { it.entityId } +
                memories.map { it.entityId } + adjustments.map { it.entityId } + preferences.map { it.entityId } +
                previousReviews.map { it.entityId }
        )
        val context = buildJsonObject {
            put("recent_days", 14)
            targetReview?.let { put("daily_review", it.getValue("review")) }
            put("doctrine", buildJsonObject {
                put("mode", if (doctrine.isBlank()) "public_core" else "private_override")
                put("sources", buildJsonArray { add(if (doctrine.isBlank()) "rules/core.md" else "doctrine.private.md") })
                put("content", doctrine)
            })
            put("source_revisions", source)
            putJsonArray("recent_records") {
                records.forEach { add(Json.parseToJsonElement(it.payloadJson)) }
            }
            putJsonArray("recent_checkins") {
                checkins.forEach { record ->
                    val payload = requireNotNull(record.publishedCheckinPayload())
                    val checkinRow = payload.getValue("checkin").jsonObject
                    payload.getValue("modules").jsonArray.forEach { element ->
                        val module = element.jsonObject.getValue("module").jsonObject
                        add(buildJsonObject {
                            put("checkin_id", checkinRow.getValue("id"))
                            put("checkin_date", checkinRow.getValue("checkin_date"))
                            put("module_key", module.getValue("module_key")); put("status", module.getValue("status"))
                            put("answers_json", module["answers_json"] ?: JsonObject(emptyMap()))
                            put("version", module.getValue("version")); put("completed_at", module["completed_at"] ?: JsonNull)
                        })
                    }
                }
            }
            put("target_checkin", buildJsonObject {
                put("date", reviewDay.toString())
                put("modules", targetModuleSummaries)
            })
            put("checkin_coverage", buildJsonObject {
                put("due", dueModules.size); put("handled", publishedKeys.intersect(dueModules.toSet()).size)
                put("missing", buildJsonArray { (dueModules - publishedKeys).forEach { add(it) } })
            })
            put(
                "checkin_resolution_note",
                "同日同模块仅使用最新已发布版本；草稿不进入上下文，明确跳过和缺失都保持未知。",
            )
            putJsonArray("food_library") {
                foods.forEach { add(Json.parseToJsonElement(it.payloadJson)) }
            }
            putJsonArray("priority_foods") {
                foods.filter { it.entityId in priorityFoodIds }.forEach { add(Json.parseToJsonElement(it.payloadJson)) }
            }
            put("ingredient_carryover_obligations", carryovers)
            put("recent_home_dinners", buildJsonArray { recentHomeMenus.take(14).forEach { add(it) } })
            put("recent_online_categories", buildJsonArray {
                recentHomeMenus.take(14).flatMap { menu -> menu["online_options"]?.jsonArray.orEmpty() }
                    .mapNotNull { it.jsonObject["category"]?.jsonPrimitive?.content }
                    .distinct().forEach { add(it) }
            })
            put("home_cooking_generation_protocol", buildJsonObject {
                put("breakfast", "quick_assembly"); put("lunch", "eat_out")
                put("dinner", "home_cook beginner card within configured time and cookware limits")
                put("rotation", "reuse ingredients while rotating dish and primary flavor")
            })
            putJsonArray("long_term_memories") {
                memories.filterNot { it.deleted }.forEach { add(Json.parseToJsonElement(it.payloadJson)) }
            }
            putJsonArray("current_adjustments") {
                adjustments.filterNot { it.deleted }.forEach { add(Json.parseToJsonElement(it.payloadJson)) }
            }
            putJsonArray("preferences") {
                preferences.forEach { add(Json.parseToJsonElement(it.payloadJson)) }
            }
            settings?.let { put("settings", it) }
            put("home_cooking_preferences", settings?.get("home_cooking") ?: buildJsonObject { put("enabled", false) })
            put("result_schema", ResultSchemas.daily(
                reviewDay.plusDays(1),
                settings?.get("meal_environment")?.jsonPrimitive?.content ?: "用户自行配置",
                settings?.get("protein_target_g")?.jsonArray ?: buildJsonArray { add(50); add(65) },
                priorityFoodIds,
                settings?.get("home_cooking")?.jsonObject,
                carryovers,
            ))
        }
        val result = ai.generate(aiConfiguration(), "daily", context)
        org.mealcircuit.app.domain.ResultValidator.daily(
            result,
            reviewDay.plusDays(1),
            expectedPriorityFoodIds = priorityFoodIds,
            expectedEnvironment = settings?.get("meal_environment")?.jsonPrimitive?.content,
            expectedProteinTarget = settings?.get("protein_target_g")?.jsonArray,
            expectedCarryoverIds = carryoverIds,
            homeCooking = settings?.get("home_cooking")?.jsonObject,
            previousRotation = previousRotation,
        )
        val provenance = provenance(source, preferences)
        val day = reviewDay.toString()
        val existingRecord = repository.records(EntityKind.DAILY_REVIEW).firstOrNull { record ->
            runCatching {
                Json.parseToJsonElement(record.payloadJson).jsonObject.getValue("review").jsonObject
                    .getValue("review_date").jsonPrimitive.content == day
            }.getOrDefault(false)
        }
        val existing = existingRecord?.let { Json.parseToJsonElement(it.payloadJson).jsonObject }
        val previousReview = existing?.get("review")?.jsonObject
        val reviewId = existingRecord?.entityId ?: DomainRevision.id("review")
        val timestamp = Instant.now().toString()
        val resultVersion = (previousReview?.get("result_version")?.jsonPrimitive?.content?.toIntOrNull() ?: 0) + 1
        val sourceRecordIds = buildJsonArray {
            records.filter { record ->
                runCatching {
                    Json.parseToJsonElement(record.payloadJson).jsonObject
                        .getValue("record_date").jsonPrimitive.content == day
                }.getOrDefault(false)
            }.map { it.entityId }.sorted().forEach { add(JsonPrimitive(it)) }
        }
        val previousResult = previousReview?.get("result_json")
        repository.save(
            EntityKind.DAILY_REVIEW,
            buildJsonObject {
                put("review", buildJsonObject {
                    put("id", reviewId); put("review_date", day); put("status", "completed")
                    put("source_record_ids_json", sourceRecordIds); put("result_json", result)
                    put("source_checkin_versions_json", sourceCheckinVersions)
                    put("result_provenance_json", provenance)
                    put("result_version", resultVersion)
                    put("created_at", previousReview?.get("created_at") ?: JsonPrimitive(timestamp))
                    put("updated_at", timestamp); put("completed_at", timestamp)
                })
                put("history", buildJsonArray {
                    existing?.get("history")?.jsonArray?.forEach { add(it) }
                    if (previousReview != null && previousResult != null && previousResult !is JsonNull) {
                        add(buildJsonObject {
                            put("id", DomainRevision.id("review_history")); put("review_id", reviewId)
                            put("version", previousReview["result_version"] ?: JsonPrimitive(1))
                            put("source_record_ids_json", previousReview["source_record_ids_json"] ?: buildJsonArray {})
                            put("result_json", previousResult)
                            previousReview["result_provenance_json"]?.let { put("result_provenance_json", it) }
                            put("completed_at", previousReview["completed_at"] ?: JsonNull)
                            put("archived_at", timestamp); put("archive_reason", "new_source")
                        })
                    }
                })
            },
            reviewId,
        )
        repository.save(
            EntityKind.ANALYSIS_RESULT,
            buildJsonObject {
                put("source_entity_id", reviewId); put("source_kind", "daily_review")
                put("result_version", resultVersion); put("result", result); put("provenance", provenance)
            },
        )
        SyncWorker.enqueue(getApplication())
    }

    private suspend fun sourceSnapshot(entityIds: Set<String>) = buildJsonArray {
        repository.heads().filter { it.entityId in entityIds }.forEach { head ->
            add(buildJsonObject {
                put("entity_id", head.entityId); put("entity_kind", head.entityKind); put("revision_id", head.revisionId)
            })
        }
    }

    private fun carryoverObligations(
        reviews: List<org.mealcircuit.app.data.MaterializedRecordEntity>,
        reviewDay: LocalDate,
    ) = buildJsonArray {
        val target = reviewDay.plusDays(1)
        reviews.forEach { record ->
            val review = runCatching {
                Json.parseToJsonElement(record.payloadJson).jsonObject.getValue("review").jsonObject
            }.getOrNull() ?: return@forEach
            if (review["status"]?.jsonPrimitive?.content != "completed") return@forEach
            val reviewDate = runCatching { LocalDate.parse(review.getValue("review_date").jsonPrimitive.content) }.getOrNull()
                ?: return@forEach
            if (!reviewDate.isBefore(reviewDay)) return@forEach
            val result = review["result_json"] as? JsonObject ?: return@forEach
            val menu = result["tomorrow_menu"] as? JsonObject ?: return@forEach
            val menuDate = runCatching { LocalDate.parse(menu.getValue("date").jsonPrimitive.content) }.getOrNull()
                ?: return@forEach
            val reuse = menu["reuse_plan"] as? JsonObject ?: return@forEach
            val horizon = reuse["horizon_days"]?.jsonPrimitive?.content?.toIntOrNull() ?: return@forEach
            if (reviewDay > menuDate.plusDays((horizon - 1).toLong())) return@forEach
            val shopping = menu["shopping_list"]?.jsonArray.orEmpty()
            reuse["items"]?.jsonArray?.forEachIndexed { index, element ->
                val item = element.jsonObject
                val ingredient = item["ingredient"]?.jsonPrimitive?.content?.trim().orEmpty()
                if (ingredient.isEmpty()) return@forEachIndexed
                val reuseText = buildString {
                    append(ingredient); append(' ')
                    append(item["tomorrow_use"]?.jsonPrimitive?.content.orEmpty()); append(' ')
                    append(item["storage"]?.jsonPrimitive?.content.orEmpty()); append(' ')
                    item["later_uses"]?.jsonArray.orEmpty().forEach { use ->
                        append(use.jsonObject["use"]?.jsonPrimitive?.content.orEmpty()); append(' ')
                    }
                }.lowercase()
                val hasRequiredPurchase = shopping.any { purchase ->
                    val value = purchase.jsonObject
                    val name = value["name"]?.jsonPrimitive?.content.orEmpty()
                    value["required"]?.jsonPrimitive?.booleanOrNull == true &&
                        (reuseText.contains(name.lowercase()) || name.contains(ingredient, ignoreCase = true))
                }
                if (!hasRequiredPurchase) return@forEachIndexed
                val planned = item["later_uses"]?.jsonArray.orEmpty().mapNotNull { use ->
                    val value = use.jsonObject
                    val date = runCatching { LocalDate.parse(value.getValue("date").jsonPrimitive.content) }.getOrNull()
                        ?: return@mapNotNull null
                    if (date < reviewDay || date > target || date > menuDate.plusDays((horizon - 1).toLong())) null else value
                }.minByOrNull { it.getValue("date").jsonPrimitive.content } ?: return@forEachIndexed
                val raw = "${record.entityId}|${menuDate}|$index|$ingredient"
                val id = "carryover_${MessageDigest.getInstance("SHA-256").digest(raw.toByteArray()).hex().take(12)}"
                add(buildJsonObject {
                    put("id", id); put("source_review_date", reviewDate.toString())
                    put("source_menu_date", menuDate.toString()); put("ingredient", ingredient)
                    put("planned_use_date", planned.getValue("date")); put("planned_use", planned.getValue("use"))
                    put("storage", item["storage"] ?: JsonPrimitive(""))
                })
            }
        }
    }

    private fun provenance(
        source: kotlinx.serialization.json.JsonArray,
        preferenceRecords: List<MaterializedRecordEntity>,
    ) = buildJsonObject {
        fun document(kind: String): Pair<String?, String?> {
            val record = preferenceRecords.firstOrNull { item ->
                runCatching {
                    Json.parseToJsonElement(item.payloadJson).jsonObject["kind"]?.jsonPrimitive?.content == kind
                }.getOrDefault(false)
            } ?: return null to null
            val content = Json.parseToJsonElement(record.payloadJson).jsonObject
                .getValue("content").jsonPrimitive.content
            val revision = source.firstOrNull { element ->
                element.jsonObject["entity_id"]?.jsonPrimitive?.content == record.entityId
            }?.jsonObject?.get("revision_id")?.jsonPrimitive?.content
            return revision to MessageDigest.getInstance("SHA-256").digest(content.toByteArray()).hex()
        }
        val settings = document("settings")
        val doctrine = document("doctrine")
        put("schema_version", 1); put("source_revisions", source); put("result_schema_version", 1)
        put("settings_revision_id", settings.first?.let(::JsonPrimitive) ?: JsonNull)
        put("settings_sha256", settings.second?.let(::JsonPrimitive) ?: JsonNull)
        put("doctrine_revision_id", doctrine.first?.let(::JsonPrimitive) ?: JsonNull)
        put("doctrine_sha256", doctrine.second?.let(::JsonPrimitive) ?: JsonNull)
        put("generator", buildJsonObject {
            put("provider", preferences.getString("ai_provider", "") ?: "")
            put("model", preferences.getString("ai_model", "") ?: "")
            put("generated_at", Instant.now().toString())
        })
    }

    private fun aiConfiguration() = org.mealcircuit.app.ai.AiConfiguration(
        AiProvider.valueOf(preferences.getString("ai_provider", null) ?: error("请先配置 AI provider")),
        preferences.getString("ai_model", null) ?: error("请先配置模型"),
    )

    fun saveTimezone(value: String) = launchAction("时区已保存") {
        val normalized = ZoneId.of(value.trim()).id
        check(preferences.edit().putString("timezone", normalized).commit())
        _timezone.value = normalized
        val entityId = preferenceId("settings")
        val existing = repository.record(entityId)?.let {
            runCatching {
                val outer = Json.parseToJsonElement(it.payloadJson).jsonObject
                Json.parseToJsonElement(outer.getValue("content").jsonPrimitive.content).jsonObject
            }.getOrNull()
        } ?: buildJsonObject {
            put("schema_version", 1); put("meal_environment", "用户自行配置")
            put("protein_target_g", buildJsonArray { add(50); add(65) })
            put("portion_method", "按实际饥饿和正餐结构")
            put("missing_training_default", "保持未知，不推断为未训练")
            put("compensation_boundary", "不跳餐、不清零主食、不极端压低热量；只撤掉重复加餐并恢复标准份量。")
            put("home_cooking", buildJsonObject { put("enabled", false) })
        }
        repository.save(
            EntityKind.PREFERENCES,
            buildJsonObject {
                put("kind", "settings")
                put("content", JsonObject(existing + ("timezone" to JsonPrimitive(normalized))).toString())
            }, entityId,
        )
        SyncWorker.enqueue(getApplication())
    }

    fun exportPortable(uri: Uri) = launchAction("加密 Portable Data 已导出") {
        val output = getApplication<Application>().contentResolver.openOutputStream(uri)
            ?: error("无法打开导出目标")
        _exportRecoveryKey.value = output.use { portable.export(it, encrypted = true) }
    }

    fun previewPortable(uri: Uri, recoveryKey: String, merge: Boolean) = launchAction("预检完成；确认后才会写入") {
        val input = getApplication<Application>().contentResolver.openInputStream(uri)
            ?: error("无法读取数据包")
        val mode = if (merge) ImportMode.MERGE else ImportMode.RESTORE
        val preview = input.use { portable.preview(it, recoveryKey.ifBlank { null }, mode) }
        _portableImport.value = PortableImportUi(uri, recoveryKey, mode, preview)
    }

    fun applyPortable() = launchAction("Portable Data 已导入") {
        val request = _portableImport.value ?: error("请先预检数据包")
        val input = getApplication<Application>().contentResolver.openInputStream(request.uri)
            ?: error("无法重新读取数据包")
        input.use { portable.import(it, request.recoveryKey.ifBlank { null }, request.mode) }
        _portableImport.value = null
    }

    fun cancelPortableImport() { _portableImport.value = null }

    fun clearExportRecoveryKey() { _exportRecoveryKey.value = null }

    fun beginRegistration(url: String, login: String, password: String, device: String) =
        launchAction(null) {
            _pendingRegistration.value = accounts.beginRegistration(url, login, password, device)
        }

    fun confirmRegistration(value: String) = launchAction("端到端加密同步已启用") {
        val pending = _pendingRegistration.value ?: error("注册确认已失效")
        accounts.confirmRegistration(pending, value)
        _pendingRegistration.value = null
        SyncWorker.enqueue(getApplication())
    }

    fun login(url: String, login: String, password: String, device: String, recovery: String) =
        launchAction("同步账户已解锁") {
            accounts.login(url, login, password, device, recovery)
            SyncWorker.enqueue(getApplication())
        }

    fun syncNow() = launchAction("同步任务已排队") { SyncWorker.enqueue(getApplication()) }
    fun syncOnDemandMediaNow() = launchAction("缺失照片按需同步完成") {
        val engine = app.syncEngineOrNull() ?: error("同步尚未启用或密钥未解锁")
        engine.run(includeOnDemandMedia = true)
    }
    fun setMediaPolicy(value: String) = launchAction("照片同步策略已更新") {
        val config = repository.syncConfiguration() ?: error("同步尚未启用")
        repository.putSyncConfiguration(config.copy(mediaPolicy = value, updatedAt = Instant.now().toString()))
        SyncWorker.enqueue(getApplication())
    }
    fun unlink() = launchAction("已取消同步；本地数据完整保留") { accounts.unlink() }
    fun createPairingQr() = launchAction("10 分钟配对二维码已生成") {
        _pairingQr.value = accounts.createPairingQr()
    }
    fun clearPairingQr() { _pairingQr.value = null }
    fun claimPairing(payload: String, login: String, password: String, device: String) =
        launchAction("新设备已通过二维码加入") {
            accounts.claimPairing(payload, login, password, device)
            SyncWorker.enqueue(getApplication())
        }
    fun refreshDevices() = launchAction(null) {
        _devices.value = accounts.devices().getValue("devices").jsonArray.map { value ->
            val item = value.jsonObject
            DeviceUi(
                item.getValue("id").jsonPrimitive.content,
                item.getValue("name").jsonPrimitive.content,
                item.getValue("current").jsonPrimitive.content.toBoolean(),
                item.getValue("revoked").jsonPrimitive.content.toBoolean(),
            )
        }
    }
    fun revokeDevice(id: String) = launchAction("设备已撤销") {
        accounts.revokeDevice(id)
        refreshDevices()
    }
    fun deleteSyncAccount(password: String) = launchAction("远端账户已删除；本机数据完整保留") {
        accounts.deleteAccount(password)
        _devices.value = emptyList()
    }
    fun prepareKeyRotation() = launchAction(null) {
        _pendingRotationRecovery.value = keyRotation.prepare()
    }
    fun confirmKeyRotation(value: String) = launchAction("安全轮换完成；其他设备已撤销") {
        keyRotation.confirm(value)
        _pendingRotationRecovery.value = null
        refreshDevices()
    }
    fun abortKeyRotation() = launchAction("密钥轮换已中止") {
        keyRotation.abort()
        _pendingRotationRecovery.value = null
    }

    fun resolveConflict(id: String, chooseLocal: Boolean) = launchAction("冲突已解决并生成合并 revision") {
        val conflict = repository.conflict(id) ?: error("冲突不存在")
        val local = repository.json.decodeFromString<org.mealcircuit.app.domain.DomainRevision>(conflict.localRevisionJson)
        val remote = repository.json.decodeFromString<org.mealcircuit.app.domain.DomainRevision>(conflict.remoteRevisionJson)
        val selected = if (chooseLocal) local else remote
        val canonicalId = minOf(local.entityId, remote.entityId)
        val merged = org.mealcircuit.app.domain.DomainRevision.create(
            kind = local.entityKind, entityId = canonicalId,
            parents = listOf(local.revisionId, remote.revisionId), deviceId = repository.deviceId,
            payload = if (local.entityId == remote.entityId) selected.payload
                else canonicalizeLogicalPayload(local.entityKind, selected.payload, canonicalId),
            deleted = selected.deleted,
        )
        val alias = if (local.entityId == remote.entityId) null else if (local.entityId == canonicalId) remote else local
        val tombstone = alias?.let {
            DomainRevision.create(
                it.entityKind, it.entityId, listOf(it.revisionId), repository.deviceId,
                it.payload, deleted = true,
            )
        }
        repository.commitConflictResolution(id, merged, tombstone)
        SyncWorker.enqueue(getApplication())
    }

    private fun launchAction(success: String?, action: suspend () -> Unit) {
        viewModelScope.launch {
            runCatching { action() }.onSuccess {
                if (success != null) _message.value = UiMessage(success)
            }.onFailure { _message.value = UiMessage(it.message ?: "操作失败", true) }
        }
    }

    private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }
}

private fun MaterializedRecordEntity.activePayload(): Boolean = !deleted && runCatching {
    Json.parseToJsonElement(payloadJson).jsonObject["active"]?.jsonPrimitive?.content in setOf("1", "true")
}.getOrDefault(false)

private fun List<MaterializedRecordEntity>.preferenceContent(kind: String): String? = firstNotNullOfOrNull { record ->
    runCatching {
        val payload = Json.parseToJsonElement(record.payloadJson).jsonObject
        payload.getValue("content").jsonPrimitive.content.takeIf {
            payload["kind"]?.jsonPrimitive?.content == kind
        }
    }.getOrNull()
}

private fun MaterializedRecordEntity.publishedCheckinPayload(): JsonObject? = runCatching {
    val payload = Json.parseToJsonElement(payloadJson).jsonObject
    val published = payload.getValue("modules").jsonArray.mapNotNull { element ->
        val aggregate = element.jsonObject
        val module = aggregate.getValue("module").jsonObject
        val version = module["version"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
        if (version <= 0) null else JsonObject(
            aggregate + ("module" to JsonObject(module - "draft_json"))
        )
    }
    if (published.isEmpty()) null else JsonObject(payload + ("modules" to kotlinx.serialization.json.JsonArray(published)))
}.getOrNull()
