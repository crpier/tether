package com.tether.capture

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class HealthConnectHostHttpClientTest {
    @Test
    fun getSyncStateUsesBearerAndRecordTypesQuery() = runTest {
        val server = MockWebServer()
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody(
                    JSONObject()
                        .put("status", "changes")
                        .put("baseline_generation", 3)
                        .put("current_token", "opaque-token")
                        .put("installation_id", "pixel-installation")
                        .put("record_types", listOf("heart_rate", "steps"))
                        .toString(),
                ),
        )
        server.start()
        try {
            val client = HealthConnectHostHttpClient(
                baseUrl = server.url("/").toString(),
                token = "api-token",
            )

            val state = client.getSyncState(
                installationId = "pixel-installation",
                recordTypes = linkedSetOf(HealthConnectRecordType.HEART_RATE, HealthConnectRecordType.STEPS),
            )

            val request = server.takeRequest()
            assertEquals("GET", request.method)
            assertEquals("Bearer api-token", request.headers["Authorization"])
            assertEquals("/api/telemetry/health-connect/sync-state?installation_id=pixel-installation&record_types=heart_rate%2Csteps", request.path)
            assertEquals(HostSyncCursor(HostSyncState.Changes, 3, "opaque-token"), state)
        } finally {
            server.close()
        }
    }
}
