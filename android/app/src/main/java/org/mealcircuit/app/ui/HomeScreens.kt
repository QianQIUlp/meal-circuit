package org.mealcircuit.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.domain.EntityKind
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

@Composable
fun HomeScreen(viewModel: MainViewModel) {
    val tasks by viewModel.repository.observe(EntityKind.TASK).collectAsState(emptyList())
    val records by viewModel.repository.observe(EntityKind.DAILY_RECORD).collectAsState(emptyList())
    val reviews by viewModel.repository.observe(EntityKind.DAILY_REVIEW).collectAsState(emptyList())
    val pending by viewModel.repository.observePendingCount().collectAsState(0)
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        Card(
            Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer),
        ) {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("本地数据是主副本", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
                Text("断网时仍可记录、查看、分析与导出；启用同步后，待上传 $pending 项。")
                Row(horizontalArrangement = Arrangement.spacedBy(20.dp)) {
                    Metric("任务", tasks.size)
                    Metric("饮食记录", records.size)
                    Metric("复盘", reviews.size)
                }
            }
        }
        SectionTitle("最近记录", "全部内容直接来自 Room，本页不依赖网络。")
        RecordList(records.take(5), "还没有饮食记录", "从“今日”或“记录”开始写下第一餐。")
        SectionTitle("处理队列")
        RecordList(tasks.take(5), "没有待办任务", "照片与原材料任务会显示在这里。")
    }
}

@Composable
private fun Metric(label: String, value: Int) {
    Column {
        Text(value.toString(), style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text(label, style = MaterialTheme.typography.labelMedium)
    }
}

@Composable
fun DailyScreen(viewModel: MainViewModel) {
    var record by remember { mutableStateOf("") }
    val records by viewModel.repository.observe(EntityKind.DAILY_RECORD).collectAsState(emptyList())
    val reviews by viewModel.repository.observe(EntityKind.DAILY_REVIEW).collectAsState(emptyList())
    val timezone by viewModel.timezone.collectAsState()
    val today = java.time.LocalDate.now(java.time.ZoneId.of(timezone)).toString()
    val reviewPending = reviews.any { row ->
        runCatching {
            val review = kotlinx.serialization.json.Json.parseToJsonElement(row.payloadJson).jsonObject
                .getValue("review").jsonObject
            review.getValue("review_date").jsonPrimitive.content == today &&
                review.getValue("status").jsonPrimitive.content == "pending"
        }.getOrDefault(false)
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        SectionTitle("今天吃了什么", "先写入本机；模型生成是独立、可选的动作。")
        OutlinedTextField(
            record, { record = it }, Modifier.fillMaxWidth(),
            label = { Text("自然语言饮食记录") }, minLines = 4,
        )
        Button(
            onClick = { viewModel.addDailyRecord(record); record = "" },
            enabled = record.isNotBlank(),
            modifier = Modifier.align(Alignment.End),
        ) { Text("保存记录") }
        SectionTitle("今日状态", "发布的信息进入复盘上下文；缺失仍保持未知。")
        CheckinEditor(viewModel)
        SectionTitle("最近饮食记录")
        androidx.compose.material3.OutlinedButton(
            onClick = viewModel::generateDailyReview,
            enabled = reviewPending,
            modifier = Modifier.fillMaxWidth(),
        ) { Text("使用本设备配置的 AI 生成今日复盘") }
        RecordList(records.take(14), "今天还没有记录", "保存后会立即出现在这里。")
    }
}
