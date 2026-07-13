package org.mealcircuit.app.domain

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.add
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.intOrNull
import java.time.LocalDate

object ResultSchemas {
    private fun range() = buildJsonArray { add(0); add(0) }
    private fun nutrition() = buildJsonObject {
        put("energy_kcal", range()); put("protein_g", range())
        put("carbs_g", range()); put("fat_g", range())
    }

    fun task(kind: String) = if (kind == "photo") buildJsonObject {
        put("summary", "string")
        put("candidates", buildJsonArray { add(buildJsonObject {
            put("name", "string"); put("portion_range", "string")
            put("nutrition", nutrition()); put("confidence", 0.0)
        }) })
        put("unknowns", buildJsonArray { add("string") })
        put("advice", buildJsonArray { add("string") })
    } else buildJsonObject {
        put("summary", "string")
        put("combinations", buildJsonArray { add("string") })
        put("batch_nutrition", nutrition()); put("per_serving_nutrition", nutrition())
        put("gaps", buildJsonArray { add("string") }); put("risks", buildJsonArray { add("string") })
        put("minimal_adjustments", buildJsonArray { add("string") })
    }

    fun daily(
        tomorrow: LocalDate,
        environment: String,
        proteinTarget: JsonArray,
        priorityFoodIds: Set<String>,
        homeCooking: JsonObject? = null,
        carryovers: JsonArray = JsonArray(emptyList()),
    ) = buildJsonObject {
        val cookingEnabled = homeCooking?.get("enabled")?.jsonPrimitive?.booleanOrNull == true
        put("system_status", "stable|observe|adjust|risk")
        listOf("facts", "inferences", "core_advice", "do_not_adjust", "risk_signals").forEach { key ->
            put(key, buildJsonArray { add("string") })
        }
        put("priority_food_decisions", buildJsonArray {
            priorityFoodIds.sorted().forEach { id -> add(buildJsonObject {
                put("food_id", id); put("decision", "use|skip"); put("reason", "string")
            }) }
        })
        if (carryovers.isNotEmpty()) put("ingredient_carryover_decisions", buildJsonArray {
            carryovers.forEach { value ->
                val item = value as JsonObject
                add(buildJsonObject {
                    put("carryover_id", item.getValue("id")); put("ingredient", item.getValue("ingredient"))
                    put("decision", "use|skip|discard"); put("reason", "string"); put("planned_use", "string")
                })
            }
        })
        put("tomorrow_menu", buildJsonObject {
            put("date", tomorrow.toString()); put("environment", environment); put("protein_target_g", proteinTarget)
            put("meals", buildJsonArray {
                listOf("早餐", "午餐", "晚餐").forEach { name -> add(buildJsonObject {
                    put("name", name); put("foods", buildJsonArray { add("string") })
                    put("portion_guidance", "string"); put("protein_g", range())
                    put("substitutions", buildJsonArray { add("string") })
                    if (cookingEnabled) {
                        put("mode", when (name) { "早餐" -> "quick_assembly"; "午餐" -> "eat_out"; else -> "home_cook" })
                        if (name == "晚餐") put("recipe_card", buildJsonObject {
                            put("title", "string")
                            put("servings", homeCooking?.get("servings") ?: JsonPrimitive(1))
                            put("active_minutes", 15); put("total_minutes", 25)
                            put("cookware", buildJsonArray { add("stovetop_pan") })
                            put("ingredients", buildJsonArray { add(buildJsonObject {
                                put("name", "string"); put("amount", "string"); put("prep", "string")
                            }) })
                            put("seasonings", buildJsonArray { add(buildJsonObject {
                                put("name", "string"); put("amount", "string"); put("timing", "string")
                            }) })
                            put("steps", buildJsonArray { add(buildJsonObject {
                                put("instruction", "string"); put("heat", "string")
                                put("done_signal", "string"); put("minutes", 5)
                            }) })
                            put("failure_rescue", buildJsonArray { add("string") })
                            put("cleanup", "string"); put("gut_fallback", "string")
                        })
                    }
                }) }
            })
            put("conditional_snack", buildJsonObject {
                put("condition", "string"); put("options", buildJsonArray { add("string") })
            })
            put("training_adjustment", "string"); put("gut_adjustment", "string")
            if (cookingEnabled) {
                put("shopping_list", buildJsonArray { add(buildJsonObject {
                    put("name", "string"); put("amount", "string"); put("purpose", "string")
                    put("selection_guide", "string"); put("storage", "string"); put("required", true)
                }) })
                put("online_options", buildJsonArray { add(buildJsonObject {
                    put("category", "string"); put("package_size", "string"); put("skip_if", "string")
                    put("selection_criteria", buildJsonArray { add("string") })
                    put("search_keywords", buildJsonArray { add("string") })
                    put("pairs_with", buildJsonArray { add("string") })
                }) })
                put("reuse_plan", buildJsonObject {
                    put("horizon_days", homeCooking?.get("rotation_window_days") ?: JsonPrimitive(3))
                    put("items", buildJsonArray { add(buildJsonObject {
                        put("ingredient", "string"); put("tomorrow_use", "string"); put("storage", "string")
                        put("later_uses", buildJsonArray { add(buildJsonObject {
                            put("date", tomorrow.plusDays(1).toString()); put("use", "string")
                        }) })
                    }) })
                })
                put("rotation", buildJsonObject {
                    listOf("dish_key", "primary_protein", "primary_vegetable", "flavor_profile", "technique").forEach { put(it, "string") }
                })
            }
        })
        put("one_line_review", "string")
    }
}
