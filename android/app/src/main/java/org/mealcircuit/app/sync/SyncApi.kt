package org.mealcircuit.app.sync

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
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
import okhttp3.ResponseBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.mealcircuit.app.BuildConfig
import org.mealcircuit.app.io.readBounded
import java.net.URI
import java.util.concurrent.TimeUnit

internal fun ResponseBody.readBoundedText(maxBytes: Int, description: String): String {
    require(maxBytes >= 0)
    val declaredBytes = contentLength()
    require(declaredBytes < 0 || declaredBytes <= maxBytes.toLong()) {
        "$description 超过 $maxBytes 字节"
    }
    return byteStream().use { it.readBounded(maxBytes) }.decodeToString()
}

internal fun ResponseBody.readBoundedBytes(maxBytes: Int, description: String): ByteArray {
    require(maxBytes >= 0)
    val declaredBytes = contentLength()
    require(declaredBytes < 0 || declaredBytes <= maxBytes.toLong()) {
        "$description 超过 $maxBytes 字节"
    }
    return byteStream().use { it.readBounded(maxBytes) }
}

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
            ?: error("同步凭据已锁定")
        return try {
            execute(path, method, body, token)
        } catch (error: SyncHttpException) {
            if (error.status != 401) throw error
            token = refresh(token)
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
        authorized("/v1/blobs/${requireSafePathId(blobId, "资源标识")}/complete", "POST", buildJsonObject {})

    suspend fun uploadChunk(blobId: String, index: Int, value: ByteArray) =
        rawRequest("/v1/blobs/${requireSafePathId(blobId, "资源标识")}/chunks/$index", "PUT", value)

    suspend fun downloadChunk(blobId: String, index: Int): ByteArray? {
        return try {
            rawRequest("/v1/blobs/${requireSafePathId(blobId, "资源标识")}/chunks/$index", "GET", null)
        } catch (error: SyncHttpException) {
            if (error.status == 404) null else throw error
        }
    }

    private suspend fun refresh(staleAccessToken: String): String = refreshMutex.withLock {
        val currentAccess = vault.get("sync.access_token")?.decodeToString()
            ?: error("同步凭据已锁定")
        if (currentAccess != staleAccessToken) return@withLock currentAccess
        val refresh = vault.get("sync.refresh_token")?.decodeToString()
            ?: error("缺少刷新令牌")
        val response = anonymous(
            "/v1/sessions/refresh",
            "POST",
            buildJsonObject { put("refresh_token", refresh) },
        )
        val access = response.getValue("access_token").jsonPrimitive.content
        val rotated = response.getValue("refresh_token").jsonPrimitive.content
        vault.putAll(
            mapOf(
                "sync.access_token" to access.toByteArray(),
                "sync.refresh_token" to rotated.toByteArray(),
            )
        )
        access
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
            if (!response.isSuccessful) {
                val text = response.body?.readBoundedText(MAX_SYNC_ERROR_BYTES, "同步错误响应").orEmpty()
                throw SyncHttpException(response.code, errorDetail(response.code, text))
            }
            val text = response.body?.readBoundedText(MAX_SYNC_JSON_BYTES, "同步 JSON 响应").orEmpty()
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
                if (!response.isSuccessful) {
                    val text = response.body?.readBoundedText(MAX_SYNC_ERROR_BYTES, "同步错误响应").orEmpty()
                    throw SyncHttpException(response.code, errorDetail(response.code, text))
                }
                response.body?.readBoundedBytes(MAX_ENCRYPTED_BLOB_CHUNK_BYTES, "加密资源分块")
                    ?: byteArrayOf()
            }
        }
        val token = vault.get("sync.access_token")?.decodeToString() ?: error("同步凭据已锁定")
        return try {
            attempt(token)
        } catch (error: SyncHttpException) {
            if (error.status != 401) throw error
            attempt(refresh(token))
        }
    }

    /**
     * Roll back a session that was created remotely before its tokens could be persisted locally.
     * The supplied token is used only for this fixed endpoint and is never written or logged.
     */
    internal suspend fun revokeCreatedSession(accessToken: String): JsonObject =
        execute(
            "/v1/sessions/current",
            "DELETE",
            null,
            requireEphemeralAccessToken(accessToken),
        )

    /** Roll back a newly created account without depending on SecretVault token persistence. */
    internal suspend fun deleteCreatedAccount(accessToken: String, password: String): JsonObject =
        execute(
            "/v1/account",
            "DELETE",
            buildJsonObject { put("password", password) },
            requireEphemeralAccessToken(accessToken),
        )

    private fun errorDetail(status: Int, text: String): String {
        val detail = runCatching {
            json.parseToJsonElement(text).jsonObject["detail"]?.jsonPrimitive?.content
        }.getOrNull()
        return detail?.let(KNOWN_SERVER_ERRORS::get) ?: when (status) {
            400, 422 -> "同步请求格式无效（HTTP $status）"
            401 -> "同步身份验证失败（HTTP 401）"
            403 -> "同步服务拒绝此操作（HTTP 403）"
            404 -> "同步资源不存在（HTTP 404）"
            409, 410 -> "同步状态已变化，请刷新后重试（HTTP $status）"
            413 -> "同步数据超过服务限制（HTTP 413）"
            in 500..599 -> "同步服务暂时不可用（HTTP $status）"
            else -> "同步服务返回错误（HTTP $status）"
        }
    }

    private fun requireEphemeralAccessToken(value: String): String {
        require(value.length in 1..16_384 && value.none(Char::isWhitespace)) {
            "同步服务返回的访问令牌无效"
        }
        return value
    }

    private companion object {
        val refreshMutex = Mutex()
    }
}

