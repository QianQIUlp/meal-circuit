package org.mealcircuit.app.domain

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.time.Instant
import java.time.LocalDate
import java.util.UUID

const val DOMAIN_SCHEMA_VERSION = 1

@Serializable
enum class EntityKind {
    @SerialName("task") TASK,
    @SerialName("task_input") TASK_INPUT,
    @SerialName("analysis_result") ANALYSIS_RESULT,
    @SerialName("correction") CORRECTION,
    @SerialName("food_item") FOOD_ITEM,
    @SerialName("daily_record") DAILY_RECORD,
    @SerialName("checkin_day") CHECKIN_DAY,
    @SerialName("checkin_draft") CHECKIN_DRAFT,
    @SerialName("daily_review") DAILY_REVIEW,
    @SerialName("memory") MEMORY,
    @SerialName("adjustment") ADJUSTMENT,
    @SerialName("preferences") PREFERENCES,
    @SerialName("asset") ASSET,
}

@Serializable
data class DomainRevision(
    @SerialName("schema_version") val schemaVersion: Int = DOMAIN_SCHEMA_VERSION,
    @SerialName("entity_id") val entityId: String,
    @SerialName("entity_kind") val entityKind: EntityKind,
    @SerialName("revision_id") val revisionId: String,
    @SerialName("parent_revision_ids") val parentRevisionIds: List<String>,
    @SerialName("created_at") val createdAt: String,
    @SerialName("author_device_id") val authorDeviceId: String,
    val deleted: Boolean,
    val payload: JsonObject,
) {
    fun validate(): DomainRevision {
        require(schemaVersion == DOMAIN_SCHEMA_VERSION) { "Unsupported domain schema $schemaVersion" }
        require(ID.matches(entityId) && ID.matches(revisionId) && ID.matches(authorDeviceId))
        require(parentRevisionIds.distinct().size == parentRevisionIds.size)
        parentRevisionIds.forEach { require(ID.matches(it)) }
        Instant.parse(createdAt)
        validatePayload()
        return this
    }

    private fun validatePayload() {
        val required = when (entityKind) {
            EntityKind.TASK -> listOf("task")
            EntityKind.TASK_INPUT -> listOf("task_id", "task_type", "input_version", "original_input", "input_history")
            EntityKind.ANALYSIS_RESULT -> listOf("source_entity_id", "source_kind", "result_version", "result", "provenance")
            EntityKind.CORRECTION -> listOf("id", "task_id", "correction_json", "created_at")
            EntityKind.FOOD_ITEM -> listOf("food", "history")
            EntityKind.DAILY_RECORD -> listOf("id", "record_date", "raw_input", "created_at")
            EntityKind.CHECKIN_DAY, EntityKind.CHECKIN_DRAFT -> listOf("checkin", "modules")
            EntityKind.DAILY_REVIEW -> listOf("review", "history")
            EntityKind.MEMORY -> listOf("id", "kind", "content", "active", "created_at", "updated_at")
            EntityKind.ADJUSTMENT -> listOf("id", "content", "active", "created_at", "updated_at")
            EntityKind.PREFERENCES -> listOf("kind", "content")
            EntityKind.ASSET -> listOf("sha256", "media_type", "extension", "byte_count")
        }
        require(required.all(payload::containsKey)) { "${entityKind.name} payload is incomplete" }
        val nested = when (entityKind) {
            EntityKind.TASK -> "task" to listOf("id", "type", "status", "created_at")
            EntityKind.FOOD_ITEM -> "food" to listOf("id", "name", "basis", "created_at", "updated_at")
            EntityKind.CHECKIN_DAY, EntityKind.CHECKIN_DRAFT -> "checkin" to listOf("id", "checkin_date", "created_at", "updated_at")
            EntityKind.DAILY_REVIEW -> "review" to listOf("id", "review_date", "status", "source_record_ids_json", "result_version", "created_at", "updated_at")
            else -> null
        }
        nested?.let { (key, fields) ->
            val value = payload[key] as? JsonObject ?: error("$key must be an object")
            require(fields.all(value::containsKey)) { "$key is incomplete" }
            require(value.getValue("id").jsonPrimitive.content == entityId) {
                "${entityKind.name} payload ID does not match entity_id"
            }
        }
        when (entityKind) {
            EntityKind.DAILY_RECORD -> {
                require(payload.getValue("id").jsonPrimitive.content == entityId)
                require(LocalDate.parse(payload.getValue("record_date").jsonPrimitive.content).toString() ==
                    payload.getValue("record_date").jsonPrimitive.content)
            }
            EntityKind.CORRECTION, EntityKind.MEMORY, EntityKind.ADJUSTMENT ->
                require(payload.getValue("id").jsonPrimitive.content == entityId)
            EntityKind.CHECKIN_DAY, EntityKind.CHECKIN_DRAFT ->
                LocalDate.parse(payload.getValue("checkin").jsonObject.getValue("checkin_date").jsonPrimitive.content)
            EntityKind.DAILY_REVIEW ->
                LocalDate.parse(payload.getValue("review").jsonObject.getValue("review_date").jsonPrimitive.content)
            else -> Unit
        }
        listOf("task_id", "source_entity_id").forEach { key ->
            payload[key]?.jsonPrimitive?.content?.let { require(ID.matches(it)) }
        }
    }

    companion object {
        private val ID = Regex("^[A-Za-z][A-Za-z0-9_-]{0,95}$")
        fun id(prefix: String): String = "${prefix}_${UUID.randomUUID()}"
        fun create(
            kind: EntityKind,
            entityId: String = id(kind.name.lowercase()),
            parents: List<String> = emptyList(),
            deviceId: String,
            payload: JsonObject,
            deleted: Boolean = false,
        ) = DomainRevision(
            entityId = entityId,
            entityKind = kind,
            revisionId = id("rev"),
            parentRevisionIds = parents,
            createdAt = Instant.now().toString(),
            authorDeviceId = deviceId,
            deleted = deleted,
            payload = payload,
        ).validate()
    }
}

