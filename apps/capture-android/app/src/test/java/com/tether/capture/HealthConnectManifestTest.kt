package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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

    @Test
    fun manifestExposesHealthConnectSettingsWithoutGenericCapture() {
        val manifest = manifestText()

        assertFalse(manifest.contains("android.permission.RECORD_AUDIO"))
        assertFalse(manifest.contains("android.intent.action.SEND"))
        assertFalse(manifest.contains(".MainActivity"))
        assertFalse(manifest.contains(".ShareActivity"))
        assertTrue(
            Regex(
                """<activity\b(?=[^>]*android:name="\.SettingsActivity")(?=[^>]*android:exported="true")[^>]*>[\s\S]*?android.intent.action.MAIN[\s\S]*?android.intent.category.LAUNCHER[\s\S]*?</activity>""",
            ).containsMatchIn(manifest),
        )
    }

    @Test
    fun projectShipsNoWearModule() {
        val settings = projectFile("settings.gradle.kts").readText()

        assertFalse(settings.contains("include(\":wear\")"))
        assertTrue(settings.contains("include(\":core\")"))
        assertTrue(settings.contains("include(\":app\")"))
    }

    private fun manifestText(): String {
        val candidates = listOf(
            File("src/main/AndroidManifest.xml"),
            File("app/src/main/AndroidManifest.xml"),
            File("apps/capture-android/app/src/main/AndroidManifest.xml"),
        )
        return candidates.first { it.exists() }.readText()
    }

    private fun projectFile(path: String): File {
        val candidates = listOf(
            File(path),
            File("../$path"),
            File("apps/capture-android/$path"),
        )
        return candidates.first { it.exists() }
    }
}
