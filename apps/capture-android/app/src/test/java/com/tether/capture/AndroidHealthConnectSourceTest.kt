package com.tether.capture

import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.units.Mass
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant
import java.time.ZoneOffset
import kotlin.reflect.KClass

class AndroidHealthConnectSourceTest {
    @Test
    fun deletionUsesPersistedUpstreamRecordTypeWithoutFabricatingOtherTombstones() = runTest {
        val index = InMemoryHealthConnectRecordTypeIndex().apply {
            remember(listOf(HealthConnectRecordType.STEPS to "deleted-steps"))
        }
        val gateway = FakeHealthConnectGateway(
            changePages = ArrayDeque(
                listOf(
                    AndroidHealthConnectChangePage(
                        changes = listOf(DeletionChange("deleted-steps"), DeletionChange("unseen")),
                        nextToken = "next",
                        hasMore = false,
                        tokenExpired = false,
                    ),
                ),
            ),
        )
        val source = AndroidHealthConnectSource(gateway = gateway, recordTypeIndex = index)

        val changes = source.readChanges("token")

        assertEquals(
            listOf(HealthConnectDeletion(HealthConnectRecordType.STEPS, "deleted-steps")),
            changes.deletions,
        )
        assertEquals(HealthConnectRecordType.STEPS, index.find("deleted-steps"))
    }

    @Test
    fun baselinePagesEachTypeAndReportsSeenIdsAndBounds() = runTest {
        val gateway = FakeHealthConnectGateway(
            baselinePages = mutableMapOf(
                StepsRecord::class to ArrayDeque(
                    listOf(
                        AndroidHealthConnectReadPage(
                            records = listOf(stepsRecord("steps-1")),
                            nextPageToken = "page-2",
                        ),
                        AndroidHealthConnectReadPage(
                            records = listOf(stepsRecord("steps-2")),
                            nextPageToken = null,
                        ),
                    ),
                ),
            ),
        )
        val source = AndroidHealthConnectSource(
            gateway = gateway,
            clock = { Instant.ofEpochMilli(2_000) },
        )

        val consumedIds = mutableListOf<String>()
        val bounds = source.scanBaseline(setOf(HealthConnectRecordType.STEPS)) { records ->
            consumedIds += records.map { it.metadata.id }
            gateway.events += "consume(${records.joinToString(",") { it.metadata.id }})"
        }

        assertEquals(
            listOf(
                "read(StepsRecord,null)",
                "consume(steps-1)",
                "read(StepsRecord,page-2)",
                "consume(steps-2)",
            ),
            gateway.events,
        )
        assertEquals(listOf("steps-1", "steps-2"), consumedIds)
        assertEquals(
            HealthConnectScanBounds(
                startTimeEpochMillis = 0,
                endTimeEpochMillis = 2_000,
            ),
            bounds[HealthConnectRecordType.STEPS],
        )
    }

    @Test
    fun baselineMapsPlannedRecordsAsGenericPayloads() = runTest {
        val gateway = FakeHealthConnectGateway(
            baselinePages = mutableMapOf(
                WeightRecord::class to ArrayDeque(
                    listOf(
                        AndroidHealthConnectReadPage(
                            records = listOf(weightRecord("weight-1")),
                            nextPageToken = null,
                        ),
                    ),
                ),
            ),
        )
        val source = AndroidHealthConnectSource(
            gateway = gateway,
            clock = { Instant.ofEpochMilli(2_000) },
        )

        val consumed = mutableListOf<HealthConnectRecord>()
        source.scanBaseline(setOf(HealthConnectRecordType.WEIGHT)) { records -> consumed += records }

        val record = consumed.single() as HealthConnectRecord.Generic
        assertEquals(HealthConnectRecordType.WEIGHT, record.recordType)
        assertEquals("weight-1", record.metadata.id)
        assertEquals(1_000L, record.startTimeEpochMillis)
        assertEquals(null, record.endTimeEpochMillis)
        assertEquals(70.5, (record.payload.getValue("weight") as Map<*, *>)["kilograms"])
        assertEquals(listOf("read(WeightRecord,null)"), gateway.events)
    }

