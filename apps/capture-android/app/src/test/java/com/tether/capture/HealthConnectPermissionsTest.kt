package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Test

class HealthConnectPermissionsTest {
    @Test
    fun summarizesRequiredOptionalAndMissingPermissions() {
        val summary = HealthConnectPermissions.summarize(
            granted = setOf(
                "android.permission.health.READ_HEART_RATE",
                "android.permission.health.READ_SLEEP",
                "android.permission.health.READ_STEPS",
                "android.permission.health.READ_EXERCISE",
            ),
        )

        assertEquals(true, summary.canReadAllRecords)
        assertEquals(emptySet<String>(), summary.missingRequired)
        assertEquals(
            setOf(
                "android.permission.health.READ_HEALTH_DATA_HISTORY",
                "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND",
            ),
            summary.missingOptional,
        )
    }
}
