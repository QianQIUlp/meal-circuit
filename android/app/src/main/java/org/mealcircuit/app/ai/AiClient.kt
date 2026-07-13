package org.mealcircuit.app.ai

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.add
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.mealcircuit.app.sync.SecretVault
import java.util.Base64
import java.util.concurrent.TimeUnit

enum class AiProvider { OPENAI, ANTHROPIC, DEEPSEEK }

data class AiConfiguration(val provider: AiProvider, val model: String)

class AiClient(
    private val vault: SecretVault,
    private val json: Json = Json { ignoreUnknownKeys = true },
) {
    private val client = OkHttpClient.Builder().readTimeout(120, TimeUnit.SECONDS).build()

    suspend fun generate(
        configuration: AiConfiguration,
        kind: String,
        context: JsonObject,
        image: ByteArray? = null,
        imageMediaType: String? = null,
    ): JsonObject = withContext(Dispatchers.IO) {
        if (configuration.provider == AiProvider.DEEPSEEK && image != null) {
            error("DeepSeek text API cannot process MealCircuit photo tasks")
        }
        val key = vault.get("ai.${configuration.provider.name.lowercase()}")?.decodeToString()
            ?: error("API key is not configured on this device")
        val prompt = "Return only the complete MealCircuit $kind JSON result for this context:\n$context"
        val outputSchema = schemaFromExample(context.getValue("result_schema"), "result")
        val (url, headers, body) = when (configuration.provider) {
            AiProvider.OPENAI -> Triple(
                "https://api.openai.com/v1/responses",
                mapOf("Authorization" to "Bearer $key"),
                buildJsonObject {
                    put("model", configuration.model)
                    put("instructions", "Use only the supplied MealCircuit context. Preserve unknowns and obey the private doctrine in the context.")
                    put("input", buildJsonArray {
                        add(buildJsonObject {
                            put("role", "user")
                            put("content", buildJsonArray {
                                image?.let {
                                    add(buildJsonObject {
                                        put("type", "input_image")
                                        put("image_url", "data:${imageMediaType ?: "image/jpeg"};base64,${Base64.getEncoder().encodeToString(it)}")
                                    })
                                }
                                add(buildJsonObject { put("type", "input_text"); put("text", prompt) })
                            })
                        })
                    })
                    put("max_output_tokens", 8192)
                    put("text", buildJsonObject {
                        put("format", buildJsonObject {
                            put("type", "json_schema"); put("name", "mealcircuit_${kind}_result")
                            put("schema", outputSchema); put("strict", false)
                        })
                    })
                },
            )
            AiProvider.ANTHROPIC -> Triple(
                "https://api.anthropic.com/v1/messages",
                mapOf("x-api-key" to key, "anthropic-version" to "2023-06-01"),
                buildJsonObject {
                    put("model", configuration.model)
                    put("max_tokens", 8192)
                    put("system", "Use only the supplied MealCircuit context. Preserve unknowns and obey the private doctrine in the context.")
                    put("messages", buildJsonArray {
                        add(buildJsonObject {
                            put("role", "user")
                            put("content", buildJsonArray {
                                image?.let {
                                    add(buildJsonObject {
                                        put("type", "image")
                                        put("source", buildJsonObject {
                                            put("type", "base64")
                                            put("media_type", imageMediaType ?: "image/jpeg")
                                            put("data", Base64.getEncoder().encodeToString(it))
                                        })
                                    })
                                }
                                add(buildJsonObject { put("type", "text"); put("text", prompt) })
                            })
                        })
                    })
                    put("tools", buildJsonArray {
                        add(buildJsonObject {
                            put("name", "submit_mealcircuit_result")
                            put("description", "Submit the complete validated MealCircuit JSON result")
                            put("input_schema", outputSchema)
                        })
                    })
                    put("tool_choice", buildJsonObject {
                        put("type", "tool"); put("name", "submit_mealcircuit_result")
                    })
                },
            )
            AiProvider.DEEPSEEK -> Triple(
                "https://api.deepseek.com/chat/completions",
                mapOf("Authorization" to "Bearer $key"),
                buildJsonObject {
                    put("model", configuration.model)
                    put("response_format", buildJsonObject { put("type", "json_object") })
                    put("messages", buildJsonArray {
                        add(buildJsonObject {
                            put("role", "system")
                            put("content", "Use only the supplied MealCircuit context. Return complete JSON and preserve unknowns.")
                        })
                        add(buildJsonObject { put("role", "user"); put("content", prompt) })
                    })
                    put("max_tokens", 8192); put("stream", false)
                    put("thinking", buildJsonObject { put("type", "disabled") })
                },
            )
        }
        val request = Request.Builder().url(url).apply {
            headers.forEach { (name, value) -> header(name, value) }
            header("Content-Type", "application/json")
            post(body.toString().toRequestBody("application/json".toMediaType()))
        }.build()
        client.newCall(request).execute().use { response ->
            val responseBody = response.body?.string().orEmpty()
            require(response.isSuccessful) { "AI provider returned HTTP ${response.code}" }
            parseProviderResult(configuration.provider, json.parseToJsonElement(responseBody).jsonObject)
        }
    }

    fun saveKey(provider: AiProvider, value: String) {
        require(value.isNotBlank())
        vault.put("ai.${provider.name.lowercase()}", value.toByteArray())
    }

    private fun parseProviderResult(provider: AiProvider, response: JsonObject): JsonObject {
        if (provider == AiProvider.ANTHROPIC) {
            return response.getValue("content").jsonArray
                .first { it.jsonObject["type"]?.jsonPrimitive?.content == "tool_use" }
                .jsonObject.getValue("input").jsonObject
        }
        val text = when (provider) {
            AiProvider.OPENAI -> response["output"]!!.jsonArray
                .flatMap { it.jsonObject["content"]?.jsonArray.orEmpty() }
                .first { it.jsonObject["type"]?.jsonPrimitive?.content == "output_text" }
                .jsonObject.getValue("text").jsonPrimitive.content
            AiProvider.ANTHROPIC -> error("handled above")
            AiProvider.DEEPSEEK -> response.getValue("choices").jsonArray.first().jsonObject
                .getValue("message").jsonObject.getValue("content").jsonPrimitive.content
        }
        return json.parseToJsonElement(text.removePrefix("```json").removeSuffix("```").trim()).jsonObject
    }

    private fun schemaFromExample(value: JsonElement, key: String): JsonObject = when (value) {
        is JsonObject -> buildJsonObject {
            put("type", "object"); put("additionalProperties", false)
            put("required", buildJsonArray { value.keys.forEach { add(it) } })
            put("properties", buildJsonObject { value.forEach { (name, child) -> put(name, schemaFromExample(child, name)) } })
        }
        is JsonArray -> {
            val item = value.firstOrNull()
            val arraySchema = buildJsonObject {
                put("type", "array"); item?.let { put("items", schemaFromExample(it, key)) }
                if (key in setOf("facts", "core_advice", "foods", "options")) put("minItems", 1)
                if (key == "core_advice") put("maxItems", 3)
                if (value.size == 2 && value.all { it is JsonPrimitive && it.doubleOrNull != null }) {
                    put("minItems", 2); put("maxItems", 2)
                }
            }
            if (value.size == 2 && value.all { it is JsonPrimitive && it.doubleOrNull != null } && key != "protein_target_g") {
                buildJsonObject { put("anyOf", buildJsonArray { add(arraySchema); add(buildJsonObject { put("type", "null") }) }) }
            } else arraySchema
        }
        is JsonNull -> buildJsonObject { put("type", "null") }
        is JsonPrimitive -> when {
            value.isString -> buildJsonObject {
                val content = value.content
                if ('|' in content) put("enum", buildJsonArray { content.split('|').forEach { add(it) } })
                else put("type", "string")
            }
            value.booleanOrNull != null -> buildJsonObject { put("type", "boolean") }
            else -> buildJsonObject {
                put("type", "number")
                if (key == "confidence") { put("minimum", 0); put("maximum", 1) }
                else put("minimum", 0)
            }
        }
        else -> error("unsupported schema example")
    }
}