    @Test
    fun stepSnapshotUsesHealthConnectsPriorityAwareHourlyAggregate() = runTest {
        val end = Instant.parse("2026-08-27T18:33:05Z")
        val bucket = AndroidHealthConnectStepAggregateBucket(
            startTime = Instant.parse("2026-08-27T17:00:00Z"),
            endTime = Instant.parse("2026-08-27T18:00:00Z"),
            zoneOffset = ZoneOffset.ofHours(3),
            count = 321,
        )
        val gateway = FakeHealthConnectGateway(stepAggregateBuckets = listOf(bucket))
        val source = AndroidHealthConnectSource(gateway = gateway, clock = { end })

        val snapshot = source.readStepAggregateSnapshot()

        assertEquals(
            HealthConnectStepAggregateSnapshot(
                startTimeEpochMillis = Instant.parse("2026-05-29T18:00:00Z").toEpochMilli(),
                endTimeEpochMillis = end.toEpochMilli(),
                buckets = listOf(
                    HealthConnectStepAggregateBucket(
                        startTimeEpochMillis = bucket.startTime.toEpochMilli(),
                        endTimeEpochMillis = bucket.endTime.toEpochMilli(),
                        zoneOffsetSeconds = 10_800,
                        count = 321,
                    ),
                ),
            ),
            snapshot,
        )
        assertEquals(
            listOf("aggregateSteps(2026-05-29T18:00:00Z,2026-08-27T18:33:05Z)"),
            gateway.events,
        )
    }

    @Test
    fun changesReturnOneDurablePageAndContinuationToken() = runTest {
        val gateway = FakeHealthConnectGateway(
            changePages = ArrayDeque(
                listOf(
                    AndroidHealthConnectChangePage(
                        changes = listOf(UpsertionChange(stepsRecord("steps-1"))),
                        nextToken = "token-2",
                        hasMore = true,
                        tokenExpired = false,
                    ),
                    AndroidHealthConnectChangePage(
                        changes = listOf(UpsertionChange(stepsRecord("steps-2"))),
                        nextToken = "token-3",
                        hasMore = false,
                        tokenExpired = false,
                    ),
                ),
            ),
        )
        val source = AndroidHealthConnectSource(gateway = gateway)

        val changes = source.readChanges("token-1")

        assertEquals(listOf("changes(token-1)"), gateway.events)
        assertEquals(listOf("steps-1"), changes.records.map { it.metadata.id })
        assertEquals("token-2", changes.nextToken)
        assertEquals(true, changes.hasMore)
    }

    private class FakeHealthConnectGateway(
        private val baselinePages: MutableMap<KClass<out androidx.health.connect.client.records.Record>, ArrayDeque<AndroidHealthConnectReadPage>> = mutableMapOf(),
        private val changePages: ArrayDeque<AndroidHealthConnectChangePage> = ArrayDeque(),
        private val stepAggregateBuckets: List<AndroidHealthConnectStepAggregateBucket> = emptyList(),
    ) : HealthConnectGateway {
        val events = mutableListOf<String>()

        override suspend fun aggregateSteps(
            startTime: Instant,
            endTime: Instant,
        ): List<AndroidHealthConnectStepAggregateBucket> {
            events += "aggregateSteps($startTime,$endTime)"
            return stepAggregateBuckets
        }

        override suspend fun getChangesToken(recordTypes: Set<KClass<out androidx.health.connect.client.records.Record>>): String = "token"

        override suspend fun readRecords(
            recordType: KClass<out androidx.health.connect.client.records.Record>,
            startTime: Instant,
            endTime: Instant,
            pageToken: String?,
        ): AndroidHealthConnectReadPage {
            events += "read(${recordType.simpleName},$pageToken)"
            return baselinePages.getValue(recordType).removeFirst()
        }

        override suspend fun getChanges(token: String): AndroidHealthConnectChangePage {
            events += "changes($token)"
            return changePages.removeFirst()
        }
    }
}

private fun stepsRecord(id: String): StepsRecord = StepsRecord(
    Instant.ofEpochMilli(1_000),
    ZoneOffset.UTC,
    Instant.ofEpochMilli(1_500),
    ZoneOffset.UTC,
    10,
    Metadata.unknownRecordingMethodWithId(id),
)

private fun weightRecord(id: String): WeightRecord = WeightRecord(
    Instant.ofEpochMilli(1_000),
    ZoneOffset.UTC,
    Mass.kilograms(70.5),
    Metadata.unknownRecordingMethodWithId(id),
)
