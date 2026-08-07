package com.tether.capture

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException
import java.nio.charset.StandardCharsets
import java.util.UUID

class HealthConnectSyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result = syncMutex.withLock {
        val repository = SettingsRepository(applicationContext)
        val settings = repository.load()
        if (!settings.isConfigured()) {
            repository.markHealthConnectFailure("Configure the Tether host before syncing")
            return@withLock Result.failure()
        }

        val healthStatus = HealthConnectStatusReader(
            AndroidHealthConnectEnvironment(applicationContext),
        ).read()
        val available = healthStatus as? HealthConnectStatus.Available
        if (available == null) {
            repository.markHealthConnectFailure(
                if (healthStatus == HealthConnectStatus.ProviderUpdateRequired) {
                    "Install or update Health Connect"
                } else {
                    "Health Connect is unavailable on this device"
                },
            )
            return@withLock Result.failure()
        }
        if (!available.permissions.canReadCapturedRecords) {
            repository.markHealthConnectFailure("Health permissions changed; grant access again")
            return@withLock Result.failure()
        }

        repository.markHealthConnectRunning(true)
        try {
            val installationId = repository.healthConnectStatus().installationId
            val source = AndroidHealthConnectSource.fromContext(
                context = applicationContext,
                hasHistoryPermission = HealthConnectPermissions.READ_HEALTH_DATA_HISTORY !in
                    available.permissions.missingOptional,
            )
            val coordinator = HealthConnectSyncCoordinator(
                installationId = installationId,
                recordTypes = available.permissions.capturedRecordTypes,
                health = source,
                host = HealthConnectHostHttpClient(settings.hostUrl, settings.token),
                requestIds = UuidRequestIds,
            )
            when (val result = coordinator.syncOnce()) {
                HealthConnectSyncResult.Success -> {
                    repository.markHealthConnectSuccess(System.currentTimeMillis())
                    Result.success()
                }
                is HealthConnectSyncResult.Failed -> {
                    repository.markHealthConnectFailure("Sync conflict; try again")
                    Result.retry()
                }
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            repository.markHealthConnectFailure(HealthConnectFailureMessage.from(error))
            when (error) {
                is IOException -> Result.retry()
                is SecurityException -> Result.failure()
                else -> Result.retry()
            }
        } finally {
            repository.markHealthConnectRunning(false)
        }
    }

    private object UuidRequestIds : RequestIds {
        override fun next(): String = UUID.randomUUID().toString()

        override fun stable(key: String): String = UUID.nameUUIDFromBytes(
            key.toByteArray(StandardCharsets.UTF_8),
        ).toString()
    }

    companion object {
        private val syncMutex = Mutex()
        val ALL_RECORD_TYPES: Set<HealthConnectRecordType> = linkedSetOf(
            HealthConnectRecordType.HEART_RATE,
            HealthConnectRecordType.SLEEP,
            HealthConnectRecordType.STEPS,
            HealthConnectRecordType.EXERCISE,
        )
    }
}
