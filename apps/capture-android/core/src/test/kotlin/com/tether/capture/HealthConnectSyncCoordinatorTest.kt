package com.tether.capture

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

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
        assertTrue(requestIds.stableKeys.all { it.startsWith("health-connect-v3:") })
        assertEquals(
            listOf(
                "host.uploadBaseline(request-3,token,token,records=500)",
                "host.uploadBaseline(request-4,token,token,records=500)",
                "host.uploadBaseline(request-5,token,token,records=1)",
            ),
            events.filter { it.startsWith("host.uploadBaseline") },
        )
    }

    @Test
    fun interruptedBaselineResumesAtFirstUnacknowledgedHealthConnectPage() = runTest {
        val records = (1..1_200).map { index ->
            HealthConnectRecord.HeartRate(
                metadata = HealthConnectMetadata("heart-$index", "com.example.watch"),
                startTimeEpochMillis = index.toLong(),
                endTimeEpochMillis = index.toLong() + 1,
                samples = emptyList(),
            )
        }
        val source = RestartableBaselineSource(records)
        val host = InterruptOnceBaselineHost()
        val checkpoints = InMemoryBaselineCheckpointStore()

        fun coordinator() = HealthConnectSyncCoordinator(
            installationId = "installation",
            recordTypes = setOf(HealthConnectRecordType.HEART_RATE),
            health = source,
            host = host,
            requestIds = SequentialRequestIds("request"),
            baselineCheckpoints = checkpoints,
        )

        try {
            coordinator().syncOnce()
            throw AssertionError("first baseline should be interrupted")
        } catch (_: IOException) {
            // WorkManager recreates the coordinator for its retry.
        }

        assertEquals(HealthConnectSyncResult.Success, coordinator().syncOnce())
        assertEquals(
            listOf("heart-1", "heart-501", "heart-501", "heart-1001"),
            host.attemptedFirstRecordIds,
        )
        assertEquals(null, checkpoints.loadBaselineCheckpoint())
    }

    @Test
    fun changesStateDiscardsACompletedBaselinesCheckpoint() = runTest {
        val checkpoints = InMemoryBaselineCheckpointStore()
        checkpoints.saveBaselineCheckpoint(
            HealthConnectBaselineCheckpoint(
                generation = 1,
                startingToken = "old-token",
                recordTypes = setOf(HealthConnectRecordType.HEART_RATE),
                scanProgress = HealthConnectBaselineScanProgress(
                    startTimeEpochMillis = 1,
                    endTimeEpochMillis = 2,
                    completedRecordTypes = setOf(HealthConnectRecordType.HEART_RATE),
                    currentRecordType = null,
                    nextPageToken = null,
                ),
            ),
        )
        val coordinator = HealthConnectSyncCoordinator(
            installationId = "installation",
            recordTypes = setOf(HealthConnectRecordType.HEART_RATE),
            health = FakeHealthConnectSource(
                events = mutableListOf(),
                startingToken = "unused",
                baselineRecords = emptyList(),
                changes = HealthConnectChanges(emptyList(), emptyList(), "next-token"),
            ),
            host = FakeHealthConnectHost(
                events = mutableListOf(),
                state = HostSyncState.Changes,
                generation = 2,
                token = "current-token",
            ),
            requestIds = SequentialRequestIds("request"),
            baselineCheckpoints = checkpoints,
        )

        assertEquals(HealthConnectSyncResult.Success, coordinator.syncOnce())
        assertEquals(null, checkpoints.loadBaselineCheckpoint())
    }

    @Test
    fun oversizedChangesPageDrainsInBoundedBatchesAdvancingOnlyAtTheEnd() = runTest {
        val records = (1..2_501).map { index ->
            HealthConnectRecord.Steps(
                metadata = HealthConnectMetadata("steps-$index", "com.example.phone"),
                startTimeEpochMillis = index.toLong(),
                endTimeEpochMillis = index.toLong() + 1,
                count = index.toLong(),
            )
        }
        val health = FakeHealthConnectSource(
            events = mutableListOf(),
            startingToken = "unused",
            baselineRecords = emptyList(),
            changes = HealthConnectChanges(
                records = records,
                deletions = listOf(HealthConnectDeletion(HealthConnectRecordType.STEPS, "gone-1")),
                nextToken = "token-next",
            ),
        )
        val host = RecordingChangesHost(initialState = HostSyncState.Changes, token = "token-page")
        val coordinator = HealthConnectSyncCoordinator(
            "installation",
            setOf(HealthConnectRecordType.STEPS),
            health,
            host,
            SequentialRequestIds("request"),
        )

        assertEquals(HealthConnectSyncResult.Success, coordinator.syncOnce())
        assertTrue(host.batches.all { it.records.size <= HealthConnectSyncCoordinator.MAX_PARENT_RECORDS_PER_BATCH })
        assertEquals(listOf(1_000, 1_000, 501), host.batches.map { it.records.size })
        // The cursor must not move until every record of the page is accepted.
        assertTrue(
            host.batches.dropLast(1).all { it.nextToken == it.expectedToken },
        )
        val last = host.batches.last()
        assertEquals("token-next", last.nextToken)
        assertEquals(1, last.deletions.size)
        assertTrue(host.batches.dropLast(1).all { it.deletions.isEmpty() })
    }

    @Test
    fun conflictMidPageFailsWithoutAdoptingThePageToken() = runTest {
        val records = (1..1_001).map { index ->
            HealthConnectRecord.Steps(
                metadata = HealthConnectMetadata("steps-$index", "com.example.phone"),
                startTimeEpochMillis = index.toLong(),
                endTimeEpochMillis = index.toLong() + 1,
                count = index.toLong(),
            )
        }
        val health = FakeHealthConnectSource(
            events = mutableListOf(),
            startingToken = "unused",
            baselineRecords = emptyList(),
            changes = HealthConnectChanges(records, emptyList(), "token-next"),
        )
        val host = RecordingChangesHost(
            initialState = HostSyncState.Changes,
            token = "token-page",
            failAfterAcceptedChunks = 1,
        )
        val coordinator = HealthConnectSyncCoordinator(
            "installation",
            setOf(HealthConnectRecordType.STEPS),
            health,
            host,
            SequentialRequestIds("request"),
        )

        assertEquals(HealthConnectSyncResult.Failed("boom"), coordinator.syncOnce())
        assertEquals("token-page", host.adoptedToken)
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
                "health.readStepAggregateSnapshot",
                "host.getSyncState(token-stale)",
                "health.readChanges(token-stale)",
                "host.uploadChanges(request-2,token-stale,token-next,records=1)",
                "host.getSyncState(token-authoritative)",
                "health.readChanges(token-authoritative)",
                "host.uploadChanges(request-3,token-authoritative,token-next,records=1)",
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
                "health.readStepAggregateSnapshot",
                "host.uploadStepAggregates(request-1,buckets=0)",
                "host.getSyncState",
                "health.getChangesToken",
                "host.startBaseline(token-before-read)",
                "health.scanBaseline(HEART_RATE,STEPS)",
                "host.uploadBaseline(request-3,token-before-read,token-before-read,records=1)",
                "host.completeBaseline(token-before-read)",
                "health.readChanges(token-before-read)",
                "host.uploadChanges(request-5,token-before-read,token-after-change,records=1)",
            ),
            events,
        )
    }

    @Test
    fun stepSyncUploadsCanonicalAggregateBeforeRawChanges() = runTest {
        val events = mutableListOf<String>()
        val health = FakeHealthConnectSource(
            events = events,
            startingToken = "unused",
            baselineRecords = emptyList(),
            changes = HealthConnectChanges(emptyList(), emptyList(), "token-next"),
            stepAggregateSnapshot = HealthConnectStepAggregateSnapshot(
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
        )
        val host = FakeHealthConnectHost(
            events = events,
            state = HostSyncState.Changes,
            generation = 1,
            token = "token-current",
        )
        val coordinator = HealthConnectSyncCoordinator(
            installationId = "pixel-installation",
            recordTypes = setOf(HealthConnectRecordType.STEPS),
            health = health,
            host = host,
            requestIds = SequentialRequestIds("request"),
        )

        assertEquals(HealthConnectSyncResult.Success, coordinator.syncOnce())
        assertEquals(
            listOf(
                "health.readStepAggregateSnapshot",
                "host.uploadStepAggregates(request-1,buckets=1)",
                "host.getSyncState",
                "health.readChanges(token-current)",
                "host.uploadChanges(request-2,token-current,token-next,records=0)",
            ),
            events,
        )
    }
}

