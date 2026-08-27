package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HealthConnectSyncStatusTest {
    @Test
    fun activeRetryHidesFailureFromPreviousAttempt() {
        val running = HealthConnectSyncStatus(
            installationId = "installation",
            running = true,
            lastSuccessEpochMillis = 1_000,
            lastFailure = "Host unavailable",
        )
        val idle = running.copy(running = false)

        assertNull(running.failureForDisplay)
        assertEquals("Host unavailable", idle.failureForDisplay)
    }
}
