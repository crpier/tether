package com.tether.capture

import java.io.IOException

object HealthConnectFailureMessage {
    fun from(error: Throwable): String = when (error) {
        is SecurityException -> "Health permissions changed; grant access again"
        is IOException -> "Host unavailable; check connection and Tether settings"
        is HealthConnectChangesTokenExpiredException -> "Health history changed; a fresh sync is required"
        else -> "Sync failed; try again"
    }
}
