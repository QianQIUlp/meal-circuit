package org.mealcircuit.app.domain

import android.content.Context
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

@Serializable
data class CheckinContract(
    @SerialName("schema_version") val schemaVersion: Int,
    val modules: List<CheckinModule>,
) {
    init { require(schemaVersion == 1) }

    fun module(key: String) = modules.first { it.key == key }

    companion object {
        fun load(context: Context, json: Json = Json { ignoreUnknownKeys = true }): CheckinContract =
            context.assets.open("checkin-modules-v1.json").bufferedReader().use {
                json.decodeFromString(it.readText())
            }
    }
}

@Serializable
data class CheckinModule(
    val key: String,
    val label: String,
    val description: String,
    val questions: List<CheckinQuestion>,
)

@Serializable
data class CheckinQuestion(
    val id: String,
    val label: String,
    val type: String,
    val options: List<CheckinOption> = emptyList(),
    val suffix: String? = null,
    val min: Double? = null,
    val max: Double? = null,
    val step: Double? = null,
    @SerialName("allow_other_text") val allowOtherText: Boolean = false,
    val `when`: CheckinCondition? = null,
    @SerialName("when_contains") val whenContains: CheckinContainsCondition? = null,
) {
    fun applicable(answers: Map<String, JsonElement>): Boolean {
        `when`?.let { condition ->
            val current = answers[condition.questionId]?.answerValues().orEmpty()
            if (current.none { it in condition.values }) return false
        }
        whenContains?.let { condition ->
            if (condition.value !in answers[condition.questionId]?.answerValues().orEmpty()) return false
        }
        return true
    }

    fun normalize(value: JsonElement, otherText: String = ""): JsonElement {
        return when (type) {
            "number" -> {
                val number = value.jsonPrimitive.content.toDoubleOrNull() ?: error("${label} 必须是数字")
                require(number >= (min ?: number) && number <= (max ?: number)) { "${label} 超出允许范围" }
                JsonPrimitive(number)
            }
            "single", "duration" -> {
                val selected = value.jsonPrimitive.content
                require(options.any { it.value == selected }) { "${label} 包含无效选项" }
                if (allowOtherText && selected == "other") {
                    require(otherText.isNotBlank() && otherText.length <= 200) { "${label} 的其他说明无效" }
                    buildJsonObject { put("value", selected); put("other_text", otherText.trim()) }
                } else JsonPrimitive(selected)
            }
            "multi" -> {
                val selected = value.answerValues().distinct()
                require(selected.isNotEmpty() && selected.all { item -> options.any { it.value == item } }) {
                    "${label} 至少需要一个有效选项"
                }
                if (allowOtherText && "other" in selected) {
                    require(otherText.isNotBlank() && otherText.length <= 200) { "${label} 的其他说明无效" }
                    buildJsonObject {
                        put("values", buildJsonArray { selected.forEach { add(JsonPrimitive(it)) } })
                        put("other_text", otherText.trim())
                    }
                } else buildJsonArray { selected.forEach { add(JsonPrimitive(it)) } }
            }
            else -> error("未知状态题型：$type")
        }
    }
}

@Serializable
data class CheckinOption(val value: String, val label: String)

@Serializable
data class CheckinCondition(
    @SerialName("question_id") val questionId: String,
    val values: List<String>,
)

@Serializable
data class CheckinContainsCondition(
    @SerialName("question_id") val questionId: String,
    val value: String,
)

fun CheckinModule.normalize(
    raw: Map<String, JsonElement>,
    other: Map<String, String>,
    requireComplete: Boolean,
): JsonObject {
    val accepted = linkedMapOf<String, JsonElement>()
    questions.forEach { question ->
        if (!question.applicable(accepted)) return@forEach
        val value = raw[question.id]
        if (value == null) {
            require(!requireComplete) { "缺少答案：${question.label}" }
        } else {
            accepted[question.id] = question.normalize(value, other[question.id].orEmpty())
        }
    }
    require(raw.keys.none { key -> questions.none { it.id == key } }) { "答案包含未知问题" }
    return JsonObject(accepted)
}

private fun JsonElement.answerValues(): List<String> = when (this) {
    is JsonArray -> map { it.jsonPrimitive.content }
    is JsonObject -> {
        get("values")?.jsonArray?.map { it.jsonPrimitive.content }
            ?: listOfNotNull(get("value")?.jsonPrimitive?.content)
    }
    else -> listOf(jsonPrimitive.content)
}
