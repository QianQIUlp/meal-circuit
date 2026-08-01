package org.mealcircuit.app.ui

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.Button
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.Saver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.data.MaterializedRecordEntity
import org.mealcircuit.app.domain.CheckinContract
import org.mealcircuit.app.domain.CheckinModule
import org.mealcircuit.app.domain.CheckinQuestion
import org.mealcircuit.app.domain.EntityKind
import java.time.LocalDate
import java.time.Duration
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlinx.coroutines.delay

private val answersSaver = Saver<Map<String, Map<String, JsonElement>>, String>(
    save = { answers -> JsonObject(answers.mapValues { JsonObject(it.value) }).toString() },
    restore = { encoded ->
        runCatching {
            Json.parseToJsonElement(encoded).jsonObject.mapValues { (_, value) -> value.jsonObject.toMap() }
        }.getOrDefault(emptyMap())
    },
)

private val otherAnswersSaver = Saver<Map<String, Map<String, String>>, String>(
    save = { answers ->
        JsonObject(answers.mapValues { (_, fields) -> JsonObject(fields.mapValues { JsonPrimitive(it.value) }) }).toString()
    },
    restore = { encoded ->
        runCatching {
            Json.parseToJsonElement(encoded).jsonObject.mapValues { (_, value) ->
                value.jsonObject.mapValues { (_, field) -> field.jsonPrimitive.content }
            }
        }.getOrDefault(emptyMap())
    },
)

private val skippedModulesSaver = Saver<Set<String>, String>(
    save = { skipped -> JsonArray(skipped.sorted().map(::JsonPrimitive)).toString() },
    restore = { encoded ->
        runCatching { Json.parseToJsonElement(encoded).jsonArray.map { it.jsonPrimitive.content }.toSet() }
            .getOrDefault(emptySet())
    },
)

internal data class VisibleCheckinInput(
    val answers: Map<String, Map<String, JsonElement>>,
    val other: Map<String, Map<String, String>>,
    val skipped: Set<String>,
) {
    val hasContent: Boolean = answers.values.any { it.isNotEmpty() } ||
        other.values.any { fields -> fields.values.any(String::isNotBlank) } ||
        skipped.isNotEmpty()
}

internal fun visibleCheckinInput(
    enabledModules: Set<String>,
    answers: Map<String, Map<String, JsonElement>>,
    other: Map<String, Map<String, String>>,
    skipped: Set<String>,
) = VisibleCheckinInput(
    answers = answers.filterKeys(enabledModules::contains),
    other = other.filterKeys(enabledModules::contains),
    skipped = skipped.intersect(enabledModules),
)