private class InMemoryBaselineCheckpointStore : HealthConnectBaselineCheckpointStore {
    private var checkpoint: HealthConnectBaselineCheckpoint? = null

    override suspend fun loadBaselineCheckpoint(): HealthConnectBaselineCheckpoint? = checkpoint

    override suspend fun saveBaselineCheckpoint(checkpoint: HealthConnectBaselineCheckpoint) {
        this.checkpoint = checkpoint
    }

    override suspend fun clearBaselineCheckpoint() {
        checkpoint = null
    }
}

private class RestartableBaselineSource(
    private val records: List<HealthConnectRecord>,
) : HealthConnectSource {
    override suspend fun getChangesToken(recordTypes: Set<HealthConnectRecordType>): String = "token"

    override suspend fun readStepAggregateSnapshot(): HealthConnectStepAggregateSnapshot =
        error("step aggregates not expected")

    override suspend fun scanBaseline(
        recordTypes: Set<HealthConnectRecordType>,
        progress: HealthConnectBaselineScanProgress?,
        consumePage: suspend (HealthConnectBaselinePage) -> Unit,
    ): Map<HealthConnectRecordType, HealthConnectScanBounds> {
        val pages = records.chunked(500)
        val startIndex = progress?.nextPageToken?.toInt() ?: 0
        for (index in startIndex until pages.size) {
            val nextPageToken = (index + 1).takeIf { it < pages.size }?.toString()
            consumePage(
                HealthConnectBaselinePage(
                    records = pages[index],
                    progress = HealthConnectBaselineScanProgress(
                        startTimeEpochMillis = 1,
                        endTimeEpochMillis = 2_000,
                        completedRecordTypes = if (nextPageToken == null) recordTypes else emptySet(),
                        currentRecordType = if (nextPageToken == null) null else HealthConnectRecordType.HEART_RATE,
                        nextPageToken = nextPageToken,
                    ),
                ),
            )
        }
        return recordTypes.associateWith { HealthConnectScanBounds(1, 2_000) }
    }

    override suspend fun readChanges(token: String): HealthConnectChanges =
        HealthConnectChanges(emptyList(), emptyList(), "next")
}

