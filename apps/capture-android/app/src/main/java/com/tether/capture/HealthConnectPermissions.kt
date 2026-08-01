package com.tether.capture

data class HealthConnectPermissionSummary(
    val missingRequired: Set<String>,
    val missingOptional: Set<String>,
) {
    val canReadAllRecords: Boolean = missingRequired.isEmpty()
}

object HealthConnectPermissions {
    const val READ_HEART_RATE = "android.permission.health.READ_HEART_RATE"
    const val READ_SLEEP = "android.permission.health.READ_SLEEP"
    const val READ_STEPS = "android.permission.health.READ_STEPS"
    const val READ_EXERCISE = "android.permission.health.READ_EXERCISE"
    const val READ_HEALTH_DATA_HISTORY = "android.permission.health.READ_HEALTH_DATA_HISTORY"
    const val READ_HEALTH_DATA_IN_BACKGROUND = "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"

    val required: Set<String> = linkedSetOf(
        READ_HEART_RATE,
        READ_SLEEP,
        READ_STEPS,
        READ_EXERCISE,
    )

    val optional: Set<String> = linkedSetOf(
        READ_HEALTH_DATA_HISTORY,
        READ_HEALTH_DATA_IN_BACKGROUND,
    )

    fun requested(supportedOptional: Set<String>): Set<String> = required + supportedOptional

    fun summarize(
        granted: Set<String>,
        supportedOptional: Set<String> = optional,
    ): HealthConnectPermissionSummary = HealthConnectPermissionSummary(
        missingRequired = required - granted,
        missingOptional = supportedOptional - granted,
    )
}