@Composable
fun CheckinEditor(viewModel: MainViewModel) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val contract = remember { CheckinContract.load(context) }
    val enabledModules by viewModel.checkinModules.collectAsState()
    val checkins by viewModel.repository.observe(EntityKind.CHECKIN_DAY).collectAsState(emptyList())
    val timezone by viewModel.timezone.collectAsState()
    var today by remember(timezone) { mutableStateOf(LocalDate.now(ZoneId.of(timezone)).toString()) }
    LaunchedEffect(timezone) {
        val zone = ZoneId.of(timezone)
        while (true) {
            val now = ZonedDateTime.now(zone)
            today = now.toLocalDate().toString()
            val nextMidnight = now.toLocalDate().plusDays(1).atStartOfDay(zone)
            delay(Duration.between(now, nextMidnight).toMillis().coerceAtLeast(1_000L))
        }
    }
    var answers by rememberSaveable(stateSaver = answersSaver) {
        mutableStateOf<Map<String, Map<String, JsonElement>>>(emptyMap())
    }
    var other by rememberSaveable(stateSaver = otherAnswersSaver) {
        mutableStateOf<Map<String, Map<String, String>>>(emptyMap())
    }
    var skipped by rememberSaveable(stateSaver = skippedModulesSaver) { mutableStateOf<Set<String>>(emptySet()) }
    var formDate by rememberSaveable { mutableStateOf(today) }
    var hydratedDate by rememberSaveable { mutableStateOf<String?>(null) }
    val formCheckin = remember(checkins, formDate) { checkins.firstOrNull { checkinDate(it) == formDate } }
    val visibleInput = visibleCheckinInput(enabledModules, answers, other, skipped)
    val hasContent = visibleInput.hasContent

    LaunchedEffect(today, hasContent) {
        if (!hasContent && formDate != today) {
            formDate = today
            hydratedDate = null
        }
    }

    LaunchedEffect(formDate, formCheckin?.updatedAt) {
        if (hydratedDate != formDate && !hasContent) {
            if (formCheckin != null) {
                hydrateCheckin(formCheckin)?.let { hydrated ->
                    answers = hydrated.answers
                    other = hydrated.other
                    skipped = hydrated.skipped
                }
            }
            hydratedDate = formDate
        }
    }

    if (formDate != today) {
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("日期已切换到 $today")
                Text("当前表单仍属于 $formDate；保存或发布时不会写入今天。")
            }
        }
    }

    contract.modules.filter { it.key in enabledModules }.forEach { module ->
        CheckinModuleEditor(
            module = module,
            values = answers[module.key].orEmpty(),
            other = other[module.key].orEmpty(),
            skipped = module.key in skipped,
            onValue = { question, value ->
                answers = answers + (module.key to (answers[module.key].orEmpty() + (question.id to value)))
                skipped = skipped - module.key
                hydratedDate = formDate
            },
            onOther = { question, value ->
                other = other + (module.key to (other[module.key].orEmpty() + (question.id to value)))
                hydratedDate = formDate
            },
            onSkip = { value ->
                skipped = if (value) skipped + module.key else skipped - module.key
                if (value) answers = answers - module.key
                hydratedDate = formDate
            },
        )
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        OutlinedButton(
            onClick = {
                val savedDate = formDate
                viewModel.saveCheckinDraft(
                    formDate,
                    visibleInput.answers,
                    visibleInput.other,
                    visibleInput.skipped,
                    onSuccess = {
                        if (savedDate != today) {
                            answers = emptyMap(); other = emptyMap(); skipped = emptySet()
                            formDate = today; hydratedDate = null
                        }
                    },
                )
            },
            enabled = hasContent,
            modifier = Modifier.weight(1f),
        ) { Text(if (formDate == today) "保存草稿" else "保存旧日草稿") }
        Button(
            onClick = {
                viewModel.publishCheckin(
                    formDate,
                    visibleInput.answers,
                    visibleInput.other,
                    visibleInput.skipped,
                    onSuccess = {
                        answers = emptyMap(); other = emptyMap(); skipped = emptySet()
                        formDate = today; hydratedDate = null
                    },
                )
            },
            enabled = hasContent,
            modifier = Modifier.weight(1f),
        ) { Text("发布状态") }
    }
}

@Composable
private fun CheckinModuleEditor(
    module: CheckinModule,
    values: Map<String, JsonElement>,
    other: Map<String, String>,
    skipped: Boolean,
    onValue: (CheckinQuestion, JsonElement) -> Unit,
    onOther: (CheckinQuestion, String) -> Unit,
    onSkip: (Boolean) -> Unit,
) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(module.label, style = androidx.compose.material3.MaterialTheme.typography.titleMedium)
            Text(module.description, style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
            FilterChip(selected = skipped, onClick = { onSkip(!skipped) }, label = { Text("今天跳过（保持未知）") })
            if (!skipped) {
                val known = linkedMapOf<String, JsonElement>()
                module.questions.forEach { question ->
                    if (question.applicable(known)) {
                        QuestionEditor(question, values[question.id], other[question.id].orEmpty(), onValue, onOther)
                        values[question.id]?.let { known[question.id] = it }
                    }
                }
            }
        }
    }
}

