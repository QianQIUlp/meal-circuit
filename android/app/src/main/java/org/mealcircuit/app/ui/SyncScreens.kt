package org.mealcircuit.app.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
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
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.serialization.json.Json
import org.mealcircuit.app.MainViewModel

@Composable
fun SyncSettingsScreen(viewModel: MainViewModel) {
    val configuration by viewModel.repository.observeSyncConfiguration().collectAsState(null)
    val pendingCount by viewModel.repository.observePendingCount().collectAsState(0)
    val pendingRegistration by viewModel.pendingRegistration.collectAsState()
    val pairingQr by viewModel.pairingQr.collectAsState()
    val devices by viewModel.devices.collectAsState()
    val rotationRecovery by viewModel.pendingRotationRecovery.collectAsState()
    var rotationConfirmation by remember { mutableStateOf("") }
    var accountDeletePassword by remember { mutableStateOf("") }
    var accountDeleteConfirmation by remember { mutableStateOf("") }
    if (pendingRegistration != null) {
        RecoveryConfirmation(viewModel)
        return
    }
    if (configuration?.enabled == true) {
        LaunchedEffect(configuration?.accountId) { viewModel.refreshDevices() }
        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 720.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            SectionTitle("端到端加密同步已启用", "Room 仍是全部 UI 的唯一数据源。")
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)) {
                Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(configuration?.serverUrl.orEmpty())
                    Text("待上传 $pendingCount 项 · 游标 ${configuration?.cursor ?: 0}")
                    Text("照片策略：${configuration?.mediaPolicy}")
                }
            }
            Button(onClick = viewModel::syncNow, Modifier.fillMaxWidth()) { Text("立即同步") }
            Text("照片下载策略")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("all" to "全部", "all_wifi" to "仅 Wi-Fi", "on_demand" to "按需").forEach { (value, label) ->
                    FilterChip(
                        selected = configuration?.mediaPolicy == value,
                        onClick = { viewModel.setMediaPolicy(value) },
                        label = { Text(label) },
                    )
                }
            }
            if (configuration?.mediaPolicy == "on_demand") {
                OutlinedButton(onClick = viewModel::syncOnDemandMediaNow, Modifier.fillMaxWidth()) {
                    Text("本次下载全部缺失照片")
                }
            }
            OutlinedButton(onClick = viewModel::createPairingQr, Modifier.fillMaxWidth()) { Text("生成新设备配对二维码") }
            pairingQr?.let { value ->
                Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    PairingQrCode(value)
                    Text("二维码 10 分钟内有效；新设备仍需输入账户密码。")
                    OutlinedButton(onClick = viewModel::clearPairingQr) { Text("关闭二维码") }
                }
            }
            OutlinedButton(onClick = viewModel::unlink, Modifier.fillMaxWidth()) { Text("取消本机同步（保留数据）") }
            SectionTitle("设备", "撤销会立即使该设备的服务端令牌失效。")
            devices.forEach { device ->
                Card(Modifier.fillMaxWidth()) {
                    Row(
                        Modifier.fillMaxWidth().padding(16.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                    ) {
                        Column {
                            Text(device.name)
                            Text(if (device.current) "当前设备" else if (device.revoked) "已撤销" else "已授权")
                        }
                        if (!device.current && !device.revoked) {
                            OutlinedButton(onClick = { viewModel.revokeDevice(device.id) }) { Text("撤销") }
                        }
                    }
                }
            }
            SectionTitle("安全轮换", "重新加密全部远端实体和照片、生成新恢复密钥，并撤销其他设备。")
            if (rotationRecovery == null) {
                OutlinedButton(onClick = viewModel::prepareKeyRotation, Modifier.fillMaxWidth()) {
                    Text("开始安全轮换")
                }
            } else {
                Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
                    Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        Text("新的恢复密钥只显示到确认完成")
                        Text(rotationRecovery.orEmpty(), fontFamily = FontFamily.Monospace)
                        SecretField("重新输入新恢复密钥", rotationConfirmation) { rotationConfirmation = it }
                        Button(
                            onClick = { viewModel.confirmKeyRotation(rotationConfirmation) },
                            enabled = rotationConfirmation.trim().uppercase() == rotationRecovery.orEmpty(),
                            modifier = Modifier.fillMaxWidth(),
                        ) { Text("确认保存并撤销其他设备") }
                        OutlinedButton(onClick = viewModel::abortKeyRotation, Modifier.fillMaxWidth()) {
                            Text("中止轮换")
                        }
                    }
                }
            }
            SectionTitle("删除远端账户", "永久删除服务端账户、密文和照片；本机数据保留并转为仅本地模式。")
            SecretField("账户密码", accountDeletePassword) { accountDeletePassword = it }
            OutlinedTextField(
                accountDeleteConfirmation,
                { accountDeleteConfirmation = it },
                Modifier.fillMaxWidth(),
                label = { Text("输入 DELETE 确认") },
                singleLine = true,
            )
            OutlinedButton(
                onClick = {
                    viewModel.deleteSyncAccount(accountDeletePassword)
                    accountDeletePassword = ""; accountDeleteConfirmation = ""
                },
                enabled = accountDeletePassword.isNotBlank() && accountDeleteConfirmation == "DELETE",
                modifier = Modifier.fillMaxWidth(),
            ) { Text("永久删除远端同步账户") }
            Text(
                "停掉同步服务后，本设备仍可继续记录、查看、分析和导出。",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        return
    }
    SyncOnboarding(viewModel)
}

