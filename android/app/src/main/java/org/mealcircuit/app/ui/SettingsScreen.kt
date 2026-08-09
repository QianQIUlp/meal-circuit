package org.mealcircuit.app.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.domain.EntityKind
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

@Composable
fun SettingsScreen(viewModel: MainViewModel) {
    var showLegacyAiCleanupDialog by rememberSaveable { mutableStateOf(false) }
    var importUri by rememberSaveable { mutableStateOf<Uri?>(null) }
    var importRecovery by remember { mutableStateOf("") }
    var merge by rememberSaveable { mutableStateOf(false) }
    val exportRecovery by viewModel.exportRecoveryKey.collectAsState()
    val importPreview by viewModel.portableImport.collectAsState()
    val savedTimezone by viewModel.timezone.collectAsState()
    var timezone by rememberSaveable(savedTimezone) { mutableStateOf(savedTimezone) }
    // Large editable documents live in the ViewModel, not SavedState, so tab changes preserve
    // drafts without risking TransactionTooLargeException during process state saving.
    val editor = viewModel.settingsEditor
    val domainPreferences by viewModel.repository.observe(EntityKind.PREFERENCES).collectAsState(emptyList())
    val enabledModules by viewModel.checkinModules.collectAsState()
    var selectedModules by rememberSaveable(enabledModules) { mutableStateOf(enabledModules) }
    LaunchedEffect(domainPreferences) {
        domainPreferences.forEach { record ->
            val payload = runCatching { Json.parseToJsonElement(record.payloadJson).jsonObject }.getOrNull() ?: return@forEach
            val content = payload["content"]?.jsonPrimitive?.content.orEmpty()
            when (payload["kind"]?.jsonPrimitive?.content) {
                "profile" -> if (record.updatedAt != editor.profileSource &&
                    (!editor.profileDirty || editor.profile == content)) {
                    editor.profile = content; editor.profileDirty = false; editor.profileSource = record.updatedAt
                }
                "doctrine" -> if (record.updatedAt != editor.doctrineSource &&
                    (!editor.doctrineDirty || editor.doctrine == content)) {
                    editor.doctrine = content; editor.doctrineDirty = false; editor.doctrineSource = record.updatedAt
                }
                "settings" -> if (record.updatedAt != editor.settingsSource &&
                    (!editor.settingsDirty || editor.settingsJson == content)) {
                    editor.settingsJson = content; editor.settingsDirty = false; editor.settingsSource = record.updatedAt
                }
            }
        }
    }
    val exporter = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/octet-stream")
    ) { uri -> uri?.let(viewModel::exportPortable) }
    val importer = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        importUri = uri
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 720.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SectionTitle("日期与时区", "“今天”按此 IANA 时区解释；饮食记录日期不会在设备间重新换算。")
        OutlinedTextField(
            timezone, { timezone = it }, Modifier.fillMaxWidth(),
            label = { Text("IANA 时区，例如 Asia/Shanghai") }, singleLine = true,
        )
        Button(onClick = { viewModel.saveTimezone(timezone) }, enabled = timezone.isNotBlank()) { Text("保存时区") }
        SectionTitle("档案与私人总纲", "Markdown 文本会作为版本化配置实体同步；私人总纲仍是饮食判断最高规则。")
        OutlinedTextField(
            editor.profile,
            { editor.profile = it; editor.profileDirty = true },
            Modifier.fillMaxWidth(),
            label = { Text("profile.md") },
            minLines = 4,
        )
        Button(
            onClick = {
                viewModel.savePreference("profile", editor.profile) { editor.profileDirty = false }
            },
            enabled = editor.profile.isNotBlank(),
        ) { Text("保存档案") }
        OutlinedTextField(
            editor.doctrine,
            { editor.doctrine = it; editor.doctrineDirty = true },
            Modifier.fillMaxWidth(),
            label = { Text("doctrine.private.md") },
            minLines = 5,
        )
        Button(
            onClick = {
                viewModel.savePreference("doctrine", editor.doctrine) { editor.doctrineDirty = false }
            },
            enabled = editor.doctrine.isNotBlank(),
        ) { Text("保存私人总纲") }
        SectionTitle("完整私人设置", "JSON 会作为 Domain v1 配置 revision 保存；时区、用餐环境、蛋白目标和居家烹饪都由用户自行配置。")
        OutlinedTextField(
            editor.settingsJson,
            { editor.settingsJson = it; editor.settingsDirty = true },
            Modifier.fillMaxWidth(),
            label = { Text("settings.json") },
            minLines = 8,
            supportingText = { Text("需包含模板中的全部字段；开启 home_cooking 时还需完整烹饪配置") },
        )
        Button(
            onClick = {
                viewModel.saveSettings(editor.settingsJson) { editor.settingsDirty = false }
            },
            enabled = editor.settingsJson.isNotBlank(),
        ) { Text("校验并保存设置") }
        SectionTitle("每日状态模块", "关闭的模块不会出现在 Android 问卷中；设置会作为版本化配置同步。")
        listOf("weight" to "体重", "training" to "训练", "hunger" to "饥饿与饱腹", "sleep" to "睡眠", "gut" to "肠胃").forEach { (keyName, label) ->
            FilterChip(
                selected = keyName in selectedModules,
                onClick = { selectedModules = if (keyName in selectedModules) selectedModules - keyName else selectedModules + keyName },
                label = { Text(label) },
            )
        }
        Button(onClick = { viewModel.saveCheckinModules(selectedModules) }) { Text("保存模块设置") }
        SectionTitle("旧版设备 AI 配置", "历史 API Key 不会被静默删除；清理只会在你明确确认后执行，且不会调用模型。")
        OutlinedButton(
            onClick = { showLegacyAiCleanupDialog = true },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("清理旧版设备 AI 配置") }
        if (showLegacyAiCleanupDialog) {
            AlertDialog(
                onDismissRequest = { showLegacyAiCleanupDialog = false },
                title = { Text("确认清理旧版设备 AI 配置？") },
                text = { Text("这会删除旧版设备中保存的 AI API Key、供应商和模型设置，且无法恢复。") },
                confirmButton = {
                    Button(
                        onClick = {
                            showLegacyAiCleanupDialog = false
                            viewModel.clearLegacyAiConfiguration()
                        },
                    ) { Text("确认清理") }
                },
                dismissButton = {
                    OutlinedButton(onClick = { showLegacyAiCleanupDialog = false }) { Text("取消") }
                },
            )
        }
        SectionTitle("Portable Data", "加密 .mcx 的导入导出入口将在系统文件选择器中操作，不授予整盘权限。")
        Text("数据包不包含 API Key、设备密钥、同步令牌或恢复密钥。", modifier = Modifier.fillMaxWidth())
        Button(
            onClick = { exporter.launch("mealcircuit-${java.time.LocalDate.now()}.mcx") },
            modifier = Modifier.fillMaxWidth(),
            enabled = exportRecovery == null,
        ) { Text("导出加密 .mcx") }
        exportRecovery?.let { recovery ->
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
                Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("恢复密钥只显示这一次")
                    Text(recovery)
                    OutlinedButton(onClick = viewModel::clearExportRecoveryKey) { Text("我已保存") }
                }
            }
        }
        OutlinedButton(
            onClick = { importer.launch(arrayOf("application/octet-stream", "application/zip", "*/*")) },
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (importUri == null) "选择数据包" else "已选择数据包") }
        OutlinedTextField(
            importRecovery, { importRecovery = it }, Modifier.fillMaxWidth(),
            label = { Text("数据包恢复密钥（明文 ZIP 留空）") },
            visualTransformation = PasswordVisualTransformation(),
        )
        FilterChip(selected = merge, onClick = { merge = !merge }, label = { Text("合并到现有数据") })
        Button(
            onClick = { importUri?.let { viewModel.previewPortable(it, importRecovery, merge) } },
            enabled = importUri != null,
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (merge) "预检合并" else "预检空目录恢复") }
        importPreview?.let { request ->
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
                Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("预检：${request.preview.entities} 个实体、${request.preview.revisions} 个 revisions、${request.preview.assets} 个资产")
                    Text("预计冲突：${request.preview.conflicts}")
                    Button(onClick = viewModel::applyPortable, modifier = Modifier.fillMaxWidth()) { Text("确认写入") }
                    OutlinedButton(onClick = viewModel::cancelPortableImport, modifier = Modifier.fillMaxWidth()) { Text("取消") }
                }
            }
        }
    }
}
