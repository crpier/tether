package com.tether.capture

import kotlinx.coroutines.sync.Mutex

sealed class HealthConnectSyncRun<out T> {
    data class Completed<T>(val value: T) : HealthConnectSyncRun<T>()

    data object AlreadyRunning : HealthConnectSyncRun<Nothing>()
}

class HealthConnectSyncGuard {
    private val mutex = Mutex()

    suspend fun <T> runIfIdle(action: suspend () -> T): HealthConnectSyncRun<T> {
        if (!mutex.tryLock()) return HealthConnectSyncRun.AlreadyRunning
        return try {
            HealthConnectSyncRun.Completed(action())
        } finally {
            mutex.unlock()
        }
    }
}