data class MergeResult(val value: JsonObject, val conflicts: List<String>)

val STATE_TRANSITIONS = mapOf(
    "task" to mapOf("pending" to setOf("pending", "completed"), "completed" to setOf("completed")),
    "daily_review" to mapOf(
        "pending" to setOf("pending", "completed"),
        "completed" to setOf("completed", "pending"),
    ),
    "checkin_module" to mapOf(
        "not_started" to setOf("not_started", "in_progress", "completed", "skipped"),
        "in_progress" to setOf("not_started", "in_progress", "completed", "skipped"),
        "completed" to setOf("completed", "in_progress", "skipped"),
        "skipped" to setOf("skipped", "in_progress", "completed"),
    ),
)

fun validateStateChange(kind: EntityKind, before: JsonObject, after: JsonObject) {
    fun transition(machine: String, old: String, new: String) {
        require(new in STATE_TRANSITIONS.getValue(machine).getValue(old)) {
            "$machine does not allow $old -> $new"
        }
    }
    when (kind) {
        EntityKind.TASK -> transition(
            "task",
            before.getValue("task").jsonObject.getValue("status").jsonPrimitive.content,
            after.getValue("task").jsonObject.getValue("status").jsonPrimitive.content,
        )
        EntityKind.DAILY_REVIEW -> transition(
            "daily_review",
            before.getValue("review").jsonObject.getValue("status").jsonPrimitive.content,
            after.getValue("review").jsonObject.getValue("status").jsonPrimitive.content,
        )
        EntityKind.CHECKIN_DAY, EntityKind.CHECKIN_DRAFT -> {
            val old = before["modules"]?.jsonArray.orEmpty().associate { element ->
                val module = element.jsonObject.getValue("module").jsonObject
                module.getValue("module_key").jsonPrimitive.content to module.getValue("status").jsonPrimitive.content
            }
            after["modules"]?.jsonArray.orEmpty().forEach { element ->
                val module = element.jsonObject.getValue("module").jsonObject
                val key = module.getValue("module_key").jsonPrimitive.content
                val previous = old[key] ?: "not_started"
                transition("checkin_module", previous, module.getValue("status").jsonPrimitive.content)
            }
        }
        else -> Unit
    }
}

