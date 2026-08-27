package com.tether.capture

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class HealthConnectHostHttpClientTest {
    @Test
    fun canonicalStepSnapshotUsesAuthenticatedIngestionRoute() = runTest {
        val server = MockWebServer()
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody(
                    """{"accepted":1,"deleted":0,"replayed":false,"skipped":0,"status":"accepted"}""",
                ),
        )
        server.start()
        try {
            val client = HealthConnectHostHttpClient(
                baseUrl = server.url("/").toString(),
                token = "api-token",
            )

            client.uploadStepAggregateSnapshot(
                HealthConnectStepAggregateSnapshotRequest(
                    installationId = "pixel-installation",
                    requestId = "snapshot-1",
                    snapshot = HealthConnectStepAggregateSnapshot(
                        startTimeEpochMillis = 1_700_000_000_000,
                        endTimeEpochMillis = 1_700_007_200_000,
                        buckets = listOf(
                            HealthConnectStepAggregateBucket(
                                startTimeEpochMillis = 1_700_000_000_000,
                                endTimeEpochMillis = 1_700_003_600_000,
                                zoneOffsetSeconds = 3_600,
                                count = 321,
                            ),
                        ),
                    ),
                ),
            )

            val request = server.takeRequest()
            assertEquals("POST", request.method)
            assertEquals("Bearer api-token", request.headers["Authorization"])
            assertEquals("/api/telemetry/health-connect/step-aggregates", request.path)
            assertEquals(
                JSONObject(
                    """{"installation_id":"pixel-installation","request_id":"snapshot-1","start_time":1700000000000,"end_time":1700007200000,"buckets":[{"start_time":1700000000000,"end_time":1700003600000,"zone_offset_seconds":3600,"count":321}]}""",
                ).toString(),
                JSONObject(request.body.readUtf8()).toString(),
            )
        } finally {
            server.close()
        }
    }

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
