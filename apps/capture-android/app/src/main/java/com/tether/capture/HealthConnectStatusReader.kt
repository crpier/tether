package com.tether.capture

import android.content.Context
import androidx.health.connect.client.HealthConnectClient

interface HealthConnectEnvironment {
    fun sdkStatus(): Int

    fun supportedOptionalPermissions(): Set<String>

    suspend fun grantedPermissions(): Set<String>
}

class AndroidHealthConnectEnvironment(context: Context) : HealthConnectEnvironment {
    private val appContext = context.applicationContext

    override fun sdkStatus(): Int = HealthConnectClient.getSdkStatus(appContext)

    override fun supportedOptionalPermissions(): Set<String> {
        if (sdkStatus() != HealthConnectClient.SDK_AVAILABLE) return emptySet()
        val features = HealthConnectClient.getOrCreate(appContext).features
        return buildSet {
            if (features.getFeatureStatus(FEATURE_BACKGROUND_READ) == FEATURE_AVAILABLE) {
                add(HealthConnectPermissions.READ_HEALTH_DATA_IN_BACKGROUND)
            }
            if (features.getFeatureStatus(FEATURE_HISTORY_READ) == FEATURE_AVAILABLE) {
                add(HealthConnectPermissions.READ_HEALTH_DATA_HISTORY)
            }
        }
    }

    override suspend fun grantedPermissions(): Set<String> =
        HealthConnectClient.getOrCreate(appContext).permissionController.getGrantedPermissions()

    companion object {
        // Public AndroidX values are Kotlin-restricted in the pinned alpha API.
        private const val FEATURE_BACKGROUND_READ = 1
        private const val FEATURE_HISTORY_READ = 4
        private const val FEATURE_AVAILABLE = 2
    }
}

sealed class HealthConnectStatus {
    data object Unsupported : HealthConnectStatus()
    data object ProviderUpdateRequired : HealthConnectStatus()
    data class Available(val permissions: HealthConnectPermissionSummary) : HealthConnectStatus()
}

class HealthConnectStatusReader(
    private val environment: HealthConnectEnvironment,
) {
    suspend fun read(): HealthConnectStatus = when (environment.sdkStatus()) {
        HealthConnectClient.SDK_AVAILABLE -> HealthConnectStatus.Available(
            HealthConnectPermissions.summarize(
                granted = environment.grantedPermissions(),
                supportedOptional = environment.supportedOptionalPermissions(),
            ),
        )
        HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> HealthConnectStatus.ProviderUpdateRequired
        else -> HealthConnectStatus.Unsupported
    }
}
