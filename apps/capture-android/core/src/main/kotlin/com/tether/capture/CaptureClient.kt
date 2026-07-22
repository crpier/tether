package com.tether.capture

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Builds and issues the two capture requests the dumb client makes against the
 * Tether host. The request-*building* half is deliberately pure and static so
 * it can be exercised by JVM unit tests with no network and no Android runtime;
 * the [send] helpers wrap those requests in a short-timeout OkHttp call.
 */
object CaptureClient {
    private const val JSON_MEDIA_TYPE = "application/json; charset=utf-8"
    private const val AUDIO_MEDIA_TYPE = "audio/mp4"

    /** Single-tenant local server: keep timeouts tight so hangs surface fast. */
    val client: OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .callTimeout(60, TimeUnit.SECONDS)
            .build()

    /**
     * Join a user-entered base URL and an API path without doubling or dropping
     * the slash. Returns null if [baseUrl] is not a parseable http(s) URL.
     */
    fun resolve(baseUrl: String, path: String): HttpUrl? {
        val trimmed = baseUrl.trim().trimEnd('/')
        val parsed = trimmed.toHttpUrlOrNull() ?: return null
        val relative = path.trimStart('/')
        return parsed.newBuilder().addPathSegments(relative).build()
    }

    /** Build the `POST /api/memories` text-capture request (JSON `{content}`). */
    fun buildTextRequest(baseUrl: String, token: String, content: String): Request {
        val url = resolve(baseUrl, "api/memories")
            ?: throw IllegalArgumentException("invalid host URL: $baseUrl")
        val payload = JSONObject().put("content", content).toString()
        val body: RequestBody = payload.toRequestBody(JSON_MEDIA_TYPE.toMediaType())
        return Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")
            .post(body)
            .build()
    }

    /**
     * Build the `POST /api/capture/voice` multipart request. The audio is sent
     * under the `file` part the host requires, with a filename so the upstream
     * STT provider can infer the format.
     */
    fun buildVoiceRequest(baseUrl: String, token: String, audio: File): Request {
        val url = resolve(baseUrl, "api/capture/voice")
            ?: throw IllegalArgumentException("invalid host URL: $baseUrl")
        val audioBody = audio.asRequestBody(AUDIO_MEDIA_TYPE)
        val multipart =
            MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", audio.name, audioBody)
                .build()
        return Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")
            .post(multipart)
            .build()
    }

    /** Parse the `transcript` field from the voice endpoint's JSON response. */
    fun parseTranscript(responseBody: String): String =
        JSONObject(responseBody).optString("transcript")

    private fun File.asRequestBody(mediaType: String): RequestBody =
        this.readBytes().toRequestBody(mediaType.toMediaType())
}
