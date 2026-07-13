package org.mealcircuit.app.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.CameraAlt
import androidx.compose.material.icons.outlined.PhotoLibrary
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.domain.EntityKind
import java.io.File
import java.util.UUID
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

internal const val CAMERA_FAILURE_MESSAGE = "拍照未完成，未创建任务。"

internal fun finalizeCameraResult(success: Boolean, hasUri: Boolean, temporary: File?): String? {
    if (success && hasUri) return null
    temporary?.delete()
    return CAMERA_FAILURE_MESSAGE
}

@Composable
fun CaptureScreen(viewModel: MainViewModel) {
    val context = LocalContext.current
    var note by remember { mutableStateOf("") }
    var materials by remember { mutableStateOf("") }
    var cameraUri by remember { mutableStateOf<Uri?>(null) }
    var cameraFile by remember { mutableStateOf<File?>(null) }
    var cameraError by remember { mutableStateOf<String?>(null) }
    var selectedInputId by remember { mutableStateOf<String?>(null) }
    var selectedTaskId by remember { mutableStateOf<String?>(null) }
    var correction by remember { mutableStateOf("") }
    val inputs by viewModel.repository.observe(EntityKind.TASK_INPUT).collectAsState(emptyList())
    val tasks by viewModel.repository.observe(EntityKind.TASK).collectAsState(emptyList())
    val camera = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { success ->
        val uri = cameraUri
        val temporary = cameraFile
        cameraUri = null
        cameraFile = null
        val error = finalizeCameraResult(success, uri != null, temporary)
        if (error == null && uri != null) {
            cameraError = null
            viewModel.addPhotoTask(uri, note) { temporary?.delete() }
        } else {
            cameraError = error
        }
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        uri?.let { viewModel.addPhotoTask(it, note) }
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        SectionTitle("照片任务", "使用系统相机或 Photo Picker；原图进入应用私有目录。")
        OutlinedTextField(
            note, { note = it }, Modifier.fillMaxWidth(),
            label = { Text("补充说明（可选）") }, minLines = 2,
        )
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(
                onClick = {
                    val file = context.cacheDir.resolve("camera/${UUID.randomUUID()}.jpg")
                    file.parentFile?.mkdirs()
                    val target = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
                    cameraError = null
                    cameraFile = file
                    cameraUri = target
                    camera.launch(target)
                },
                modifier = Modifier.weight(1f),
            ) { Icon(Icons.Outlined.CameraAlt, null); Text("拍照") }
            OutlinedButton(
                onClick = {
                    picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
                },
                modifier = Modifier.weight(1f),
            ) { Icon(Icons.Outlined.PhotoLibrary, null); Text("选择照片") }
        }
        cameraError?.let { Text(it, color = androidx.compose.material3.MaterialTheme.colorScheme.error) }
        SectionTitle("原材料任务", "适合记录冰箱现有食材和粗略数量。")
        OutlinedTextField(
            materials, { materials = it }, Modifier.fillMaxWidth(),
            label = { Text("例如：鸡蛋 4 个、番茄 2 个、米饭一碗") }, minLines = 4,
        )
        Button(
            onClick = {
                selectedInputId?.let { viewModel.updateTaskInput(it, materials) }
                    ?: viewModel.addMaterialTask(materials)
                materials = ""; selectedInputId = null
            },
            enabled = materials.isNotBlank(),
        ) { Text(if (selectedInputId == null) "创建任务" else "保存输入修订") }
        SectionTitle("本机任务输入")
        OutlinedButton(
            onClick = viewModel::generateLatestTask,
            enabled = inputs.isNotEmpty(),
            modifier = Modifier.fillMaxWidth(),
        ) { Text("使用本设备配置的 AI 处理最新任务") }
        RecordList(inputs.take(20), "还没有任务", "拍照或填写原材料后会立即保存在本机。") { record ->
            val payload = Json.parseToJsonElement(record.payloadJson).jsonObject
            selectedInputId = record.entityId
            materials = payload["original_input"]?.jsonPrimitive?.content.orEmpty()
        }
        SectionTitle("结果校正", "完成后的结果不可覆盖；校正作为独立不可变记录追加。")
        RecordList(tasks.take(20), "还没有任务主体", "完成任务后可选择并追加校正。") { record ->
            selectedTaskId = record.entityId
        }
        OutlinedTextField(
            correction, { correction = it }, Modifier.fillMaxWidth(),
            label = { Text("对选中已完成任务的校正") }, minLines = 2,
        )
        Button(
            onClick = { selectedTaskId?.let { viewModel.addTaskCorrection(it, correction) }; correction = "" },
            enabled = selectedTaskId != null && correction.isNotBlank(),
        ) { Text("追加校正") }
    }
}
