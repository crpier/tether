package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Duration

class HealthConnectWorkSchedulerTest {
    @Test
    fun scheduleUsesUniqueSixHourlyNetworkWorkWithoutCharging() {
        val gateway = FakeWorkGateway()
        val scheduler = HealthConnectWorkScheduler(gateway)

        scheduler.ensurePeriodicSync()
        scheduler.syncNow()

        assertEquals(
            listOf(
                HealthConnectWorkSpec(
                    uniqueName = "health-connect-periodic",
                    interval = Duration.ofHours(6),
                    requiresNetwork = true,
                    requiresCharging = false,
                    replaceExisting = false,
                ),
                HealthConnectWorkSpec(
                    uniqueName = "health-connect-sync",
                    interval = null,
                    requiresNetwork = true,
                    requiresCharging = false,
                    replaceExisting = false,
                ),
            ),
            gateway.enqueued,
        )
    }

    private class FakeWorkGateway : HealthConnectWorkGateway {
        val enqueued = mutableListOf<HealthConnectWorkSpec>()

        override fun enqueue(spec: HealthConnectWorkSpec) {
            enqueued += spec
        }
    }
}
