package com.tether.capture.wear

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.tether.capture.CaptureSettings
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "tether_capture_settings")

/**
 * DataStore-backed persistence for [CaptureSettings], scoped to this watch.
 * The watch authenticates independently of any paired phone, so it keeps its
 * own copy rather than syncing from the phone app (mirrors app/Settings.kt;
 * DataStore is Android-only so this repository isn't shared via `core`).
 */
class WearSettingsRepository(private val context: Context) {
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
