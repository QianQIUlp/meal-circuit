package org.mealcircuit.app.domain

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.time.LocalDate
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int

object ResultValidator {
    fun task(kind: String, value: JsonObject): JsonObject {
        text(value, "summary")
        when (kind) {
            "photo" -> {
                val candidates = array(value, "candidates").also { require(it.isNotEmpty()) }
                candidates.forEach { element ->
                    val item = element.jsonObject
                    text(item, "name"); text(item, "portion_range"); nutrition(item.getValue("nutrition").jsonObject)
                    require(item.getValue("confidence").jsonPrimitive.double in 0.0..1.0)
                }
                textArray(value, "unknowns"); textArray(value, "advice")
            }
            "material" -> {
                textArray(value, "combinations")
                nutrition(value.getValue("batch_nutrition").jsonObject)
                nutrition(value.getValue("per_serving_nutrition").jsonObject)
                textArray(value, "gaps"); textArray(value, "risks"); textArray(value, "minimal_adjustments")
            }
            else -> error("Unknown task kind")
        }
        return value
    }

    fun daily(
        value: JsonObject,
        tomorrow: LocalDate,
        expectedPriorityFoodIds: Set<String> = emptySet(),
        expectedEnvironment: String? = null,
        expectedProteinTarget: JsonArray? = null,
        expectedCarryoverIds: Set<String> = emptySet(),
        homeCooking: JsonObject? = null,
        previousRotation: JsonObject? = null,
    ): JsonObject {
        require(text(value, "system_status") in setOf("stable", "observe", "adjust", "risk"))
        require(textArray(value, "facts").isNotEmpty())
        textArray(value, "inferences")
        require(textArray(value, "core_advice").size in 1..3)
        textArray(value, "do_not_adjust"); textArray(value, "risk_signals")
        text(value, "one_line_review")
        val seenFoodIds = mutableSetOf<String>()
        array(value, "priority_food_decisions").forEach { element ->
            val decision = element.jsonObject
            require(seenFoodIds.add(text(decision, "food_id"))) { "priority_food_decisions 包含重复食品" }
            require(text(decision, "decision") in setOf("use", "skip"))
            text(decision, "reason")
        }
        require(seenFoodIds == expectedPriorityFoodIds) { "priority_food_decisions 必须覆盖全部高优先级食品" }
        val seenCarryovers = mutableSetOf<String>()
        val carryovers = value["ingredient_carryover_decisions"]
        if (expectedCarryoverIds.isNotEmpty() || carryovers != null) {
            carryovers!!.jsonArray.forEach { element ->
                val decision = element.jsonObject
                require(seenCarryovers.add(text(decision, "carryover_id"))) { "承接食材裁决重复" }
                text(decision, "ingredient")
                require(text(decision, "decision") in setOf("use", "skip", "discard"))
                text(decision, "reason"); text(decision, "planned_use")
            }
            require(seenCarryovers == expectedCarryoverIds) { "承接食材裁决不完整" }
        }
        val menu = value.getValue("tomorrow_menu").jsonObject
        require(LocalDate.parse(text(menu, "date")) == tomorrow)
        val environment = text(menu, "environment")
        if (expectedEnvironment != null) require(environment == expectedEnvironment)
        range(menu["protein_target_g"])
        if (expectedProteinTarget != null) require(menu["protein_target_g"] == expectedProteinTarget)
        val meals = array(menu, "meals").also { require(it.size == 3) }
        val names = meals.map { meal ->
            val item = meal.jsonObject
            text(item, "name").also {
                require(textArray(item, "foods").isNotEmpty()); text(item, "portion_guidance")
                range(item["protein_g"]); textArray(item, "substitutions")
            }
        }.toSet()
        require(names == setOf("早餐", "午餐", "晚餐"))
        if (homeCooking?.get("enabled")?.jsonPrimitive?.content == "true") {
            validateHomeCooking(menu, meals.map { it.jsonObject }, homeCooking, tomorrow)
            previousRotation?.let { previous ->
                val current = menu.getValue("rotation").jsonObject
                val repeated = current["dish_key"] == previous["dish_key"] ||
                    current["flavor_profile"] == previous["flavor_profile"]
                if (repeated) {
                    require(current["repeat_reason"]?.jsonPrimitive?.content in setOf(
                        "health_recovery", "ingredient_expiry", "shopping_constraint"
                    )) { "连续晚餐重复必须提供允许的 repeat_reason" }
                }
            }
        }
        val snack = menu.getValue("conditional_snack").jsonObject
        text(snack, "condition"); require(textArray(snack, "options").isNotEmpty())
        text(menu, "training_adjustment"); text(menu, "gut_adjustment")
        return value
    }

