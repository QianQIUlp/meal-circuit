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
                    put("created_at", Instan