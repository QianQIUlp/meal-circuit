package org.mealcircuit.app.ui

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.mealcircuit.app.data.MaterializedRecordEntity

/**
 * A user-facing projection of the plan that Python has already reviewed and
 * published. Android deliberately does not turn this into a local generation
 * path: it is an execution surface for the shared, signed-off result.
 */
internal data class PublishedPlan(
    val reviewDate: String,
    val planDate: String,
    val summary: String,
    val coreAdvice: List<String>,
    val rationale: List<String>,
    val evidence: List<String>,
    val problems: List<String>,
    val strategy: String?,
    val tradeoffs: List<String>,
    val nutrition: String?,
    val dayAdjustments: List<String>,
    val meals: List<PublishedMeal>,
)

internal data class PublishedMeal(
    val name: String,
    val mode: String?,
    val purpose: String?,
    val whyToday: String?,
    val wholeDayRole: String?,
    val foods: List<String>,
    val portions: List<PublishedPortion>,
    val eatOutGuidance: List<String>,
    val adjustments: List<String>,
    val executionRisks: List<String>,
)

internal data class PublishedPortion(
    val item: String,
    val amount: String,
    val increaseIf: String?,
    val decreaseIf: String?,
)

internal fun publishedPlans(records: List<MaterializedRecordEntity>): List<PublishedPlan> =
    records.mapNotNull(::publishedPlan).sortedWith(
        compareByDescending<PublishedPlan> { it.planDate }.thenByDescending { it.reviewDate }
    )

internal fun publishedPlan(record: MaterializedRecordEntity): PublishedPlan? {
    if (record.deleted) return null
    val payload = runCatching { Json.parseToJsonElement(record.payloadJson).jsonObject }.getOrNull() ?: return null
    val review = payload["review"] as? JsonObject ?: return null
    if (review.string("status") != "completed") return null
    val result = review["result_json"] as? JsonObject ?: return null
    val reviewDate = review.string("review_date") ?: return null
    val menu = result["tomorrow_menu"] as? JsonObject
    val planDate = menu?.string("date") ?: reviewDate
    return PublishedPlan(
        reviewDate = reviewDate,
        planDate = planDate,
        summary = result.string("case_summary") ?: result.string("one_line_review") ?: "已发布安排",
        coreAdvice = result.strings("core_advice"),
        rationale = result.strings("planning_rationale"),
        evidence = result.strings("evidence_summary"),
        problems = result.strings("problems_to_solve"),
        strategy = result.string("selected_strategy"),
        tradeoffs = result.strings("strategy_tradeoffs"),
        nutrition = nutritionSummary(result["day_nutrition"] as? JsonObject),
        dayAdjustments = result.strings("adjustment_conditions") + listOfNotNull(
            menu?.string("training_adjustment"),
            menu?.string("gut_adjustment"),
        ),
        meals = menu?.array("meals")?.mapNotNull(::publishedMeal).orEmpty(),
    )
}

internal fun mealModeLabel(mode: String?): String? = when (mode) {
    "quick_assembly" -> "快速组合"
    "home_cook" -> "在家下厨"
    "eat_out" -> "外食"
    else -> null
}

private fun publishedMeal(element: JsonElement): PublishedMeal? {
    val meal = element as? JsonObject ?: return null
    val name = meal.string("name") ?: return null
    val guidance = (meal["eat_out_guidance"] as? JsonObject)?.let(::eatOutGuidance).orEmpty()
    val logic = (meal["adjustment_logic"] as? JsonObject)?.let(::adjustmentLogic).orEmpty()
    return PublishedMeal(
        name = name,
        mode = meal.string("mode"),
        purpose = meal.string("purpose") ?: meal.string("portion_guidance"),
        whyToday = meal.string("why_today"),
        wholeDayRole = meal.string("whole_day_role"),
        foods = meal.strings("foods"),
        portions = meal.array("portion_contracts")?.mapNotNull(::publishedPortion).orEmpty(),
        eatOutGuidance = guidance,
        adjustments = logic,
        executionRisks = meal.strings("execution_risks"),
    )
}

private fun publishedPortion(element: JsonElement): PublishedPortion? {
    val value = element as? JsonObject ?: return null
    val item = value.string("item") ?: return null
    val range = value.array("gram_range")?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
    val basis = when (value.string("measurement_basis")) {
        "raw" -> "生重"
        "cooked" -> "熟重"
        "as_served" -> "上桌重量"
        else -> null
    }
    val amount = listOfNotNull(
        range?.takeIf { it.isNotEmpty() }?.joinToString("–", postfix = " g"),
        basis,
        value.string("household_measure"),
    ).joinToString(" · ").ifBlank { "份量按实际情况调整" }
    return PublishedPortion(
        item = item,
        amount = amount,
        increaseIf = value.string("increase_if"),
        decreaseIf = value.string("decrease_if"),
    )
}

private fun eatOutGuidance(value: JsonObject): List<String> = listOfNotNull(
    value.string("protein_anchor")?.let { "优先蛋白：$it" },
    value.string("staple")?.let { "主食：$it" },
    value.string("vegetables")?.let { "蔬菜：$it" },
    value.string("sauce_rule")?.let { "酱汁：$it" },
    value.string("fallback")?.let { "没有合适选择时：$it" },
)

private fun adjustmentLogic(value: JsonObject): List<String> = listOfNotNull(
    value.string("if_hungry")?.let { "仍饿时：$it" },
    value.string("if_low_appetite")?.let { "食欲低时：$it" },
    value.string("if_gut_unwell")?.let { "肠胃不适时：$it" },
)

private fun nutritionSummary(value: JsonObject?): String? {
    value ?: return null
    val protein = value.array("protein_g")?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
        ?.takeIf { it.isNotEmpty() }?.joinToString("–", postfix = " g 蛋白质")
    val energy = value.array("energy_kcal")?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
        ?.takeIf { it.isNotEmpty() }?.joinToString("–", postfix = " kcal")
    val confidence = when (value.string("confidence")) {
        "high" -> "高把握"
        "medium" -> "中等把握"
        "low" -> "低把握"
        else -> null
    }
    return listOfNotNull(protein, energy, confidence).joinToString(" · ").ifBlank { null }
}

private fun JsonObject.string(key: String): String? =
    this[key]?.jsonPrimitive?.contentOrNull?.trim()?.takeIf { it.isNotEmpty() }

private fun JsonObject.strings(key: String): List<String> =
    array(key)?.mapNotNull {
        (it as? JsonPrimitive)?.contentOrNull?.trim()?.takeIf(String::isNotEmpty)
    }.orEmpty()

private fun JsonObject.array(key: String): JsonArray? = this[key] as? JsonArray
