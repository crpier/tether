package com.tether.capture

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.take
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class SettingsRepositoryTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    @OptIn(ExperimentalCoroutinesApi::class)
    @Test
    fun syncStatusUpdatesWhenAnActiveRetryStarts() = runTest {
        val file = temporaryFolder.newFile("status.preferences_pb").apply { delete() }
        val repository = SettingsRepository(
            PreferenceDataStoreFactory.create(produceFile = { file }),
        )
        repository.healthConnectStatus()
        repository.markHealthConnectFailure("Host unavailable")
        val statuses = backgroundScope.async(UnconfinedTestDispatcher(testScheduler)) {
            repository.healthConnectStatusUpdates().take(2).toList()
        }

        repository.markHealthConnectRunning(true)

        val updates = statuses.await()
        assertEquals("Host unavailable", updates.first().failureForDisplay)
        assertNull(updates.last().failureForDisplay)
        assertEquals(true, updates.last().running)
    }

    @Test
    fun baselineCheckpointSurvivesRepositoryRecreationUntilCleared() = runTest {
        val file = temporaryFolder.newFile("checkpoint.preferences_pb").apply { delete() }
        val dataStore = PreferenceDataStoreFactory.create(produceFile = { file })
        val checkpoint = HealthConnectBaselineCheckpoint(
            generation = 4,
            startingToken = "starting-token",
            recordTypes = setOf(HealthConnectRecordType.HEART_RATE, HealthConnectRecordType.STEPS),
            scanProgress = HealthConnectBaselineScanProgress(
                startTimeEpochMillis = 100,
                endTimeEpochMillis = 2_000,
                completedRecordTypes = setOf(HealthConnectRecordType.HEART_RATE),
                currentRecordType = HealthConnectRecordType.STEPS,
                nextPageToken = "page-2",
            ),
        )

        SettingsRepository(dataStore).saveBaselineCheckpoint(checkpoint)
        val recreated = SettingsRepository(dataStore)

        assertEquals(checkpoint, recreated.loadBaselineCheckpoint())
        recreated.clearBaselineCheckpoint()
        assertNull(recreated.loadBaselineCheckpoint())
    }
}
