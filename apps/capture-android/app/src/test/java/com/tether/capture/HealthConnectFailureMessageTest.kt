package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Test

class HealthConnectFailureMessageTest {
    @Test
    fun convertsFailuresToConciseSafeActionsWithoutPayloadOrTokens() {
        assertEquals(
            "Host unavailable; check connection and Tether settings",
            HealthConnectFailureMessage.from(java.io.IOException("opaque-token 61 bpm secret note")),
        )
        assertEquals(
            "Health permissions changed; grant access again",
            HealthConnectFailureMessage.from(SecurityException("raw health payload")),
        )
        assertEquals(
            "Sync failed; try again",
            HealthConnectFailureMessage.from(IllegalStateException("opaque-token 61 bpm secret note")),
        )
    }
}