    private fun nutrition(value: JsonObject) {
        listOf("energy_kcal", "protein_g", "carbs_g", "fat_g").forEach { range(value[it]) }
    }

    private fun range(value: kotlinx.serialization.json.JsonElement?) {
        if (value == null || value is JsonNull) return
        val items = value.jsonArray
        require(items.size == 2)
        val low = items[0].jsonPrimitive.double
        val high = items[1].jsonPrimitive.double
        require(low >= 0 && high >= low)
    }

    private fun text(value: JsonObject, key: String): String =
        value.getValue(key).jsonPrimitive.content.trim().also { require(it.isNotEmpty()) }

    private fun array(value: JsonObject, key: String): JsonArray = value.getValue(key).jsonArray

    private fun textArray(value: JsonObject, key: String): JsonArray = array(value, key).also { items ->
        items.forEach { require(it.jsonPrimitive.content.trim().isNotEmpty()) }
    }

    private fun validateHomeCooking(
        menu: JsonObject,
        meals: List<JsonObject>,
        settings: JsonObject,
        tomorrow: LocalDate,
    ) {
        val byName = meals.associateBy { text(it, "name") }
        require(text(byName.getValue("早餐"), "mode") == "quick_assembly")
        require(text(byName.getValue("午餐"), "mode") == "eat_out")
        require(text(byName.getValue("晚餐"), "mode") == "home_cook")
        val recipe = byName.getValue("晚餐").getValue("recipe_card").jsonObject
        text(recipe, "title")
        require(recipe.getValue("servings").jsonPrimitive.int == settings.getValue("servings").jsonPrimitive.int)
        val active = recipe.getValue("active_minutes").jsonPrimitive.int
        val total = recipe.getValue("total_minutes").jsonPrimitive.int
        val limit = settings.getValue("weekday_time_limit_minutes").jsonPrimitive.int
        require(active > 0 && total >= active && total <= limit)
        val equipment = settings.getValue("equipment").jsonArray.map { it.jsonPrimitive.content }.toSet()
        val cookware = textArray(recipe, "cookware").map { it.jsonPrimitive.content }
        require(cookware.size in 1..2 && cookware.all { it in equipment })
        structuredArray(recipe, "ingredients", setOf("name", "amount", "prep"))
        structuredArray(recipe, "seasonings", setOf("name", "amount", "timing"))
        val steps = array(recipe, "steps").also { require(it.isNotEmpty()) }
        steps.forEach { item ->
            val step = item.jsonObject
            listOf("instruction", "heat", "done_signal").forEach { text(step, it) }
            require(step.getValue("minutes").jsonPrimitive.double > 0)
        }
        require(textArray(recipe, "failure_rescue").isNotEmpty())
        text(recipe, "cleanup"); text(recipe, "gut_fallback")
        structuredArray(menu, "shopping_list", setOf("name", "amount", "purpose", "selection_guide", "storage"))
        array(menu, "shopping_list").forEach { it.jsonObject.getValue("required").jsonPrimitive.boolean }
        val online = array(menu, "online_options").also { require(it.size <= 3) }
        online.forEach { item ->
            val option = item.jsonObject
            listOf("category", "package_size", "skip_if").forEach { text(option, it) }
            listOf("selection_criteria", "search_keywords", "pairs_with").forEach {
                require(textArray(option, it).isNotEmpty())
            }
        }
        val reuse = menu.getValue("reuse_plan").jsonObject
        val horizon = settings.getValue("rotation_window_days").jsonPrimitive.int
        require(reuse.getValue("horizon_days").jsonPrimitive.int == horizon)
        val reuseItems = array(reuse, "items").also { require(it.isNotEmpty()) }
        reuseItems.forEach { item ->
            val value = item.jsonObject
            listOf("ingredient", "tomorrow_use", "storage").forEach { text(value, it) }
            val later = array(value, "later_uses").also { require(it.isNotEmpty()) }
            later.forEach { use ->
                val detail = use.jsonObject
                val day = LocalDate.parse(text(detail, "date"))
                require(day > tomorrow && day <= tomorrow.plusDays((horizon - 1).toLong()))
                text(detail, "use")
            }
        }
        val rotation = menu.getValue("rotation").jsonObject
        listOf("dish_key", "primary_protein", "primary_vegetable", "flavor_profile", "technique").forEach {
            text(rotation, it)
        }
        rotation["repeat_reason"]?.jsonPrimitive?.content?.let {
            require(it in setOf("health_recovery", "ingredient_expiry", "shopping_constraint"))
        }
    }

    private fun structuredArray(value: JsonObject, key: String, fields: Set<String>) {
        val items = array(value, key).also { require(it.isNotEmpty()) }
        items.forEach { element -> fields.forEach { text(element.jsonObject, it) } }
    }
}