fun canonicalizeLogicalPayload(kind: EntityKind, payload: JsonObject, canonicalId: String): JsonObject {
    val value = payload.toMutableMap()
    when (kind) {
        EntityKind.CHECKIN_DAY, EntityKind.CHECKIN_DRAFT -> {
            value["checkin"] = JsonObject(payload.getValue("checkin").jsonObject + ("id" to JsonPrimitive(canonicalId)))
            value["modules"] = JsonArray(payload["modules"]?.jsonArray.orEmpty().map { element ->
                val item = element.jsonObject
                val module = JsonObject(item.getValue("module").jsonObject + ("checkin_id" to JsonPrimitive(canonicalId)))
                JsonObject(item + ("module" to module))
            })
        }
        EntityKind.DAILY_REVIEW -> {
            value["review"] = JsonObject(payload.getValue("review").jsonObject + ("id" to JsonPrimitive(canonicalId)))
            value["history"] = JsonArray(payload["history"]?.jsonArray.orEmpty().map { element ->
                JsonObject(element.jsonObject + ("review_id" to JsonPrimitive(canonicalId)))
            })
        }
        else -> Unit
    }
    return JsonObject(value)
}

/** Field-wise three-way merge. Timestamps are metadata and never select business values. */
fun threeWayMerge(base: JsonObject, local: JsonObject, remote: JsonObject): MergeResult {
    val result = mutableMapOf<String, JsonElement>()
    val conflicts = mutableListOf<String>()
    for (key in (base.keys + local.keys + remote.keys).sorted()) {
        val before = base[key]
        val left = local[key]
        val right = remote[key]
        when {
            left == right -> left?.let { result[key] = it }
            left == before -> right?.let { result[key] = it }
            right == before -> left?.let { result[key] = it }
            before is JsonObject && left is JsonObject && right is JsonObject -> {
                val child = threeWayMerge(before, left, right)
                result[key] = child.value
                conflicts += child.conflicts.map { "$key.$it" }
            }
            before is JsonArray && left is JsonArray && right is JsonArray &&
                listOf(before, left, right).all { array ->
                    array.all { it is JsonObject && it["id"] != null }
                } -> {
                val merged = mergeIdArray(before, left, right, key)
                result[key] = merged.first
                conflicts += merged.second
            }
            key.endsWith("_at") && left != null && right != null -> {
                result[key] = Json.parseToJsonElement(maxOf(left.toString(), right.toString()))
            }
            else -> {
                conflicts += key
                if (left != null && left !is JsonNull) result[key] = left
            }
        }
    }
    return MergeResult(JsonObject(result), conflicts)
}

private fun mergeIdArray(
    base: JsonArray,
    local: JsonArray,
    remote: JsonArray,
    path: String,
): Pair<JsonArray, List<String>> {
    fun JsonArray.byId() = associateBy { (it as JsonObject).getValue("id").jsonPrimitive.content }
    val before = base.byId()
    val left = local.byId()
    val right = remote.byId()
    val output = mutableListOf<JsonElement>()
    val conflicts = mutableListOf<String>()
    for (id in (before.keys + left.keys + right.keys).sorted()) {
        val a = before[id]
        val b = left[id]
        val c = right[id]
        when {
            b == c -> b?.let(output::add)
            b == a -> c?.let(output::add)
            c == a -> b?.let(output::add)
            a is JsonObject && b is JsonObject && c is JsonObject -> {
                val child = threeWayMerge(a, b, c)
                output += child.value
                conflicts += child.conflicts.map { "$path[$id].$it" }
            }
            else -> {
                conflicts += "$path[$id]"
                b?.let(output::add)
            }
        }
    }
    return JsonArray(output) to conflicts
}
