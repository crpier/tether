package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

class HealthConnectManifestTest {
    @Test
    fun manifestDeclaresEveryRequestedHealthReadPermission() {
        val manifest = manifestText()
        val declared = Regex("android:name=\"(android\\.permission\\.health\\.[^\"]+)\"")
            .findAll(manifest)
            .map { it.groupValues[1] }
            .toSet()

        assertEquals(
            HealthConnectPermissions.required + HealthConnectPermissions.optional,
            declared.filter { it.startsWith("android.permission.health.") }.toSet(),
        )
    }

    private fun manifestText(): String {
        val candidates = listOf(
            File("src/main/AndroidManifest.xml"),
            File("app/src/main/AndroidManifest.xml"),
            File("apps/capture-android/app/src/main/AndroidManifest.xml"),
        )
        return candidates.first { it.exists() }.readText()
    }
}
