package com.tether.capture

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class HealthConnectSyncCoordinatorTest {
    @Test
    fun baselineUploadsBoundedBatches() = runTest {
        val events = mutableListOf<String>()
        val records = (1..1_001).map { index ->
            HealthConnectRecord.Steps(
                metadata = HealthConnectMetadata("steps-$index", "com.example.phone"),
                startTimeEpochMillis = index.toLong(),
                endTimeEpochMillis = index.toLong() + 1,
                count = index.toLong(),
            )
        }
        val health = FakeHealthConnectSource(
            events = events,
            startingToken = "token",
            baselineRecords = records,
            changes = HealthConnectChanges(emptyList(), emptyList(), "next"),
        )
        val host = FakeHealthConnectHost(events, HostSyncState.Initial, 0)
        val requestIds = CapturingRequestIds()
        val coordinator = HealthConnectSyncCoordinator(
            "installation",
            setOf(HealthConnectRecordType.STEPS),
            health,
            host,
            requestIds,
        )

        assertEquals(HealthConnectSyncResult.Success, coordinator.syncOnce())
        assertTrue(requestIds.stableKeys.all { it.startsWith("health-connect-v2:") })
        assertEquals(
            listOf(
                "host.uploadBaseline(request-2,token,token,records=500)",
                "host.uploadBaseline(request-3,token,token,records=500)",
                "host.uploadBaseline(request-4,token,token,records=1)",
            ),
            events.filter { it.startsWith("host.uploadBaseline") },
        )
    }

    @Test
    fun staleChangeTokenRefetchesHostStateAndConverges() = runTest {
        val events = mutableListOf<String>()
        val health = FakeHealthConnectSource(
            events = events,
            startingToken = "unused",
            baselineRecords = emptyList(),
            changes = HealthConnectChanges(
                records = listOf(
                    HealthConnectRecord.Steps(
                        metadata = HealthConnectMetadata("steps-latest", "com.example.phone"),
                        startTimeEpochMillis = 1,
                        endTimeEpochMillis = 2,
                        count = 10,
                    ),
                ),
                deletions = emptyList(),
                nextToken = "token-next",
            ),
        )
        val host = StaleOnceHost(events)
        val coordinator = HealthConnectSyncCoordinator(
            installationId = "pixel-installation",
            recordTypes = setOf(HealthConnectRecordType.STEPS),
            health = health,
            host = host,
            requestIds = SequentialRequestIds("request"),
        )

        val result = coordinator.syncOnce()

        assertEquals(HealthConnectSyncResult.Success, result)
        assertEquals(
            listOf(
                "host.getSyncState(token-stale)",
                "health.readChanges(token-stale)",
                "host.uploadChanges(request-1,token-stale,token-next,records=1)",
                "host.getSyncState(token-authoritative)",
                "health.readChanges(token-authoritative)",
                "host.uploadChanges(request-2,token-authoritative,token-next,records=1)",
            ),
            events,
        )
    }

    @Test
    fun initialSyncGetsTokenBeforeBaselineReadsThenConsumesPostBaselineChanges() = runTest {
        val events = mutableListOf<String>()
        val health = FakeHealthConnectSource(
            events = events,
            startingToken = "token-before-read",
            baselineRecords = listOf(
                HealthConnectRecord.HeartRate(
                    metadata = HealthConnectMetadata(
                        id = "heart-1",
                        dataOriginPackage = "com.example.watch",
                    ),
                    startTimeEpochMillis = 1_700_000_000_000,
                    endTimeEpochMillis = 1_700_000_060_000,
                    samples = listOf(HeartRateSample(1_700_000_001_000, 61)),
                ),
            ),
            changes = HealthConnectChanges(
                records = listOf(
                    HealthConnectRecord.Steps(
                        metadata = HealthConnectMetadata(
                            id = "steps-1",
                            dataOriginPackage = "com.example.phone",
                        ),
                        startTimeEpochMillis = 1_700_000_000_000,
                        endTimeEpochMillis = 1_700_003_600_000,
                        count = 1234,
                    ),
                ),
                deletions = emptyList(),
                nextToken = "token-after-change",
            ),
        )
        val host = FakeHealthConnectHost(
            events = events,
            state = HostSyncState.Initial,
            generation = 0,
        )
        val coordinator = HealthConnectSyncCoordinator(
            installationId = "pixel-installation",
            recordTypes = setOf(HealthConnectRecordType.HEART_RATE, HealthConnectRecordType.STEPS),
            health = health,
            host = host,
            requestIds = SequentialRequestIds("request"),
        )

        val result = coordinator.syncOnce()

        assertEquals(HealthConnectSyncResult.Success, result)
        assertEquals(
            listOf(
                "host.getSyncState",
                "health.getChangesToken",
                "host.startBaseline(token-before-read)",
                "health.scanBaseline(HEART_RATE,STEPS)",
                "host.uploadBaseline(request-2,token-before-read,token-before-read,records=1)",
                "host.completeBaseline(token-before-read)",
                "health.readChanges(token-before-read)",
                "host.uploadChanges(request-4,token-before-read,token-after-change,records=1)",
            ),
            events,
        )
    }
}

