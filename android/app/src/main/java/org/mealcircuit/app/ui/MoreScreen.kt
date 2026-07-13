package org.mealcircuit.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.domain.EntityKind
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

private enum class MoreTab(val label: String) {
    HISTORY("历史"), MEMORY("记忆"), SETTINGS("AI 与导入"), SYNC("同步"), CONFLICTS("冲突")
}

@Composable
fun MoreScreen(viewModel: MainViewModel) {
    var tab by remember { mutableStateOf(MoreTab.HISTORY) }
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            MoreTab.entries.forEach { item ->
                FilterChip(selected = tab == item, onClick = { tab = item }, label = { Text(item.label) })
            }
        }
        when (tab) {
            MoreTab.HISTORY -> HistoryScreen(viewModel)
            MoreTab.MEMORY -> MemoryScreen(viewModel)
            MoreTab.SETTINGS -> SettingsScreen(viewModel)
            MoreTab.SYNC -> SyncSettingsScreen(viewModel)
            MoreTab.CONFLICTS -> ConflictScreen(viewModel)
        }
    }
}

@Composable
private fun HistoryScreen(viewModel: MainViewModel) {
    val reviews by viewModel.repository.observe(EntityKind.DAILY_REVIEW).collectAsState(emptyList())
    val heads by viewModel.repository.observeHeads().collectAsState(emptyList())
    val headMap = heads.associate { it.entityId to it.revisionId }
    val stale = reviews.count { record ->
        runCatching {
            val review = kotlinx.serialization.json.Json.parseToJsonElement(record.payloadJson).jsonObject
                .getValue("review").jsonObject
            val provenance = review["result_provenance_json"]?.jsonObject ?: return@runCatching false
            provenance.getValue("source_revisions").jsonArray.any { source ->
                val item = source.jsonObject
                headMap[item.getValue("entity_id").jsonPrimitive.content] != item.getValue("revision_id").jsonPrimitive.content
            }
        }.getOrDefault(false)
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SectionTitle("历史建议", "AI 结果是派生数据；来源变化后原版本仍保留。")
        if (stale > 0) Text("$stale 条结果的来源已变化，已标记过期但未覆盖或删除。")
        RecordList(reviews, "没有历史复盘", "完成每日复盘后会按日期出现在这里。")
    }
}

@Composable
private fun MemoryScreen(viewModel: MainViewModel) {
    var value by remember { mutableStateOf("") }
    var adjustment by remember { mutableStateOf("") }
    var selectedMemory by remember { mutableStateOf<String?>(null) }
    var selectedAdjustment by remember { mutableStateOf<String?>(null) }
    val memories by viewModel.repository.observe(EntityKind.MEMORY).collectAsState(emptyList())
    val adjustments by viewModel.repository.observe(EntityKind.ADJUSTMENT).collectAsState(emptyList())
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SectionTitle("长期记忆与调整", "写下稳定偏好、肠胃触发或当前约束。")
        OutlinedTextField(value, { value = it }, Modifier.fillMaxWidth(), label = { Text("新增长期记忆") }, minLines = 3)
        Button(onClick = { viewModel.addMemory(value); value = "" }, enabled = value.isNotBlank()) { Text("保存记忆") }
        RecordList(memories, "没有长期记忆", "已验证的偏好会进入后续上下文。") { selectedMemory = it.entityId }
        selectedMemory?.let { id ->
            androidx.compose.material3.OutlinedButton(onClick = { viewModel.setActive(EntityKind.MEMORY, id, false); selectedMemory = null }) {
                Text("停用选中记忆")
            }
        }
        SectionTitle("当前调整")
        OutlinedTextField(adjustment, { adjustment = it }, Modifier.fillMaxWidth(), label = { Text("当前有效调整") }, minLines = 2)
        Button(
            onClick = { viewModel.addAdjustment(adjustment); adjustment = "" },
            enabled = adjustment.isNotBlank(),
        ) { Text("保存调整") }
        RecordList(adjustments, "没有当前调整", "桌面端或导入的数据会同步显示。") { selectedAdjustment = it.entityId }
        selectedAdjustment?.let { id ->
            androidx.compose.material3.OutlinedButton(onClick = { viewModel.setActive(EntityKind.ADJUSTMENT, id, false); selectedAdjustment = null }) {
                Text("停用选中调整")
            }
        }
    }
}
