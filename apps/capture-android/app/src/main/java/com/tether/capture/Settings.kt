package com.tether.capture

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.distinctUntilChanged
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
) {
    val failureForDisplay: String?
        get() = lastFailure.takeUnless { running }
}

class SettingsRepository internal constructor(
    private val dataStore: DataStore<Preferences>,
) : HealthConnectBaselineCheckpointStore {
    constructor(context: Context) : this(context.dataStore)
    private val hostKey = stringPreferencesKey("host_url")
    private val tokenKey = stringPreferencesKey("api_token")
    private val installationIdKey = stringPreferencesKey("health_connect_installation_id")
    private val syncRunningKey = booleanPreferencesKey("health_connect_sync_running")
    private val lastSuccessKey = longPreferencesKey("health_connect_last_success")
    private val lastFailureKey = stringPreferencesKey("health_connect_last_failure")
    private val baselineGenerationKey = intPreferencesKey("health_connect_baseline_generation")
    private val baselineStartingTokenKey = stringPreferencesKey("health_connect_baseline_starting_token")
    private val baselineRecordTypesKey = stringPreferencesKey("health_connect_baseline_record_types")
    private val baselineStartTimeKey = longPreferencesKey("health_connect_baseline_start_time")
    private val baselineEndTimeKey = longPreferencesKey("health_connect_baseline_end_time")
    private val baselineCompletedTypesKey = stringPreferencesKey("health_connect_baseline_completed_types")
    private val baselineCurrentTypeKey = stringPreferencesKey("health_connect_baseline_current_type")
    private val baselineNextPageTokenKey = stringPreferencesKey("health_connect_baseline_next_page_token")

    suspend fun load(): CaptureSettings {
        val prefs = dataStore.data.map { it }.first()
        return CaptureSettings(
            hostUrl = prefs[hostKey].orEmpty(),
            token = prefs[tokenKey].orEmpty(),
        )
    }

    suspend fun save(settings: CaptureSettings) {
        dataStore.edit { prefs ->
            prefs[hostKey] = settings.hostUrl
            prefs[tokenKey] = settings.token
        }
    }

    suspend fun healthConnectStatus(): HealthConnectSyncStatus =
        healthConnectStatusUpdates().first()

    fun healthConnectStatusUpdates(): Flow<HealthConnectSyncStatus> = dataStore.data.map { prefs ->
        HealthConnectSyncStatus(
            installationId = prefs[installationIdKey] ?: createInstallationId(),
            running = prefs[syncRunningKey] ?: false,
            lastSuccessEpochMillis = prefs[lastSuccessKey],
            lastFailure = prefs[lastFailureKey],
        )
    }.distinctUntilChanged()

    suspend fun markHealthConnectRunning(running: Boolean) {
        dataStore.edit { it[syncRunningKey] = running }
    }

    suspend fun markHealthConnectSuccess(atEpochMillis: Long) {
        dataStore.edit { prefs ->
            prefs[syncRunningKey] = false
            prefs[lastSuccessKey] = atEpochMillis
            prefs.remove(lastFailureKey)
        }
    }

    suspend fun markHealthConnectFailure(message: String) {
        dataStore.edit { prefs ->
            prefs[syncRunningKey] = false
            prefs[lastFailureKey] = message
        }
    }

    override suspend fun loadBaselineCheckpoint(): HealthConnectBaselineCheckpoint? {
        val prefs = dataStore.data.first()
        val generation = prefs[baselineGenerationKey] ?: return null
        val startingToken = prefs[baselineStartingTokenKey] ?: return null
        val recordTypes = decodeRecordTypes(prefs[baselineRecordTypesKey] ?: return null) ?: return null
        val startTime = prefs[baselineStartTimeKey] ?: return null
        val endTime = prefs[baselineEndTimeKey] ?: return null
        val completedTypes = decodeRecordTypes(prefs[baselineCompletedTypesKey].orEmpty()) ?: return null
        val currentTypeName = prefs[baselineCurrentTypeKey]
        val currentType = currentTypeName?.let(::decodeRecordType)
        if (currentTypeName != null && currentType == null) return null
        val nextPageToken = prefs[baselineNextPageTokenKey]
        return HealthConnectBaselineCheckpoint(
            generation = generation,
            startingToken = startingToken,
            recordTypes = recordTypes,
            scanProgress = HealthConnectBaselineScanProgress(
                startTimeEpochMillis = startTime,
                endTimeEpochMillis = endTime,
                completedRecordTypes = completedTypes,
                currentRecordType = currentType,
                nextPageToken = nextPageToken,
            ),
        )
    }

    override suspend fun saveBaselineCheckpoint(checkpoint: HealthConnectBaselineCheckpoint) {
        dataStore.edit { prefs ->
            prefs[baselineGenerationKey] = checkpoint.generation
            prefs[baselineStartingTokenKey] = checkpoint.startingToken
            prefs[baselineRecordTypesKey] = encodeRecordTypes(checkpoint.recordTypes)
            prefs[baselineStartTimeKey] = checkpoint.scanProgress.startTimeEpochMillis
            prefs[baselineEndTimeKey] = checkpoint.scanProgress.endTimeEpochMillis
            prefs[baselineCompletedTypesKey] = encodeRecordTypes(checkpoint.scanProgress.completedRecordTypes)
            checkpoint.scanProgress.currentRecordType?.let { currentType ->
                prefs[baselineCurrentTypeKey] = currentType.wireName
            } ?: prefs.remove(baselineCurrentTypeKey)
            checkpoint.scanProgress.nextPageToken?.let { pageToken ->
                prefs[baselineNextPageTokenKey] = pageToken
            } ?: prefs.remove(baselineNextPageTokenKey)
        }
    }

    override suspend fun clearBaselineCheckpoint() {
        dataStore.edit { prefs ->
            prefs.remove(baselineGenerationKey)
            prefs.remove(baselineStartingTokenKey)
            prefs.remove(baselineRecordTypesKey)
            prefs.remove(baselineStartTimeKey)
            prefs.remove(baselineEndTimeKey)
            prefs.remove(baselineCompletedTypesKey)
            prefs.remove(baselineCurrentTypeKey)
            prefs.remove(baselineNextPageTokenKey)
        }
    }

    private suspend fun createInstallationId(): String {
        val generated = UUID.randomUUID().toString()
        var result = generated
        dataStore.edit { prefs ->
            result = prefs[installationIdKey] ?: generated
            prefs[installationIdKey] = result
        }
        return result
    }

    private fun encodeRecordTypes(recordTypes: Set<HealthConnectRecordType>): String =
        recordTypes.sortedBy { it.wireName }.joinToString(",") { it.wireName }

    private fun decodeRecordTypes(encoded: String): Set<HealthConnectRecordType>? {
        if (encoded.isEmpty()) return emptySet()
        val decoded = encoded.split(",").map { decodeRecordType(it) ?: return null }
        return decoded.toCollection(linkedSetOf())
    }

    private fun decodeRecordType(wireName: String): HealthConnectRecordType? =
        HealthConnectRecordType.entries.firstOrNull { it.wireName == wireName }
}