@Composable
private fun SyncOnboarding(viewModel: MainViewModel) {
    var register by remember { mutableStateOf(false) }
    var pairing by remember { mutableStateOf(false) }
    var url by remember { mutableStateOf("") }
    var login by remember { mutableStateOf("") }
    var device by remember { mutableStateOf(android.os.Build.MODEL) }
    var password by remember { mutableStateOf("") }
    var confirmation by remember { mutableStateOf("") }
    var recovery by remember { mutableStateOf("") }
    var pairingPayload by remember { mutableStateOf("") }
    val scanner = rememberLauncherForActivityResult(ScanContract()) { result ->
        result.contents?.let { pairingPayload = it }
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 720.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        SectionTitle("可选自托管同步", "不登录也能永久离线使用；自定义 URL 在正式版中必须是 HTTPS。")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(!register && !pairing, { register = false; pairing = false }, label = { Text("登录") })
            FilterChip(register, { register = true; pairing = false }, label = { Text("注册") })
            FilterChip(pairing, { register = false; pairing = true }, label = { Text("扫码加入") })
        }
        if (!pairing) {
            OutlinedTextField(url, { url = it }, Modifier.fillMaxWidth(), label = { Text("同步服务 URL") }, singleLine = true)
        }
        OutlinedTextField(login, { login = it }, Modifier.fillMaxWidth(), label = { Text("登录名") }, singleLine = true)
        OutlinedTextField(device, { device = it }, Modifier.fillMaxWidth(), label = { Text("设备名称") }, singleLine = true)
        SecretField("账户密码", password) { password = it }
        if (register) SecretField("再次输入密码", confirmation) { confirmation = it }
        else if (!pairing) SecretField("恢复密钥", recovery) { recovery = it }
        if (pairing) {
            OutlinedButton(
                onClick = {
                    scanner.launch(ScanOptions().setPrompt("扫描已登录设备显示的 MealCircuit 配对二维码").setBeepEnabled(false))
                },
                modifier = Modifier.fillMaxWidth(),
            ) { Text(if (pairingPayload.isBlank()) "扫描配对二维码" else "已扫描配对二维码") }
        }
        Button(
            onClick = {
                if (register) viewModel.beginRegistration(url, login, password, device)
                else if (pairing) viewModel.claimPairing(pairingPayload, login, password, device)
                else viewModel.login(url, login, password, device, recovery)
            },
            enabled = login.isNotBlank() && device.isNotBlank() && password.length >= 12 && when {
                register -> url.isNotBlank() && password == confirmation
                pairing -> pairingPayload.isNotBlank()
                else -> url.isNotBlank() && recovery.isNotBlank()
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (register) "创建账户" else if (pairing) "加入并恢复数据" else "登录并解锁") }
    }
}

@Composable
private fun RecoveryConfirmation(viewModel: MainViewModel) {
    val pending by viewModel.pendingRegistration.collectAsState()
    var value by remember { mutableStateOf("") }
    val recovery = pending?.material?.recoveryKey.orEmpty()
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 720.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SectionTitle("保存恢复密钥", "它只显示这一次；服务端无法替你恢复。")
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
            Text(recovery, Modifier.fillMaxWidth().padding(16.dp), fontFamily = FontFamily.Monospace)
        }
        Text("丢失全部设备且没有恢复密钥时，加密数据永久不可恢复。")
        SecretField("完整重新输入恢复密钥", value) { value = it }
        Button(
            onClick = { viewModel.confirmRegistration(value) },
            enabled = value.trim().uppercase() == recovery,
            modifier = Modifier.fillMaxWidth(),
        ) { Text("我已保存，启用同步") }
    }
}

@Composable
private fun SecretField(label: String, value: String, onChange: (String) -> Unit) {
    OutlinedTextField(
        value, onChange, Modifier.fillMaxWidth(), label = { Text(label) },
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        singleLine = true,
    )
}

@Composable
fun ConflictScreen(viewModel: MainViewModel) {
    val conflicts by viewModel.repository.observeConflicts().collectAsState(emptyList())
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        SectionTitle("冲突中心", "不会按时间覆盖同字段并发值；两个 sibling revision 都会保留。")
        if (conflicts.isEmpty()) {
            EmptyState("没有待解决冲突", "离线编辑不同实体或不同字段会自动合并。")
        }
        conflicts.forEach { conflict ->
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
                Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("${conflict.entityKind} · ${conflict.entityId}")
                    Text("冲突路径：${conflict.conflictingPathsJson}")
                    Text("本机 sibling")
                    Text(pretty(conflict.localRevisionJson), maxLines = 8, fontFamily = FontFamily.Monospace)
                    Text("远端 sibling")
                    Text(pretty(conflict.remoteRevisionJson), maxLines = 8, fontFamily = FontFamily.Monospace)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = { viewModel.resolveConflict(conflict.id, true) }) { Text("保留本机") }
                        OutlinedButton(onClick = { viewModel.resolveConflict(conflict.id, false) }) { Text("保留远端") }
                    }
                }
            }
        }
    }
}

private fun pretty(value: String) = runCatching {
    Json { prettyPrint = true }.encodeToString(
        kotlinx.serialization.json.JsonElement.serializer(),
        Json.parseToJsonElement(value),
    )
}.getOrDefault(value)
