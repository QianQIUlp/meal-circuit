package org.mealcircuit.app.ui

import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.domain.CheckinContract
import org.mealcircuit.app.domain.CheckinModule
import org.mealcircuit.app.domain.CheckinQuestion

@Composable
fun CheckinEditor(viewModel: MainViewModel) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val contract = remember { CheckinContract.load(context) }
    val enabledModules by viewModel.checkinModules.collectAsState()
    var answers by remember { mutableStateOf<Map<String, Map<String, JsonElement>>>(emptyMap()) }
    var other by remember { mutableStateOf<Map<String, Map<String, String>>>(emptyMap()) }
    var skipped by remember { mutableStateOf<Set<String>>(emptySet()) }

    contract.modules.filter { it.key in enabledModules }.forEach { module ->
        CheckinModuleEditor(
            module = module,
            values = answers[module.key].orEmpty(),
            other = other[module.key].orEmpty(),
            skipped = module.key in skipped,
            onValue = { question, value ->
                answers = answers + (module.key to (answers[module.key].orEmpty() + (question.id to value)))
                skipped = skipped - module.key
            },
            onOther = { question, value ->
                other = other + (module.key to (other[module.key].orEmpty() + (question.id to value)))
            },
            onSkip = { value ->
                skipped = if (value) skipped + module.key else skipped - module.key
                if (value) answers = answers - module.key
            },
        )
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        OutlinedButton(
            onClick = { viewModel.saveCheckinDraft(answers, other, skipped) },
            enabled = answers.values.any { it.isNotEmpty() } || skipped.isNotEmpty(),
            modifier = Modifier.weight(1f),
        ) { Text("保存草稿") }
        Button(
            onClick = {
                viewModel.publishCheckin(answers, other, skipped)
                answers = emptyMap(); other = emptyMap(); skipped = emptySet()
            },
            enabled = answers.values.any { it.isNotEmpty() } || skipped.isNotEmpty(),
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