class SyncHttpException(val status: Int, message: String) : Exception(message)

fun validateServerUrl(value: String): String {
    val clean = value.trim().trimEnd('/')
    require(clean.length in 1..MAX_SERVER_URL_CHARS) { "同步服务地址无效" }
    val uri = runCatching { URI(clean) }.getOrElse { throw IllegalArgumentException("同步服务地址无效", it) }
    require(uri.userInfo == null && uri.query == null && uri.fragment == null && uri.host != null)
    val scheme = uri.scheme?.lowercase() ?: error("同步服务地址无效")
    val host = uri.host.lowercase()
    val localhost = host in setOf("localhost", "127.0.0.1", "::1", "[::1]")
    require(scheme == "https" || (BuildConfig.ALLOW_INSECURE_LOCALHOST && scheme == "http" && localhost)) {
        "自定义同步服务地址必须使用 HTTPS"
    }
    val rawPath = uri.rawPath.orEmpty()
    require(rawPath.length <= MAX_SERVER_PATH_CHARS && '\\' !in rawPath)
    require(!ENCODED_PATH_DELIMITER.containsMatchIn(rawPath)) { "同步服务地址路径无效" }
    require(uri.path.orEmpty().split('/').none { it == "." || it == ".." }) { "同步服务地址路径无效" }
    val port = when {
        scheme == "https" && uri.port == 443 -> -1
        scheme == "http" && uri.port == 80 -> -1
        else -> uri.port
    }
    val path = uri.normalize().rawPath.orEmpty().trimEnd('/')
    return URI(scheme, null, host, port, path, null, null).toASCIIString().trimEnd('/')
}

internal fun requireSafePathId(value: String, description: String): String {
    require(value.length in 1..MAX_REMOTE_ID_CHARS && SAFE_REMOTE_ID.matches(value)) {
        "$description 无效"
    }
    return value
}

internal const val MAX_SYNC_JSON_BYTES = 32 * 1024 * 1024
private const val MAX_SYNC_ERROR_BYTES = 64 * 1024
private const val MAX_ENCRYPTED_BLOB_CHUNK_BYTES = 4 * 1024 * 1024 + 12 + 16
private const val MAX_SERVER_URL_CHARS = 2_048
private const val MAX_SERVER_PATH_CHARS = 512
private const val MAX_REMOTE_ID_CHARS = 128
private val SAFE_REMOTE_ID = Regex("[A-Za-z0-9_-]+")
private val ENCODED_PATH_DELIMITER = Regex("%(?:2e|2f|5c)", RegexOption.IGNORE_CASE)
private val KNOWN_SERVER_ERRORS = mapOf(
    "invalid credentials" to "登录名或密码错误",
    "registration is closed" to "同步服务已关闭注册",
    "login already exists" to "该登录名已存在",
    "recovery envelope not found" to "账户尚未配置恢复密钥",
    "finish or abort key rotation first" to "请先完成或中止密钥轮换",
    "another device is rotating this account" to "另一台设备正在执行密钥轮换",
    "account key rotation is in progress" to "账户正在执行密钥轮换",
    "pairing not found" to "配对请求不存在",
    "pairing expired or already claimed" to "配对二维码已过期或已使用",
    "pairing claim token invalid" to "配对二维码令牌无效",
    "device not found" to "设备不存在",
    "account or device disabled" to "账户或设备已停用",
    "refresh token invalid or reused" to "同步会话已失效，请重新登录",
    "refresh token expired or revoked" to "同步会话已过期或被撤销，请重新登录",
    "staged rotation inventory is incomplete" to "密钥轮换暂存数据不完整",
    "staged rotation inventory is smaller than the active inventory" to "密钥轮换缺少部分原有数据，已阻止提交",
    "recovery envelope must be configured before uploading encrypted data" to
        "上传加密数据前必须先确认恢复密钥",
)
