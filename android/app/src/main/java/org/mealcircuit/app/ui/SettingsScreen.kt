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
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
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
import androidx.compose.runtime.setValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.ai.AiProvider
import org.mealcircuit.app.domain.EntityKind
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(viewModel: MainViewModel) {
    var provider by remember { mutableStateOf(AiProvider.OPENAI) }
    var expanded by remember { mutableStateOf(false) }
    var key by remember { mutableStateOf("") }
    var model by remember { mutableStateOf("") }
    var importUri by remember { mutableStateOf<Uri?>(null) }
    var importRecovery by remember { mutableStateOf("") }
    var merge by remember { mutableStateOf(false) }
    val exportRecovery by viewModel.exportRecoveryKey.collectAsState()
    val importPreview by viewModel.portableImport.collectAsState()
    val savedTimezone by viewModel.timezone.collectAsState()
    var timezone by remember(savedTimezone) { mutableStateOf(savedTimezone) }
    var profile by remember { mutableStateOf("") }
    var doctrine by remember { mutableStateOf("") }
    var settingsJson by remember {
        mutableStateOf(
            """{
  "schema_version": 1,
  "timezone": "${java.time.ZoneId.systemDefault().id}",
  "meal_environment": "用户自行配置",
  "protein_target_g": [50, 65],
  "portion_method": "按实际饥饿和正餐结构",
  "missing_training_default": "保持未知，不推断为未训练",
  "compensation_boundary": "不跳餐、不清零主食、不极端压低热量；只撤掉重复加餐并恢复标准份量。",
  "home_cooking": {"enabled": false}
}"""
        )
    }
    val domainPreferences by viewModel.repository.observe(EntityKind.PREFERENCES).collectAsState(emptyList())
    val enabledModules by viewModel.checkinModules.collectAsState()
    var selectedModules by remember(enabledModules) { mutableStateOf(enabledModules) }
    LaunchedEffect(domainPreferences) {
        domainPreferences.forEach { record ->
            val payload = runCatching { Json.parseToJsonElement(record.payloadJson).jsonObject }.getOrNull() ?: return@forEach
            when (payload["kind"]?.jsonPrimitive?.content) {
                "profile" -> if (profile.isBlank()) profile = payload["content"]?.jsonPrimitive?.content.orEmpty()
                "doctrine" -> if (doctrine.isBlank()) doctrine = payload["content"]?.jsonPrimitive?.content.orEmpty()
                "settings" -> settingsJson = payload["content"]?.jsonPrimitive?.content.orEmpty()
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
        OutlinedTextField(profile, { profile = it }, Modifier.fillMaxWidth(), label = { Text("profile.md") }, minLines = 4)
        Button(onClick = { viewModel.savePreference("profile", profile) }, enabled = profile.isNotBlank()) { Text("保存档案") }
        OutlinedTextField(doctrine, { doctrine = it }, Modifier.fillMaxWidth(), label = { Text("doctrine.private.md") }, minLines = 5)
        Button(onClick = { viewModel.savePreference("doctrine", doctrine) }, enabled = doctrine.isNotBlank()) { Text("保存私人总纲") }
        SectionTitle("完整私人设置", "JSON 会作为 Domain v1 配置 revision 保存；时区、用餐环境、蛋白目标和居家烹饪都由用户自行配置。")
        OutlinedTextField(
            settingsJson,
            { settingsJson = it },
            Modifier.fillMaxWidth(),
            label = { Text("settings.json") },
            minLines = 8,
            supportingText = { Text("需包含模板中的全部字段；开启 home_cooking 时还需完整烹饪配置") },
        )
        Button(onClick = { viewModel.saveSettings(settingsJson) }, enabled = settingsJson.isNotBlank()) { Text("校验并保存设置") }
        SectionTitle("每日状态模块", "关闭的模块不会出现在 Android 问卷中；设置会作为版本化配置同步。")
        listOf("weight" to "体重", "training" to "训练", "hunger" to "饥饿与饱腹", "sleep" to "睡眠", "gut" to "肠胃").forEach { (keyName, label) ->
            FilterChip(
                selected = keyName in selectedModules,
                onClick = { selectedModules = if (keyName in selectedModules) selectedModules - keyName else selectedModules + keyName },
                label = { Text(label) },
            )
        }
        Button(onClick = { viewModel.saveCheckinModules(selectedModules) }) { Text("保存模块设置") }
        SectionTitle("设备内 AI 接入", "API Key 仅由 Android Keystore 包装，不进入 Room、同步或导出。")
        ExposedDropdownMenuBox(expanded, { expanded = !expanded }) {
            OutlinedTextField(
                provider.name, {}, readOnly = true,
                label = { Text("供应商") },
                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded) },
                modifier = Modifier.menuAnchor().fillMaxWidth(),
            )
            ExposedDropdownMenu(expanded, { expanded = false }) {
                AiProvider.entries.forEach { item ->
                    DropdownMenuItem(
                        text = { Text(item.name) },
                        onClick = { provider = item; expanded = false },
                    )
                }
            }
        }
        OutlinedTextField(
            model, { model = it }, Modifier.fillMaxWidth(),
            label = { Text("模型名") }, singleLine = true,
        )
        OutlinedTextField(
            key, { key = it }, Modifier.fillMaxWidth(),
            label = { Text("API Key") },
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            singleLine = true,
        )
        Button(
            onClick = { viewModel.saveAiKey(provider, model, key); key = "" },
            enabled = key.isNotBlank() && model.isNotBlank(),
        ) { Text("安全保存到本设备") }
        SectionTitle("Portable Data", "加密 .mcx 的导入导出入口将在系统文件选择器中操作，不授予整盘权限。")
        Text("数据包不包含 API Key、设备密钥、同步令牌或恢复密钥。", modifier = Modifier.fillMaxWidth())
        Button(
            onClick = { exporter.launch("mealcircuit-${java.time.LocalDate.now()}.mcx") },
            modifier = Modifier.fillMaxWidth(),
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
