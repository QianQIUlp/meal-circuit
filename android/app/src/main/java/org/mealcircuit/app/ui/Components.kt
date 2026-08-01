package org.mealcircuit.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.clickable
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Inbox
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.mealcircuit.app.data.MaterializedRecordEntity
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

@Composable
fun SectionTitle(title: String, supporting: String? = null) {
    Column(Modifier.fillMaxWidth().semantics { heading() }) {
        Text(title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
        supporting?.let { Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant) }
    }
}

@Composable
fun EmptyState(title: String, detail: String) {
    Column(
        Modifier.fillMaxWidth().padding(vertical = 32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Icon(Icons.Outlined.Inbox, null, Modifier.size(32.dp), tint = MaterialTheme.colorScheme.outline)
        Text(title, fontWeight = FontWeight.Medium)
        Text(detail, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
fun RecordList(
    records: List<MaterializedRecordEntity>,
    emptyTitle: String,
    emptyDetail: String,
    onSelect: ((MaterializedRecordEntity) -> Unit)? = null,
) {
    if (records.isEmpty()) {
        EmptyState(emptyTitle, emptyDetail)
        return
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        records.forEach { record ->
            val payload = runCatching { Json.parseToJsonElement(record.payloadJson).jsonObject }.getOrNull()
            val direct = listOf("name", "summary", "raw_input", "content", "original_input", "status")
                .firstNotNullOfOrNull { key -> payload?.get(key)?.jsonPrimitive?.contentOrNull }
            val nested = listOf("food", "task", "review", "checkin").firstNotNullOfOrNull { key ->
                val item = payload?.get(key) as? kotlinx.serialization.json.JsonObject ?: return@firstNotNullOfOrNull null
                listOf("name", "one_line_review", "status", "review_date", "checkin_date")
                    .firstNotNullOfOrNull { field -> item[field]?.jsonPrimitive?.contentOrNull }
            }
            val title = direct ?: nested
                ?: record.entityId
            Card(
                Modifier.fillMaxWidth().then(if (onSelect == null) Modifier else Modifier.clickable { onSelect(record) }),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = .42f)),
            ) {
                Row(
                    Modifier.fillMaxWidth().padding(16.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(title, maxLines = 2, fontWeight = FontWeight.Medium)
                        Text(formatLocalTimestamp(record.updatedAt), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text(entityKindLabel(record.entityKind), style = MaterialTheme.typography.labelMedium)
                }
            }
        }
    }
}

private fun formatLocalTimestamp(timestamp: String): String = runCatching {
    Instant.parse(timestamp).atZone(ZoneId.systemDefault())
        .format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm", Locale.getDefault()))
}.getOrDefault(timestamp)

private fun entityKindLabel(kind: String): String = when (kind) {
    "task" -> "任务"
    "task_input" -> "任务输入"
    "analysis_result" -> "分析结果"
    "correction" -> "校正"
    "food_item" -> "食品"
    "daily_record" -> "饮食记录"
    "checkin_day" -> "每日状态"
    "checkin_draft" -> "状态草稿"
    "daily_review" -> "每日复盘"
    "memory" -> "长期记忆"
    "adjustment" -> "当前调整"
    "preferences" -> "偏好设置"
    "asset" -> "资源"
    else -> "其他记录"
}
