package org.mealcircuit.app.sync

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.mealcircuit.app.BuildConfig
import java.net.URI
import java.util.concurrent.TimeUnit

class SyncApi(
    serverUrl: String,
    private val vault: SecretVault,
    private val json: Json = Json { ignoreUnknownKeys = true; encodeDefaults = true },
) {
    val baseUrl = validateServerUrl(serverUrl)
    private val client = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()
    private val mediaType = "application/json".toMediaType()

    suspend fun anonymous(path: String, method: String = "GET", body: JsonObject? = null): JsonObject =
        execute(path, method, body, null)

    suspend fun authorized(path: String, method: String = "GET", body: JsonObject? = null): JsonObject {
        var token = vault.get("sync.access_token")?.decodeToString()
            ?: error("Synchronization is locked")
        return try {
            execute(path, method, body, token)
        } catch (error: SyncHttpException) {
            if (error.status != 401) throw error
            token = refresh()
            execute(path, method, body, token)
        }
    }

    suspend fun push(operations: JsonObject) = authorized("/v1/sync/push", "POST", operations)
    suspend fun capabilities() = anonymous("/v1/capabilities")
    suspend fun pull(cursor: Long, offset: Int = 0, limit: Int = 500) =
        authorized("/v1/sync/pull?cursor=$cursor&limit=$limit&snapshot_offset=$offset")
    suspend fun ack(cursor: Long) = authorized(
        "/v1/sync/ack",
        "POST",
        buildJsonObject { put("cursor", cursor) },
    )

    suspend fun createBlob(body: JsonObject) = authorized("/v1/blobs", "POST", body)
    suspend fun completeBlob(blobId: String) =
        authorized("/v1/blobs/$blobId/complete", "POST", buildJsonObject {})

    suspend fun uploadChunk(blobId: String, index: Int, value: ByteArray) =
        rawRequest("/v1/blobs/$blobId/chunks/$index", "PUT", value)

    suspend fun downloadChunk(blobId: String, index: Int): ByteArray? {
        return try {
            rawRequest("/v1/blobs/$blobId/chunks/$index", "GET", null)
        } catch (error: SyncHttpException) {
            if (error.status == 404) null else throw error
        }
    }

    private suspend fun refresh(): String {
        val refresh = vault.get("sync.refresh_token")?.decodeToString()
            ?: error("Missing refresh token")
        val response = anonymous(
            "/v1/sessions/refresh",
            "POST",
            buildJsonObject { put("refresh_token", refresh) },
        )
        val access = response.getValue("access_token").jsonPrimitive.content
        val rotated = response.getValue("refresh_token").jsonPrimitive.content
        vault.put("sync.access_token", access.toByteArray())
        vault.put("sync.refresh_token", rotated.toByteArray())
        return access
    }

    private suspend fun execute(
        path: String,
        method: String,
        body: JsonObject?,
        token: String?,
    ): JsonObject = withContext(Dispatchers.IO) {
        val requestBody = body?.let { json.encodeToString(JsonObject.serializer(), it).toRequestBody(mediaType) }
        val builder = Request.Builder().url(baseUrl + path).header("Accept", "application/json")
        token?.let { builder.header("Authorization", "Bearer $it") }
        builder.method(method, requestBody)
        client.newCall(builder.build()).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val detail = runCatching {
                    json.parseToJsonElement(text).jsonObject["detail"]?.jsonPrimitive?.content
                }.getOrNull() ?: "HTTP ${response.code}"
                throw SyncHttpException(response.code, detail)
            }
            if (text.isBlank()) JsonObject(emptyMap()) else json.parseToJsonElement(text).jsonObject
        }
    }

    private suspend fun rawRequest(path: String, method: String, value: ByteArray?): ByteArray {
        suspend fun attempt(token: String): ByteArray = withContext(Dispatchers.IO) {
            val body = value?.toRequestBody("application/octet-stream".toMediaType())
            val request = Request.Builder()
                .url(baseUrl + path)
                .header("Authorization", "Bearer $token")
                .method(method, body)
                .build()
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw SyncHttpException(response.code, "HTTP ${response.code}")
                response.body?.bytes() ?: byteArrayOf()
            }
        }
        val token = vault.get("sync.access_token")?.decodeToString() ?: error("Synchronization is locked")
        return try {
            attempt(token)
        } catch (error: SyncHttpException) {
            if (error.status != 401) throw error
            attempt(refresh())
        }
    }
}

class SyncHttpException(val status: Int, message: String) : Exception(message)

fun validateServerUrl(value: String): String {
    val clean = value.trim().trimEnd('/')
    val uri = URI(clean)
    require(uri.userInfo == null && uri.query == null && uri.fragment == null && uri.host != null)
    val localhost = uri.host in setOf("localhost", "127.0.0.1", "::1")
    require(uri.scheme == "https" || (BuildConfig.ALLOW_INSECURE_LOCALHOST && uri.scheme == "http" && localhost)) {
        "Custom synchronization URLs must use HTTPS"
    }
    return clean
}
