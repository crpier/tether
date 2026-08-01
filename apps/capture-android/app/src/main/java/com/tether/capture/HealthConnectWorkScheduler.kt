package com.tether.capture

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.time.Duration
import java.util.concurrent.TimeUnit

data class HealthConnectWorkSpec(
    val uniqueName: String,
    val interval: Duration?,
    val requiresNetwork: Boolean,
    val requiresCharging: Boolean,
    val replaceExisting: Boolean,
)

interface HealthConnectWorkGateway {
    fun enqueue(spec: HealthConnectWorkSpec)
}

class HealthConnectWorkScheduler(
    private val gateway: HealthConnectWorkGateway,
) {
    fun ensurePeriodicSync() {
        gateway.enqueue(
            HealthConnectWorkSpec(
                uniqueName = PERIODIC_WORK_NAME,
                interval = Duration.ofHours(6),
                requiresNetwork = true,
                requiresCharging = false,
                replaceExisting = false,
            ),
        )
    }

    fun syncNow() {
        gateway.enqueue(
            HealthConnectWorkSpec(
                uniqueName = IMMEDIATE_WORK_NAME,
                interval = null,
                requiresNetwork = true,
                requiresCharging = false,
                replaceExisting = true,
            ),
        )
    }

    companion object {
        const val PERIODIC_WORK_NAME = "health-connect-periodic"
        const val IMMEDIATE_WORK_NAME = "health-connect-sync"

        fun fromContext(context: Context): HealthConnectWorkScheduler = HealthConnectWorkScheduler(
            WorkManagerHealthConnectGateway(WorkManager.getInstance(context)),
        )
    }
}

class WorkManagerHealthConnectGateway(
    private val workManager: WorkManager,
) : HealthConnectWorkGateway {
    override fun enqueue(spec: HealthConnectWorkSpec) {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(if (spec.requiresNetwork) NetworkType.CONNECTED else NetworkType.NOT_REQUIRED)
            .setRequiresCharging(spec.requiresCharging)
            .build()
        if (spec.interval == null) {
            val request = OneTimeWorkRequestBuilder<HealthConnectSyncWorker>()
                .setConstraints(constraints)
                .build()
            workManager.enqueueUniqueWork(
                spec.uniqueName,
                if (spec.replaceExisting) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP,
                request,
            )
        } else {
            val request = PeriodicWorkRequestBuilder<HealthConnectSyncWorker>(
                spec.interval.toHours(),
                TimeUnit.HOURS,
            )
                .setConstraints(constraints)
                .build()
            workManager.enqueueUniquePeriodicWork(
                spec.uniqueName,
                if (spec.replaceExisting) ExistingPeriodicWorkPolicy.UPDATE else ExistingPeriodicWorkPolicy.KEEP,
                request,
            )
        }
    }
}