private class InterruptOnceBaselineHost : HealthConnectHost {
    val attemptedFirstRecordIds = mutableListOf<String>()
    private var state = HostSyncState.Initial
    private var generation = 1
    private var interrupted = false

    override suspend fun getSyncState(
        installationId: String,
        recordTypes: Set<HealthConnectRecordType>,
    ): HostSyncCursor = HostSyncCursor(state, generation, if (state == HostSyncState.Initial) null else "token")

    override suspend fun startBaseline(request: StartBaselineRequest): HostSyncCursor {
        state = HostSyncState.Baseline
        generation += 1
        return HostSyncCursor(state, generation, request.startingToken)
    }

    override suspend fun uploadBatch(request: HealthConnectBatchRequest): BatchUploadResult {
        if (request.mode == HealthConnectBatchMode.Changes) return BatchUploadResult.Accepted
        attemptedFirstRecordIds += request.records.first().metadata.id
        if (!interrupted && attemptedFirstRecordIds.size == 2) {
            interrupted = true
            throw IOException("worker stopped")
        }
        return BatchUploadResult.Accepted
    }

    override suspend fun uploadStepAggregateSnapshot(request: HealthConnectStepAggregateSnapshotRequest) = Unit

    override suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor {
        state = HostSyncState.Changes
        return HostSyncCursor(state, request.generation, request.expectedToken)
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
    private val stepAggregateSnapshot: HealthConnectStepAggregateSnapshot =
        HealthConnectStepAggregateSnapshot(0, 1, emptyList()),
) : HealthConnectSource {
    override suspend fun getChangesToken(recordTypes: Set<HealthConnectRecordType>): String {
        events += "health.getChangesToken"
        return startingToken
    }

    override suspend fun readStepAggregateSnapshot(): HealthConnectStepAggregateSnapshot {
        events += "health.readStepAggregateSnapshot"
        return stepAggregateSnapshot
    }

    override suspend fun scanBaseline(
        recordTypes: Set<HealthConnectRecordType>,
        progress: HealthConnectBaselineScanProgress?,
        consumePage: suspend (HealthConnectBaselinePage) -> Unit,
    ): Map<HealthConnectRecordType, HealthConnectScanBounds> {
        events += "health.scanBaseline(${recordTypes.joinToString(",")})"
        val pages = baselineRecords.chunked(500)
        pages.forEachIndexed { index, records ->
            consumePage(
                HealthConnectBaselinePage(
                    records = records,
                    progress = HealthConnectBaselineScanProgress(
                        startTimeEpochMillis = 1_699_990_000_000,
                        endTimeEpochMillis = 1_700_010_000_000,
                        completedRecordTypes = if (index == pages.lastIndex) recordTypes else emptySet(),
                        currentRecordType = recordTypes.firstOrNull().takeIf { index != pages.lastIndex },
                        nextPageToken = (index + 1).takeIf { it < pages.size }?.toString(),
                    ),
                ),
            )
        }
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

    override suspend fun uploadStepAggregateSnapshot(request: HealthConnectStepAggregateSnapshotRequest) = Unit

    override suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor =
        error("baseline not expected")
}

private class FakeHealthConnectHost(
    private val events: MutableList<String>,
    private val state: HostSyncState,
    private val generation: Int,
    private val token: String? = null,
) : HealthConnectHost {
    override suspend fun getSyncState(
        installationId: String,
        recordTypes: Set<HealthConnectRecordType>,
    ): HostSyncCursor {
        events += "host.getSyncState"
        return HostSyncCursor(state = state, generation = generation, token = token)
    }

    override suspend fun startBaseline(request: StartBaselineRequest): HostSyncCursor {
        events += "host.startBaseline(${request.startingToken})"
        return HostSyncCursor(state = HostSyncState.Baseline, generation = generation + 1, token = request.startingToken)
    }

    override suspend fun uploadBatch(request: HealthConnectBatchRequest): BatchUploadResult {
        events += "host.upload${request.mode.name.lowercase().replaceFirstChar { it.uppercase() }}(${request.requestId},${request.expectedToken},${request.nextToken},records=${request.records.size})"
        return BatchUploadResult.Accepted
    }

    override suspend fun uploadStepAggregateSnapshot(request: HealthConnectStepAggregateSnapshotRequest) {
        events += "host.uploadStepAggregates(${request.requestId},buckets=${request.snapshot.buckets.size})"
    }

    override suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor {
        events += "host.completeBaseline(${request.expectedToken})"
        return HostSyncCursor(state = HostSyncState.Changes, generation = request.generation, token = request.expectedToken)
    }
}

private class RecordingChangesHost(
    private val initialState: HostSyncState,
    private val token: String?,
    private val failAfterAcceptedChunks: Int = Int.MAX_VALUE,
) : HealthConnectHost {
    val batches = mutableListOf<HealthConnectBatchRequest>()
    private var accepted = 0

    /** Cursor position a real host would hold: only batches that change the token adopt it. */
    var adoptedToken: String? = token
        private set

    override suspend fun getSyncState(
        installationId: String,
        recordTypes: Set<HealthConnectRecordType>,
    ): HostSyncCursor = HostSyncCursor(state = initialState, generation = 7, token = token)

    override suspend fun startBaseline(request: StartBaselineRequest): HostSyncCursor =
        error("baseline not expected")

    override suspend fun uploadBatch(request: HealthConnectBatchRequest): BatchUploadResult {
        batches += request
        if (accepted >= failAfterAcceptedChunks) return BatchUploadResult.Conflict("boom")
        accepted += 1
        if (request.mode == HealthConnectBatchMode.Changes && request.nextToken != request.expectedToken) {
            adoptedToken = request.nextToken
        }
        return BatchUploadResult.Accepted
    }

    override suspend fun uploadStepAggregateSnapshot(request: HealthConnectStepAggregateSnapshotRequest) = Unit

    override suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor =
        error("baseline not expected")
}