private class CapturingRequestIds : RequestIds {
    val stableKeys = mutableListOf<String>()

    override fun next(): String = "request-${stableKeys.size + 1}"

    override fun stable(key: String): String {
        stableKeys += key
        return "request-${stableKeys.size}"
    }
}

private class FakeHealthConnectSource(
    private val events: MutableList<String>,
    private val startingToken: String,
    private val baselineRecords: List<HealthConnectRecord>,
    private val changes: HealthConnectChanges,
) : HealthConnectSource {
    override suspend fun getChangesToken(recordTypes: Set<HealthConnectRecordType>): String {
        events += "health.getChangesToken"
        return startingToken
    }

    override suspend fun scanBaseline(
        recordTypes: Set<HealthConnectRecordType>,
        consumePage: suspend (List<HealthConnectRecord>) -> Unit,
    ): Map<HealthConnectRecordType, HealthConnectScanBounds> {
        events += "health.scanBaseline(${recordTypes.joinToString(",")})"
        baselineRecords.chunked(500).forEach { consumePage(it) }
        return recordTypes.associateWith {
            HealthConnectScanBounds(
                startTimeEpochMillis = 1_699_990_000_000,
                endTimeEpochMillis = 1_700_010_000_000,
            )
        }
    }

    override suspend fun readChanges(token: String): HealthConnectChanges {
        events += "health.readChanges($token)"
        return changes
    }
}

private class StaleOnceHost(private val events: MutableList<String>) : HealthConnectHost {
    private var firstUpload = true
    private var cursorToken = "token-stale"

    override suspend fun getSyncState(
        installationId: String,
        recordTypes: Set<HealthConnectRecordType>,
    ): HostSyncCursor {
        events += "host.getSyncState($cursorToken)"
        return HostSyncCursor(state = HostSyncState.Changes, generation = 7, token = cursorToken)
    }

    override suspend fun startBaseline(request: StartBaselineRequest): HostSyncCursor =
        error("baseline not expected")

    override suspend fun uploadBatch(request: HealthConnectBatchRequest): BatchUploadResult {
        events += "host.uploadChanges(${request.requestId},${request.expectedToken},${request.nextToken},records=${request.records.size})"
        return if (firstUpload) {
            firstUpload = false
            cursorToken = "token-authoritative"
            BatchUploadResult.StaleToken
        } else {
            BatchUploadResult.Accepted
        }
    }

    override suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor =
        error("baseline not expected")
}

private class FakeHealthConnectHost(
    private val events: MutableList<String>,
    private val state: HostSyncState,
    private val generation: Int,
) : HealthConnectHost {
    override suspend fun getSyncState(
        installationId: String,
        recordTypes: Set<HealthConnectRecordType>,
    ): HostSyncCursor {
        events += "host.getSyncState"
        return HostSyncCursor(state = state, generation = generation, token = null)
    }

    override suspend fun startBaseline(request: StartBaselineRequest): HostSyncCursor {
        events += "host.startBaseline(${request.startingToken})"
        return HostSyncCursor(state = HostSyncState.Baseline, generation = generation + 1, token = request.startingToken)
    }

    override suspend fun uploadBatch(request: HealthConnectBatchRequest): BatchUploadResult {
        events += "host.upload${request.mode.name.lowercase().replaceFirstChar { it.uppercase() }}(${request.requestId},${request.expectedToken},${request.nextToken},records=${request.records.size})"
        return BatchUploadResult.Accepted
    }

    override suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor {
        events += "host.completeBaseline(${request.expectedToken})"
        return HostSyncCursor(state = HostSyncState.Changes, generation = request.generation, token = request.expectedToken)
    }
}
