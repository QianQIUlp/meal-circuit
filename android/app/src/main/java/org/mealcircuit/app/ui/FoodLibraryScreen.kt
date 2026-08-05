package org.mealcircuit.app.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
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
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import org.mealcircuit.app.MainViewModel
import org.mealcircuit.app.domain.EntityKind
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.jsonArray

@Composable
fun FoodLibraryScreen(viewModel: MainViewModel) {
    var name by rememberSaveable { mutableStateOf("") }
    var notes by rememberSaveable { mutableStateOf("") }
    var energy by rememberSaveable { mutableStateOf("") }
    var protein by rememberSaveable { mutableStateOf("") }
    var carbs by rememberSaveable { mutableStateOf("") }
    var fat by rememberSaveable { mutableStateOf("") }
    var packagePhoto by rememberSaveable { mutableStateOf<Uri?>(null) }
    var selectedId by rememberSaveable { mutableStateOf<String?>(null) }
    val energyError = nutritionNumberError(energy)
    val proteinError = nutritionNumberError(protein)
    val carbsError = nutritionNumberError(carbs)
    val fatError = nutritionNumberError(fat)
    val nutritionValid = listOf(energyError, proteinError, carbsError, fatError).all { it == null }

    fun clearForm() {
        name = ""; notes = ""; energy = ""; protein = ""; carbs = ""; fat = ""
        packagePhoto = null; selectedId = null
    }

    val foods by viewModel.repository.observe(EntityKind.FOOD_ITEM).collectAsState(emptyList())
    val packagePicker = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        packagePhoto = uri
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 880.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SectionTitle("食品营养库", "用户确认的数据优先于模型估算；未知营养值保持为空。")
        OutlinedTextField(name, { name = it }, Modifier.fillMaxWidth(), label = { Text("食品名称") })
        OutlinedTextField(
            notes, { notes = it }, Modifier.fillMaxWidth(),
            label = { Text("品牌、包装营养或使用条件") }, minLines = 3,
        )
        androidx.compose.foundation.layout.Row(
            Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            OutlinedTextField(
                energy, { energy = it }, Modifier.weight(1f),
                label = { Text("kcal") }, singleLine = true,
                isError = energyError != null,
                supportingText = energyError?.let { error -> { Text(error) } },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            )
            OutlinedTextField(
                protein, { protein = it }, Modifier.weight(1f),
                label = { Text("蛋白质 g") }, singleLine = true,
                isError = proteinError != null,
                supportingText = proteinError?.let { error -> { Text(error) } },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            )
        }
        androidx.compose.foundation.layout.Row(
            Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            OutlinedTextField(
                carbs, { carbs = it }, Modifier.weight(1f),
                label = { Text("碳水 g") }, singleLine = true,
                isError = carbsError != null,
                supportingText = carbsError?.let { error -> { Text(error) } },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            )
            OutlinedTextField(
                fat, { fat = it }, Modifier.weight(1f),
                label = { Text("脂肪 g") }, singleLine = true,
                isError = fatError != null,
                supportingText = fatError?.let { error -> { Text(error) } },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            )
        }
        androidx.compose.material3.OutlinedButton(
            onClick = {
                packagePicker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (packagePhoto == null) "选择包装照片（可选）" else "已选择新包装照片") }
        Button(
            onClick = {
                selectedId?.let {
                    viewModel.updateFood(it, name, notes, energy, protein, carbs, fat, packagePhoto, onSuccess = ::clearForm)
                } ?: viewModel.addFood(name, notes, energy, protein, carbs, fat, packagePhoto, onSuccess = ::clearForm)
            },
            enabled = name.isNotBlank() && nutritionValid,
        ) { Text(if (selectedId == null) "保存食品" else "保存新修订") }
        selectedId?.let { id ->
            androidx.compose.material3.OutlinedButton(
                onClick = {
                    viewModel.deleteFood(id)
                    clearForm()
                },
            ) { Text("软删除此食品") }
        }
        RecordList(
            foods.filterNot { it.deleted },
            "营养库为空",
            "添加常吃食品后，桌面与 Android 都能离线读取。",
        ) { record ->
            val food = Json.parseToJsonElement(record.payloadJson).jsonObject.getValue("food").jsonObject
            selectedId = record.entityId
            name = food["name"]?.jsonPrimitive?.content.orEmpty()
            notes = food["notes"]?.jsonPrimitive?.content.orEmpty()
            energy = food["energy_kcal"]?.jsonPrimitive?.doubleOrNull?.toString().orEmpty()
            protein = food["protein_g"]?.jsonPrimitive?.doubleOrNull?.toString().orEmpty()
            carbs = food["carbs_g"]?.jsonPrimitive?.doubleOrNull?.toString().orEmpty()
            fat = food["fat_g"]?.jsonPrimitive?.doubleOrNull?.toString().orEmpty()
        }
        SectionTitle("修订历史")
        selectedId?.let { id ->
            val record = foods.firstOrNull { it.entityId == id }
            val count = record?.let { Json.parseToJsonElement(it.payloadJson).jsonObject["history"]?.jsonArray?.size } ?: 0
            Text("此食品保留 $count 条历史事件；同步冲突会保留并列历史版本。")
        }
    }
}

private fun nutritionNumberError(value: String): String? {
    if (value.isBlank()) return null
    val number = value.trim().toDoubleOrNull()
    return if (number != null && number.isFinite() && number >= 0.0) null else "请输入非负数字"
}
