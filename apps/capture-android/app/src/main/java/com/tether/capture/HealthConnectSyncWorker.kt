package com.tether.capture

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.CancellationException
import java.io.IOException
import java.nio.charset.StandardCharsets
import java.util.UUID

class HealthConnectSyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result = when (val run = syncGuard.runIfIdle { sync() }) {
        HealthConnectSyncRun.AlreadyRunning -> Result.success()
        is HealthConnectSyncRun.Completed -> run.value
    }

    private suspend fun sync(): Result {
        val repository = SettingsRepository(applicationContext)
        val settings = repository.load()
        if (!settings.isConfigured()) {
            repository.markHealthConnectFailure("Configure the Tether host before syncing")
            return Result.failure()
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
            return Result.failure()
        }
        if (!available.permissions.canReadAnyRecord) {
            repository.markHealthConnectFailure("Health permissions changed; grant access again")
            return Result.failure()
        }

        repository.markHealthConnectRunning(true)
        return try {
            val installationId = repository.healthConnectStatus().installationId
            val source = AndroidHealthConnectSource.fromContext(
                context = applicationContext,
                hasHistoryPermission = HealthConnectPermissions.READ_HEALTH_DATA_HISTORY !in
                    available.permissions.missingOptional,
            )
            val coordinator = HealthConnectSyncCoordinator(
                installationId = installationId,
                recordTypes = available.permissions.grantedRecordTypes,
                health = source,
                host = HealthConnectHostHttpClient(settings.hostUrl, settings.token),
                requestIds = UuidRequestIds,
                baselineCheckpoints = repository,
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
        private val syncGuard = HealthConnectSyncGuard()
        val ALL_RECORD_TYPES: Set<HealthConnectRecordType> = HealthConnectRecordType.entries.toCollection(linkedSetOf())
    }
}