@Composable
private fun QuestionEditor(
    question: CheckinQuestion,
    value: JsonElement?,
    otherText: String,
    onValue: (CheckinQuestion, JsonElement) -> Unit,
    onOther: (CheckinQuestion, String) -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(question.label)
        when (question.type) {
            "number" -> OutlinedTextField(
                value?.jsonPrimitive?.content.orEmpty(),
                { onValue(question, JsonPrimitive(it)) },
                Modifier.fillMaxWidth(),
                suffix = { question.suffix?.let { Text(it) } },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                singleLine = true,
            )
            "single", "duration" -> OptionRow(question, value, false, onValue)
            "multi" -> OptionRow(question, value, true, onValue)
        }
        if (question.allowOtherText && "other" in selectedValues(value)) {
            OutlinedTextField(
                otherText,
                { onOther(question, it) },
                Modifier.fillMaxWidth(),
                label = { Text("其他说明") },
                singleLine = true,
            )
        }
    }
}

@Composable
private fun OptionRow(
    question: CheckinQuestion,
    value: JsonElement?,
    multi: Boolean,
    onValue: (CheckinQuestion, JsonElement) -> Unit,
) {
    val selected = selectedValues(value)
    Row(
        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        question.options.forEach { option ->
            FilterChip(
                selected = option.value in selected,
                onClick = {
                    val next = if (multi) {
                        if (option.value in selected) selected - option.value else selected + option.value
                    } else setOf(option.value)
                    onValue(
                        question,
                        if (multi) JsonArray(next.sorted().map(::JsonPrimitive)) else JsonPrimitive(option.value),
                    )
                },
                label = { Text(option.label) },
            )
        }
    }
}

private fun selectedValues(value: JsonElement?): Set<String> = when (value) {
    is JsonArray -> value.map { it.jsonPrimitive.content }.toSet()
    null -> emptySet()
    else -> setOf(value.jsonPrimitive.content)
}

private data class HydratedCheckin(
    val answers: Map<String, Map<String, JsonElement>>,
    val other: Map<String, Map<String, String>>,
    val skipped: Set<String>,
)

private fun checkinDate(record: MaterializedRecordEntity): String? = runCatching {
    Json.parseToJsonElement(record.payloadJson).jsonObject
        .getValue("checkin").jsonObject.getValue("checkin_date").jsonPrimitive.content
}.getOrNull()

private fun hydrateCheckin(record: MaterializedRecordEntity): HydratedCheckin? = runCatching {
    val answers = mutableMapOf<String, Map<String, JsonElement>>()
    val other = mutableMapOf<String, Map<String, String>>()
    val skipped = mutableSetOf<String>()
    val payload = Json.parseToJsonElement(record.payloadJson).jsonObject
    payload.getValue("modules").jsonArray.forEach { entry ->
        val module = entry.jsonObject.getValue("module").jsonObject
        val key = module.getValue("module_key").jsonPrimitive.content
        if (module["status"]?.jsonPrimitive?.content == "skipped") skipped += key
        val draft = module["draft_json"]
        val source = if (draft != null && draft !is JsonNull) draft.jsonObject
            else module["answers_json"]?.jsonObject ?: JsonObject(emptyMap())
        val moduleAnswers = mutableMapOf<String, JsonElement>()
        val moduleOther = mutableMapOf<String, String>()
        source.forEach { (questionId, normalized) ->
            if (normalized is JsonObject) {
                normalized["other_text"]?.jsonPrimitive?.content?.let { moduleOther[questionId] = it }
                moduleAnswers[questionId] = normalized["values"] ?: normalized["value"] ?: normalized
            } else {
                moduleAnswers[questionId] = normalized
            }
        }
        if (moduleAnswers.isNotEmpty()) answers[key] = moduleAnswers
        if (moduleOther.isNotEmpty()) other[key] = moduleOther
    }
    HydratedCheckin(answers, other, skipped)
}.getOrNull()
