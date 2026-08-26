package com.tether.capture

import org.junit.Assert.assertThrows
import org.junit.Test

class HealthConnectOnlySurfaceTest {
    @Test
    fun coreExposesNoGenericCaptureClient() {
        assertThrows(ClassNotFoundException::class.java) {
            Class.forName("com.tether.capture.CaptureClient")
        }
    }
}
