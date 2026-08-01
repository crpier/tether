package com.tether.capture

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.util.UUID

private val Context.dataStore by preferencesDataStore(name = "tether_capture_settings")

/** DataStore-backed persistence for [CaptureSettings]. */
data class HealthConnectSyncStatus(
    val installationId: String,
    val running: Boolean,
    val lastSuccessEpochMillis: Long?,
    val lastFailure: String?,
)

class SettingsRepository(private val context: Context) {
    private val hostKey = stringPreferencesKey("host_url")
    private val tokenKey = stringPreferencesKey("api_token")
    private val installationIdKey = stringPreferencesKey("health_connect_installation_id")
    private val syncRunningKey = booleanPreferencesKey("health_connect_sync_running")
    private val lastSuccessKey = longPreferencesKey("health_connect_last_success")
    private val lastFailureKey = stringPreferencesKey("health_connect_last_failure")

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

    suspend fun healthConnectStatus(): HealthConnectSyncStatus {
        val prefs = context.dataStore.data.first()
        val installationId = prefs[installationIdKey] ?: createInstallationId()
        return HealthConnectSyncStatus(
            installationId = installationId,
            running = prefs[syncRunningKey] ?: false,
            lastSuccessEpochMillis = prefs[lastSuccessKey],
            lastFailure = prefs[lastFailureKey],
        )
    }

    suspend fun markHealthConnectRunning(running: Boolean) {
        context.dataStore.edit { it[syncRunningKey] = running }
    }

    suspend fun markHealthConnectSuccess(atEpochMillis: Long) {
        context.dataStore.edit { prefs ->
            prefs[syncRunningKey] = false
            prefs[lastSuccessKey] = atEpochMillis
            prefs.remove(lastFailureKey)
        }
    }

    suspend fun markHealthConnectFailure(message: String) {
        context.dataStore.edit { prefs ->
            prefs[syncRunningKey] = false
            prefs[lastFailureKey] = message
        }
    }

    private suspend fun createInstallationId(): String {
        val generated = UUID.randomUUID().toString()
        var result = generated
        context.dataStore.edit { prefs ->
            result = prefs[installationIdKey] ?: generated
            prefs[installationIdKey] = result
        }
        return result
    }
}
