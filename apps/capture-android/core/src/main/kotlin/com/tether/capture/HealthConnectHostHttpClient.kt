package com.tether.capture

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class HealthConnectHostHttpClient(
    private val baseUrl: String,
    private val token: String,
    private val client: OkHttpClient = defaultClient,
) : HealthConnectHost {
    override suspend fun getSyncState(
        installationId: String,
        recordTypes: Set<HealthConnectRecordType>,
    ): HostSyncCursor {
        val url = resolve("api/telemetry/health-connect/sync-state")
            ?.newBuilder()
            ?.addQueryParameter("installation_id", installationId)
            ?.addQueryParameter("record_types", recordTypes.joinToString(",") { it.wireName })
            ?.build()
            ?: throw IllegalArgumentException("invalid host URL: $baseUrl")
        val request = Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")
            .get()
            .build()
        return executeJson(request).toCursor()
    }

    override suspend fun startBaseline(request: StartBaselineRequest): HostSyncCursor {
        val httpRequest = jsonPost(
            path = "api/telemetry/health-connect/sync-state/baselines",
            body = HealthConnectWireJson.startBaselineRequest(request),
        )
        return executeJson(httpRequest).toCursor()
    }

    override suspend fun uploadBatch(request: HealthConnectBatchRequest): BatchUploadResult {
        val httpRequest = jsonPost(
            path = "api/telemetry/health-connect/batches",
            body = HealthConnectWireJson.batchRequest(request),
        )
        client.newCall(httpRequest).execute().use { response ->
            return when (response.code) {
                200, 201 -> BatchUploadResult.Accepted
                409 -> BatchUploadResult.StaleToken
                else -> throw IOException("Health Connect batch failed: HTTP ${response.code}")
            }
        }
    }

    override suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor {
        val httpRequest = jsonPost(
            path = "api/telemetry/health-connect/sync-state/baselines/complete",
            body = HealthConnectWireJson.completeBaselineRequest(request),
        )
        return executeJson(httpRequest).toCursor()
    }

    private fun jsonPost(path: String, body: JSONObject): Request {
        val url = resolve(path)
            ?: throw IllegalArgumentException("invalid host URL: $baseUrl")
        return Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")
            .post(body.toString().toRequestBody(JSON_MEDIA_TYPE.toMediaType()))
            .build()
    }

    private fun resolve(path: String): HttpUrl? {
        val parsed = baseUrl.trim().trimEnd('/').toHttpUrlOrNull() ?: return null
        return parsed.newBuilder().addPathSegments(path.trimStart('/')).build()
    }

    private fun executeJson(request: Request): JSONObject {
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw IOException("Health Connect request failed: HTTP ${response.code}")
            }
            return JSONObject(response.body?.string().orEmpty())
        }
    }

    private fun JSONObject.toCursor(): HostSyncCursor = HostSyncCursor(
        state = when (getString("status")) {
            "initial" -> HostSyncState.Initial
            "baseline" -> HostSyncState.Baseline
            "changes" -> HostSyncState.Changes
            else -> throw IOException("unknown Health Connect sync status")
        },
        generation = getInt("baseline_generation"),
        token = optString("current_token").takeIf { !isNull("current_token") },
    )

    private companion object {
        const val JSON_MEDIA_TYPE = "application/json; charset=utf-8"

        val defaultClient: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .callTimeout(60, TimeUnit.SECONDS)
            .build()
    }
}
