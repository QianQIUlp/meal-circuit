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
import androidx.compose.material3.OutlinedButton
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
import org.mealcircuit.app.data.MaterializedRecordEntity
import org.mealcircuit.app.domain.EntityKind
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.time.LocalDate
import java.time.ZoneId

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
fun TodayScreen(viewModel: MainViewModel) {
    var record by remember { mutableStateOf("") }
    var editingRecordId by remember { mutableStateOf<String?>(null) }
    val records by viewModel.repository.observe(EntityKind.DAILY_RECORD).collectAsState(emptyList())
    val reviews by viewModel.repository.observe(EntityKind.DAILY_REVIEW).collectAsState(emptyList())
    val timezone by viewModel.timezone.collectAsState()
    val today = LocalDate.now(ZoneId.of(timezone)).toString()
    val todayRecords = records.filter { recordDate(it) == today }.sortedByDescending { it.updatedAt }
    val plans = publishedPlans(reviews)
    val planForToday = plans.firstOrNull { it.planDate == today }
    val reviewToday = plans.firstOrNull { it.reviewDate == today }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        SectionTitle("今天", "记录、状态和 Windows 已发布的安排会在设备间同步；手机不会绕过多阶段审查重新发布计划。")
        SectionTitle("记一笔", "写下吃了什么、执行阻力或真实变化。保存后仍可直接修改这条记录。")
        OutlinedTextField(
            record, { record = it }, Modifier.fillMaxWidth(),
            label = { Text(if (editingRecordId == null) "自然语言饮食记录" else "修改这条记录") }, minLines = 4,
        )
        Row(Modifier.align(Alignment.End), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            editingRecordId?.let {
                OutlinedButton(onClick = { editingRecordId = null; record = "" }) { Text("取消修改") }
            }
            Button(
                onClick = {
                    editingRecordId?.let { viewModel.updateDailyRecord(it, record) }
                        ?: viewModel.addDailyRecord(record)
                    editingRecordId = null
                    record = ""
                },
                enabled = record.isNotBlank(),
            ) { Text(if (editingRecordId == null) "记下来" else "保存修改") }
        }
        RecordList(
            todayRecords,
            "今天还没有记录",
            "从早餐、状态或任何影响执行的变化开始写下第一条。",
        ) { selected ->
            editingRecordId = selected.entityId
            record = dailyRecordText(selected)
        }
        SectionTitle("今日状态", "发布的信息进入复盘上下文；缺失仍保持未知。")
        CheckinEditor(viewModel)
        reviewToday?.let { plan ->
            SectionTitle("今天最重要的方向", "来自今天的真实记录和已发布复盘。")
            PublishedPlanCard(plan, showReviewDate = false)
        }
        planForToday?.takeIf { it != reviewToday }?.let { plan ->
            SectionTitle("今天的安排", "这是 Windows 已审查并发布的可执行计划。")
            PublishedPlanCard(plan, showReviewDate = true)
        }
    }
}

@Composable
fun PlansScreen(viewModel: MainViewModel) {
    val reviews by viewModel.repository.observe(EntityKind.DAILY_REVIEW).collectAsState(emptyList())
    val plans = publishedPlans(reviews)
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SectionTitle("计划", "只显示已经完成独立审查并正式发布的安排；草案不会在手机端冒充正式计划。")
        if (plans.isEmpty()) {
            EmptyState("还没有已发布计划", "在 Windows 端完成当天复盘并采用安排后，这里会自动显示。")
        } else {
            plans.forEach { PublishedPlanCard(it, showReviewDate = true) }
        }
    }
}

@Composable
internal fun PublishedPlanCard(plan: PublishedPlan, showReviewDate: Boolean) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(
                if (showReviewDate) "计划日期 · ${plan.planDate}" else "已发布复盘",
                style = MaterialTheme.typography.labelLarge,
            )
            Text(
                plan.summary,
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.SemiBold,
            )
            if (plan.coreAdvice.isNotEmpty()) {
                Text("今天最重要的方向", style = MaterialTheme.typography.titleMedium)
                plan.coreAdvice.forEach { Text("• $it") }
            }
            if (plan.problems.isNotEmpty()) {
                Text("这份安排回应", style = MaterialTheme.typography.titleMedium)
                plan.problems.forEach { Text("• $it") }
            }
            plan.strategy?.let { strategy ->
                Text("采用策略：$strategy", style = MaterialTheme.typography.bodyMedium)
            }
            plan.tradeoffs.forEach { Text("取舍：$it", style = MaterialTheme.typography.bodySmall) }
            plan.nutrition?.let { Text("全天估算：$it", style = MaterialTheme.typography.bodyMedium) }
            if (plan.rationale.isNotEmpty()) {
                Text("为什么这样安排", style = MaterialTheme.typography.titleMedium)
            }
            plan.rationale.forEach { Text("• $it") }
            plan.evidence.forEach { Text("参考：$it", style = MaterialTheme.typography.bodySmall) }
            plan.meals.forEach { meal ->
                Column(
                    Modifier.fillMaxWidth().padding(top = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Text(
                        listOfNotNull(meal.name, mealModeLabel(meal.mode)).joinToString(" · "),
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                    )
                    meal.purpose?.let { Text(it) }
                    meal.foods.takeIf { it.isNotEmpty() }?.let { Text("食物：${it.joinToString("、")}") }
                    meal.whyToday?.let {
                        Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    meal.wholeDayRole?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                    meal.portions.forEach { portion ->
                        Text("${portion.item} · ${portion.amount}", style = MaterialTheme.typography.bodyMedium)
                        portion.increaseIf?.let { Text("吃不饱时：$it", style = MaterialTheme.typography.bodySmall) }
                        portion.decreaseIf?.let { Text("食欲低时：$it", style = MaterialTheme.typography.bodySmall) }
                    }
                    meal.eatOutGuidance.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
                    meal.adjustments.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
                    meal.executionRisks.forEach { Text("留意：$it", style = MaterialTheme.typography.bodySmall) }
                }
            }
            plan.dayAdjustments.forEach { Text("调整条件：$it", style = MaterialTheme.typography.bodySmall) }
        }
    }
}

private fun recordDate(record: MaterializedRecordEntity): String? = runCatching {
    Json.parseToJsonElement(record.payloadJson).jsonObject["record_date"]?.jsonPrimitive?.contentOrNull
}.getOrNull()

private fun dailyRecordText(record: MaterializedRecordEntity): String = runCatching {
    Json.parseToJsonElement(record.payloadJson).jsonObject["raw_input"]?.jsonPrimitive?.contentOrNull.orEmpty()
}.getOrDefault("")
