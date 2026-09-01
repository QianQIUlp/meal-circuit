package org.mealcircuit.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp

private data class LegalDocument(
    val label: String,
    val assetPath: String,
)

private val legalDocuments = listOf(
    LegalDocument("MealCircuit MIT 许可", "legal/MEALCIRCUIT_LICENSE.txt"),
    LegalDocument("第三方声明", "legal/THIRD_PARTY_NOTICES.md"),
    LegalDocument("Apache-2.0 许可", "legal/APACHE-2.0.txt"),
    LegalDocument("隐私说明", "legal/PRIVACY.md"),
    LegalDocument("安全说明", "legal/SECURITY.md"),
    LegalDocument("健康免责声明", "legal/DISCLAIMER.md"),
)

@Composable
fun LegalScreen() {
    var selectedPath by rememberSaveable { mutableStateOf(legalDocuments.first().assetPath) }
    val selected = legalDocuments.first { it.assetPath == selectedPath }
    val context = LocalContext.current
    val content = remember(selected.assetPath) {
        runCatching {
            context.assets.open(selected.assetPath).bufferedReader(Charsets.UTF_8).use { it.readText() }
        }.getOrElse {
            "法律文本无法读取。请重新安装来自官方发布页的 MealCircuit 安装包。"
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        SectionTitle(
            "法律、隐私与安全",
            "以下文本随应用离线提供；不需要联网，也不会发送任何私人数据。",
        )
        legalDocuments.forEach { document ->
            FilterChip(
                selected = selectedPath == document.assetPath,
                onClick = { selectedPath = document.assetPath },
                label = { Text(document.label) },
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Text(selected.label, style = MaterialTheme.typography.titleMedium)
        SelectionContainer {
            Text(content, fontFamily = FontFamily.Monospace)
        }
    }
}

@Composable
fun LegalSettingsEntry() {
    var showDocuments by rememberSaveable { mutableStateOf(false) }
    val context = LocalContext.current
    val allDocuments = remember(context) {
        legalDocuments.joinToString("\n\n") { document ->
            val content = runCatching {
                context.assets.open(document.assetPath).bufferedReader(Charsets.UTF_8).use { it.readText() }
            }.getOrElse {
                "法律文本无法读取。请重新安装来自官方发布页的 MealCircuit 安装包。"
            }
            "===== ${document.label} =====\n\n$content"
        }
    }

    SectionTitle(
        "法律、隐私与安全",
        "查看随安装包离线提供的项目许可、第三方声明、隐私说明、安全说明和健康免责声明。",
    )
    OutlinedButton(
        onClick = { showDocuments = true },
        modifier = Modifier.fillMaxWidth(),
    ) { Text("打开法律与隐私文本") }
    if (showDocuments) {
        AlertDialog(
            onDismissRequest = { showDocuments = false },
            title = { Text("法律、隐私与安全") },
            text = {
                SelectionContainer {
                    Text(
                        allDocuments,
                        modifier = Modifier.fillMaxWidth().heightIn(max = 560.dp)
                            .verticalScroll(rememberScrollState()),
                        fontFamily = FontFamily.Monospace,
                    )
                }
            },
            confirmButton = {
                OutlinedButton(onClick = { showDocuments = false }) { Text("关闭") }
            },
        )
    }
}
