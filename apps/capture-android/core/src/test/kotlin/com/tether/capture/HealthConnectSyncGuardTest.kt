package com.tether.capture

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class HealthConnectSyncGuardTest {
    @Test
    fun concurrentRunExitsWithoutWaitingForActiveSync() = runTest {
        val guard = HealthConnectSyncGuard()
        val activeStarted = CompletableDeferred<Unit>()
        val releaseActive = CompletableDeferred<Unit>()
        var concurrentActionRan = false
        val active = async {
            guard.runIfIdle {
                activeStarted.complete(Unit)
                releaseActive.await()
                "finished"
            }
        }
        activeStarted.await()

        val concurrent = guard.runIfIdle {
            concurrentActionRan = true
            "unexpected"
        }

        assertEquals(HealthConnectSyncRun.AlreadyRunning, concurrent)
        assertFalse(concurrentActionRan)
        releaseActive.complete(Unit)
        assertEquals(HealthConnectSyncRun.Completed("finished"), active.await())
    }
}
