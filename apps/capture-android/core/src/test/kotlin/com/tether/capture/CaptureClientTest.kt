package com.tether.capture

import okio.Buffer
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CaptureClientTest {
    @Test
    fun resolveJoinsWithoutTrailingSlash() {
        val url = CaptureClient.resolve("https://host.example", "api/memories")
        assertEquals("https://host.example/api/memories", url.toString())
    }

    @Test
    fun resolveStripsTrailingSlashAndLeadingSlash() {
        val url = CaptureClient.resolve("https://host.example/", "/api/memories")
        assertEquals("https://host.example/api/memories", url.toString())
    }

    @Test
    fun resolveKeepsPortAndSubpath() {
        val url = CaptureClient.resolve("http://10.0.0.5:8000", "api/capture/voice")
        assertEquals("http://10.0.0.5:8000/api/capture/voice", url.toString())
    }

    @Test
    fun resolveRejectsNonHttpUrl() {
        assertNull(CaptureClient.resolve("not a url", "api/memories"))
    }

    @Test
    fun textRequestCarriesBearerAndJsonContent() {
        val request =
            CaptureClient.buildTextRequest("https://host.example", "sekret", "buy oat milk")
        assertEquals("POST", request.method)
        assertEquals("https://host.example/api/memories", request.url.toString())
        assertEquals("Bearer sekret", request.header("Authorization"))

        val buffer = Buffer()
        request.body!!.writeTo(buffer)
        val json = JSONObject(buffer.readUtf8())
        assertEquals("buy oat milk", json.getString("content"))
        assertTrue(
            request.body!!.contentType().toString().startsWith("application/json"),
        )
    }

    @Test
    fun voiceRequestBuildsMultipartWithFilePart() {
        val audio = File.createTempFile("voice_test", ".m4a")
        audio.writeBytes(byteArrayOf(1, 2, 3, 4))
        try {
            val request =
                CaptureClient.buildVoiceRequest("https://host.example/", "tok", audio)
            assertEquals("POST", request.method)
            assertEquals("https://host.example/api/capture/voice", request.url.toString())
            assertEquals("Bearer tok", request.header("Authorization"))

            val contentType = request.body!!.contentType().toString()
            assertTrue("expected multipart, got $contentType", contentType.startsWith("multipart/form-data"))

            val buffer = Buffer()
            request.body!!.writeTo(buffer)
            val rendered = buffer.readUtf8()
            assertTrue(rendered.contains("name=\"file\""))
            assertTrue(rendered.contains("filename=\"${audio.name}\""))
            assertTrue(rendered.contains("audio/mp4"))
        } finally {
            audio.delete()
        }
    }

    @Test
    fun parseTranscriptReadsField() {
        val body = JSONObject().put("transcript", "hello world").toString()
        assertEquals("hello world", CaptureClient.parseTranscript(body))
    }

    @Test
    fun parseTranscriptDefaultsEmptyWhenAbsent() {
        assertEquals("", CaptureClient.parseTranscript("{}"))
    }
}
