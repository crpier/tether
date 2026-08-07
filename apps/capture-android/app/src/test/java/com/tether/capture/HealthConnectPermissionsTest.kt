package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Test

class HealthConnectPermissionsTest {
    @Test
    fun summarizesRequiredOptionalAndMissingPermissions() {
        val summary = HealthConnectPermissions.summarize(
            granted = HealthConnectPermissions.captured,
        )

        assertEquals(false, summary.canReadAllRecords)
        assertEquals(true, summary.canReadCapturedRecords)
        assertEquals(HealthConnectPermissions.required - HealthConnectPermissions.captured, summary.missingRequired)
        assertEquals(emptySet<HealthConnectRecordType>(), summary.missingCapturedRecordTypes)
        assertEquals(
            setOf(
                "android.permission.health.READ_HEALTH_DATA_HISTORY",
                "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND",
            ),
            summary.missingOptional,
        )
    }

    @Test
    fun permittedCapturedCategoriesRemainSyncableWhenAnotherCategoryIsDenied() {
        val summary = HealthConnectPermissions.summarize(
            granted = setOf("android.permission.health.READ_STEPS"),
            supportedOptional = emptySet(),
        )

        assertEquals(true, summary.canReadCapturedRecords)
        assertEquals(setOf(HealthConnectRecordType.STEPS), summary.capturedRecordTypes)
        assertEquals(
            setOf(
                HealthConnectRecordType.HEART_RATE,
                HealthConnectRecordType.SLEEP,
                HealthConnectRecordType.EXERCISE,
            ),
            summary.missingCapturedRecordTypes,
        )
    }

    @Test
    fun requestedPermissionsIncludeEveryReadableSdkRecordType() {
        assertEquals(
            HealthConnectRecordInventory.entries.map { it.readPermission }.toSet() +
                HealthConnectPermissions.optional,
            HealthConnectPermissions.requested(HealthConnectPermissions.optional),
        )
    }
}
