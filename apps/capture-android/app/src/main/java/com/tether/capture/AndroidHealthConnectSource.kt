package com.tether.capture

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.changes.Change
import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.records.ExerciseSessionRecord as AndroidExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord as AndroidHeartRateRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.SleepSessionRecord as AndroidSleepSessionRecord
import androidx.health.connect.client.records.StepsRecord as AndroidStepsRecord
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import java.time.Instant
import kotlin.reflect.KClass

private const val HEALTH_CONNECT_PAGE_SIZE = 500

data class AndroidHealthConnectReadPage(
    val records: List<Record>,
    val nextPageToken: String?,
)

data class AndroidHealthConnectChangePage(
    val changes: List<Change>,
    val nextToken: String,
    val hasMore: Boolean,
    val tokenExpired: Boolean,
)

interface HealthConnectGateway {
    suspend fun getChangesToken(recordTypes: Set<KClass<out Record>>): String

    suspend fun readRecords(
        recordType: KClass<out Record>,
        startTime: Instant,
        endTime: Instant,
        pageToken: String?,
    ): AndroidHealthConnectReadPage

    suspend fun getChanges(token: String): AndroidHealthConnectChangePage
}

class AndroidHealthConnectClientGateway(
    private val client: HealthConnectClient,
) : HealthConnectGateway {
    override suspend fun getChangesToken(recordTypes: Set<KClass<out Record>>): String =
        client.getChangesToken(ChangesTokenRequest(recordTypes = recordTypes))

    override suspend fun readRecords(
        recordType: KClass<out Record>,
        startTime: Instant,
        endTime: Instant,
        pageToken: String?,
    ): AndroidHealthConnectReadPage {
        val response = client.readRecords(
            ReadRecordsRequest(
                recordType = recordType,
                timeRangeFilter = TimeRangeFilter.between(startTime, endTime),
                ascendingOrder = true,
                pageSize = HEALTH_CONNECT_PAGE_SIZE,
                pageToken = pageToken,
            ),
        )
        return AndroidHealthConnectReadPage(
            records = response.records,
            nextPageToken = response.pageToken,
        )
    }

    override suspend fun getChanges(token: String): AndroidHealthConnectChangePage {
        val response = client.getChanges(token)
        return AndroidHealthConnectChangePage(
            changes = response.changes,
            nextToken = response.nextChangesToken,
            hasMore = response.hasMore,
            tokenExpired = response.changesTokenExpired,
        )
    }
}

class AndroidHealthConnectSource(
    private val gateway: HealthConnectGateway,
    private val clock: () -> Instant = { Instant.now() },
    private val baselineStart: (Instant) -> Instant = { Instant.EPOCH },
    private val recordTypeIndex: HealthConnectRecordTypeIndex = InMemoryHealthConnectRecordTypeIndex(),
) : HealthConnectSource {
    override suspend fun getChangesToken(recordTypes: Set<HealthConnectRecordType>): String =
        gateway.getChangesToken(recordTypes.toAndroidRecordClasses())

    override suspend fun scanBaseline(
        recordTypes: Set<HealthConnectRecordType>,
        consumePage: suspend (List<HealthConnectRecord>) -> Unit,
    ): Map<HealthConnectRecordType, HealthConnectScanBounds> {
        val end = clock()
        val start = baselineStart(end)
        val bounds = mutableMapOf<HealthConnectRecordType, HealthConnectScanBounds>()
        for (recordType in recordTypes) {
            var pageToken: String? = null
            do {
                val page = gateway.readRecords(recordType.toAndroidRecordClass(), start, end, pageToken)
                val mapped = page.records.mapNotNull { it.toWireRecordOrNull() }
                recordTypeIndex.remember(mapped.map { it.recordType to it.metadata.id })
                if (mapped.isNotEmpty()) {
                    consumePage(mapped)
                }
                pageToken = page.nextPageToken
            } while (pageToken != null)
            bounds[recordType] = HealthConnectScanBounds(
                startTimeEpochMillis = start.toEpochMilli(),
                endTimeEpochMillis = end.toEpochMilli(),
            )
        }
        return bounds
    }

    override suspend fun readChanges(token: String): HealthConnectChanges {
        val page = gateway.getChanges(token)
        if (page.tokenExpired) {
            throw HealthConnectChangesTokenExpiredException()
        }
        val records = mutableListOf<HealthConnectRecord>()
        val deletions = mutableListOf<HealthConnectDeletion>()
        page.changes.forEach { change ->
            when (change) {
                is UpsertionChange -> change.record.toWireRecordOrNull()?.let { record ->
                    records += record
                    recordTypeIndex.remember(listOf(record.recordType to record.metadata.id))
                }
                is DeletionChange -> recordTypeIndex.find(change.recordId)?.let { type ->
                    // Keep the index entry until a later upsert replaces it: a failed upload
                    // must be able to classify the same deletion again on retry.
                    deletions += HealthConnectDeletion(recordType = type, recordId = change.recordId)
                }
            }
        }
        return HealthConnectChanges(
            records = records,
            deletions = deletions,
            nextToken = page.nextToken,
            hasMore = page.hasMore,
        )
    }

    private fun Set<HealthConnectRecordType>.toAndroidRecordClasses(): Set<KClass<out Record>> =
        map { it.toAndroidRecordClass() }.toSet()

    private fun HealthConnectRecordType.toAndroidRecordClass(): KClass<out Record> = when (this) {
        HealthConnectRecordType.HEART_RATE -> AndroidHeartRateRecord::class
        HealthConnectRecordType.SLEEP -> AndroidSleepSessionRecord::class
        HealthConnectRecordType.STEPS -> AndroidStepsRecord::class
        HealthConnectRecordType.EXERCISE -> AndroidExerciseSessionRecord::class
    }

    private fun Record.toWireRecordOrNull(): HealthConnectRecord? = when (this) {
        is AndroidHeartRateRecord -> HealthConnectRecordMapper.map(this)
        is AndroidSleepSessionRecord -> HealthConnectRecordMapper.map(this)
        is AndroidStepsRecord -> HealthConnectRecordMapper.map(this)
        is AndroidExerciseSessionRecord -> HealthConnectRecordMapper.map(this)
        else -> null
    }

    companion object {
        fun fromContext(
            context: Context,
            hasHistoryPermission: Boolean,
        ): AndroidHealthConnectSource = AndroidHealthConnectSource(
            gateway = AndroidHealthConnectClientGateway(HealthConnectClient.getOrCreate(context)),
            baselineStart = { end ->
                if (hasHistoryPermission) Instant.EPOCH else end.minusSeconds(30L * 24 * 60 * 60)
            },
            recordTypeIndex = SqliteHealthConnectRecordTypeIndex(context),
        )
    }
}
