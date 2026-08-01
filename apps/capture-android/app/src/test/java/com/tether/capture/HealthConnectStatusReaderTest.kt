package com.tether.capture

import androidx.health.connect.client.HealthConnectClient
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

class HealthConnectStatusReaderTest {
    @Test
    fun unsupportedDevicesDoNotRequirePermissions() = runTest {
        val reader = HealthConnectStatusReader(
            environment = FakeHealthConnectEnvironment(
                sdkStatus = HealthConnectClient.SDK_UNAVAILABLE,
                granted = emptySet(),
            ),
        )

        assertEquals(
            HealthConnectStatus.Unsupported,
            reader.read(),
        )
    }

    @Test
    fun availableDeviceReportsGrantedAndMissingCapabilities() = runTest {
        val reader = HealthConnectStatusReader(
            environment = FakeHealthConnectEnvironment(
                sdkStatus = HealthConnectClient.SDK_AVAILABLE,
                granted = HealthConnectPermissions.required,
            ),
        )

        assertEquals(
            HealthConnectStatus.Available(
                permissions = HealthConnectPermissionSummary(
                    missingRequired = emptySet(),
                    missingOptional = HealthConnectPermissions.optional,
                ),
            ),
            reader.read(),
        )
    }

    private class FakeHealthConnectEnvironment(
        private val sdkStatus: Int,
        private val granted: Set<String>,
    ) : HealthConnectEnvironment {
        override fun sdkStatus(): Int = sdkStatus

        override fun supportedOptionalPermissions(): Set<String> = HealthConnectPermissions.optional

        override suspend fun grantedPermissions(): Set<String> = granted
    }
}
