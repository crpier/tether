package com.tether.capture

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

/**
 * The two things the client needs to talk to a host: where it lives and the
 * bearer token to authenticate with. Kept as a plain immutable value so its
 * normalization/validation is unit-testable without DataStore or Android.
 */
data class CaptureSettings(
    val hostUrl: String = "",
    val token: String = "",
) {
    /** True once both fields are non-blank — the client can attempt a capture. */
    fun isConfigured(): Boolean = hostUrl.isNotBlank() && token.isNotBlank()

    companion object {
        /** Trim user input and strip a trailing slash so URL joins stay clean. */
        fun normalize(hostUrl: String, token: String): CaptureSettings =
            CaptureSettings(hostUrl = hostUrl.trim().trimEnd('/'), token = token.trim())
    }
}

private val Context.dataStore by preferencesDataStore(name = "tether_capture_settings")

/** DataStore-backed persistence for [CaptureSettings]. */
class SettingsRepository(private val context: Context) {
    private val hostKey = stringPreferencesKey("host_url")
    private val tokenKey = stringPreferencesKey("api_token")

    suspend fun load(): CaptureSettings {
        val prefs = context.dataStore.data.map { it }.first()
        return CaptureSettings(
            hostUrl = prefs[hostKey].orEmpty(),
            token = prefs[tokenKey].orEmpty(),
        )
    }

    suspend fun save(settings: CaptureSettings) {
        context.dataStore.edit { prefs ->
            prefs[hostKey] = settings.hostUrl
            prefs[tokenKey] = settings.token
        }
    }
}
