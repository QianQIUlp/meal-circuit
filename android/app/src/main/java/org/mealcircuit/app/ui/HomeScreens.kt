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
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
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
    val latestPublished = reviews.mapNotNull { row ->
        runCatching {
            val review = Json.parseToJsonElement(row.payloadJson).jsonObject.getValue("review").jsonObject
            if (review.getValue("status").jsonPrimitive.content != "completed") null
            else review.getValue("review_date").jsonPrimitive.content to review.getValue("result_json").jsonObject
        }.getOrNull()
    }.maxByOrNull { it.first }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        SectionTitle("今天吃了什么", "记录会在设备间同步，并用于之后的复盘和安排。")
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
        latestPublished?.let { (reviewDate, result) ->
            PublishedAgentPlan(reviewDate, result)
        }
        RecordList(records.take(14), "今天还没有记录", "保存后会立即出现在这里。")
    }
}

@Composable
private fun PublishedAgentPlan(reviewDate: String, result: JsonObject) {
    val menu = result["tomorrow_menu"] as? JsonObject
    val rationale = result["planning_rationale"]?.jsonArray.orEmpty().mapNotNull {
        runCatching { it.jsonPrimitive.content }.getOrNull()
    }
    val coreAdvice = result["core_advice"]?.jsonArray.orEmpty().mapNotNull {
        runCatching { it.jsonPrimitive.content }.getOrNull()
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("当天复盘 · $reviewDate", style = MaterialTheme.typography.labelLarge)
            Text(
                result["case_summary"]?.jsonPrimitive?.contentOrNull
                    ?: result["one_line_review"]?.jsonPrimitive?.contentOrNull
                    ?: "已同步正式计划",
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.SemiBold,
            )
            if (coreAdvice.isNotEmpty()) {
                Text("今天最重要的方向", style = MaterialTheme.typography.titleMedium)
                coreAdvice.forEach { Text("• $it") }
            }
            if (rationale.isNotEmpty()) {
                Text("为什么这样安排", style = MaterialTheme.typography.titleMedium)
            }
            rationale.forEach { Text("• $it") }
            menu?.get("meals")?.jsonArray.orEmpty().forEach { element ->
                val meal = element.jsonObject
                val name = meal["name"]?.jsonPrimitive?.contentOrNull ?: "餐次"
                val purpose = meal["purpose"]?.jsonPrimitive?.contentOrNull
                    ?: meal["portion_guidance"]?.jsonPrimitive?.contentOrNull.orEmpty()
                Column(
                    Modifier.fillMaxWidth().padding(top = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Text(name, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                    if (purpose.isNotBlank()) Text(purpose)
                    meal["why_today"]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }?.let {
                        Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    meal["portion_contracts"]?.jsonArray.orEmpty().forEach { contractElement ->
                        val contract = contractElement.jsonObject
                        val grams = contract["gram_range"]?.let { range ->
                            runCatching { range.jsonArray.joinToString("–") { it.jsonPrimitive.content } + "g" }.getOrNull()
                        } ?: "克数未知"
                        val measure = contract["household_measure"]?.jsonPrimitive?.contentOrNull.orEmpty()
                        val basis = when (contract["measurement_basis"]?.jsonPrimitive?.contentOrNull) {
                            "raw" -> "生重"
                            "cooked" -> "熟重"
                            "as_served" -> "上桌重量"
                            else -> ""
                        }
                        Text(
                            listOfNotNull(
                                contract["item"]?.jsonPrimitive?.contentOrNull,
                                grams,
                                basis.takeIf { it.isNotBlank() },
                                measure.takeIf { it.isNotBlank() },
                            ).joinToString(" · "),
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        contract["increase_if"]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }?.let {
                            Text("吃不饱时：$it", style = MaterialTheme.typography.bodySmall)
                        }
                        contract["decrease_if"]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }?.let {
                            Text("食欲低时：$it", style = MaterialTheme.typography.bodySmall)
                        }
                    }
                }
            }
        }
    }
}
