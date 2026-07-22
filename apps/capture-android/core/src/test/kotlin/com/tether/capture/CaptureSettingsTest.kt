package com.tether.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureSettingsTest {
    @Test
    fun normalizeTrimsAndStripsTrailingSlash() {
        val settings = CaptureSettings.normalize("  https://host.example/  ", "  tok  ")
        assertEquals("https://host.example", settings.hostUrl)
        assertEquals("tok", settings.token)
    }

    @Test
    fun normalizeStripsMultipleTrailingSlashes() {
        val settings = CaptureSettings.normalize("https://host.example///", "tok")
        assertEquals("https://host.example", settings.hostUrl)
    }

    @Test
    fun isConfiguredRequiresBothFields() {
        assertTrue(CaptureSettings("https://h", "t").isConfigured())
        assertFalse(CaptureSettings("", "t").isConfigured())
        assertFalse(CaptureSettings("https://h", "").isConfigured())
        assertFalse(CaptureSettings("   ", "t").isConfigured())
    }
}
