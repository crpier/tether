package com.tether.capture

data class HealthConnectPermissionSummary(
    val missingRequired: Set<String>,
    val missingOptional: Set<String>,
    val capturedRecordTypes: Set<HealthConnectRecordType>,
    val missingCapturedRecordTypes: Set<HealthConnectRecordType>,
) {
    val canReadAllRecords: Boolean = missingRequired.isEmpty()
    val canReadCapturedRecords: Boolean = capturedRecordTypes.isNotEmpty()
}

object HealthConnectPermissions {
    const val READ_HEART_RATE = "android.permission.health.READ_HEART_RATE"
    const val READ_SLEEP = "android.permission.health.READ_SLEEP"
    const val READ_STEPS = "android.permission.health.READ_STEPS"
    const val READ_EXERCISE = "android.permission.health.READ_EXERCISE"
    const val READ_HEALTH_DATA_HISTORY = "android.permission.health.READ_HEALTH_DATA_HISTORY"
    const val READ_HEALTH_DATA_IN_BACKGROUND = "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"

    private val capturedPermissionByType: Map<HealthConnectRecordType, String> = linkedMapOf(
        HealthConnectRecordType.HEART_RATE to READ_HEART_RATE,
        HealthConnectRecordType.SLEEP to READ_SLEEP,
        HealthConnectRecordType.STEPS to READ_STEPS,
        HealthConnectRecordType.EXERCISE to READ_EXERCISE,
    )

    val required: Set<String> = HealthConnectRecordInventory.entries
        .map { it.readPermission }
        .toCollection(linkedSetOf())

    val captured: Set<String> = capturedPermissionByType.values.toCollection(linkedSetOf())

    val optional: Set<String> = linkedSetOf(
        READ_HEALTH_DATA_HISTORY,
        READ_HEALTH_DATA_IN_BACKGROUND,
    )

    fun capturedRecordTypes(granted: Set<String>): Set<HealthConnectRecordType> =
        capturedPermissionByType
            .filterValues { it in granted }
            .keys

    fun requested(supportedOptional: Set<String>): Set<String> = required + supportedOptional

    fun summarize(
        granted: Set<String>,
        supportedOptional: Set<String> = optional,
    ): HealthConnectPermissionSummary {
        val grantedCapturedRecordTypes = capturedRecordTypes(granted)
        return HealthConnectPermissionSummary(
            missingRequired = required - granted,
            missingOptional = supportedOptional - granted,
            capturedRecordTypes = grantedCapturedRecordTypes,
            missingCapturedRecordTypes = capturedPermissionByType.keys - grantedCapturedRecordTypes,
        )
    }
}
