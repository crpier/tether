package com.tether.capture

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.lifecycle.lifecycleScope
import com.tether.capture.databinding.ActivitySettingsBinding
import kotlinx.coroutines.launch
import java.text.DateFormat
import java.util.Date

class SettingsActivity : AppCompatActivity() {
    private lateinit var binding: ActivitySettingsBinding
    private lateinit var repository: SettingsRepository
    private lateinit var scheduler: HealthConnectWorkScheduler

    private val requestHealthPermissions = registerForActivityResult(
        PermissionController.createRequestPermissionResultContract(),
    ) { granted ->
        val summary = HealthConnectPermissions.summarize(granted)
        if (summary.canReadCapturedRecords) {
            scheduler.ensurePeriodicSync()
            scheduler.syncNow()
            toast(getString(R.string.health_connect_sync_queued))
        }
        refreshHealthStatus()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        repository = SettingsRepository(applicationContext)
        scheduler = HealthConnectWorkScheduler.fromContext(applicationContext)

        lifecycleScope.launch {
            val current = repository.load()
            binding.hostInput.setText(current.hostUrl)
            binding.tokenInput.setText(current.token)
        }

        binding.saveButton.setOnClickListener {
            val settings = CaptureSettings.normalize(
                binding.hostInput.text?.toString().orEmpty(),
                binding.tokenInput.text?.toString().orEmpty(),
            )
            lifecycleScope.launch {
                repository.save(settings)
                toast(getString(R.string.settings_saved))
            }
        }
        binding.healthGrantButton.setOnClickListener { grantOrEnableHealthConnect() }
        binding.healthSyncButton.setOnClickListener {
            scheduler.syncNow()
            toast(getString(R.string.health_connect_sync_queued))
            refreshHealthStatus()
        }
    }

    override fun onResume() {
        super.onResume()
        refreshHealthStatus()
    }

    private fun grantOrEnableHealthConnect() {
        when (HealthConnectClient.getSdkStatus(this)) {
            HealthConnectClient.SDK_AVAILABLE -> {
                val supportedOptional = AndroidHealthConnectEnvironment(this)
                    .supportedOptionalPermissions()
                requestHealthPermissions.launch(
                    HealthConnectPermissions.requested(supportedOptional),
                )
            }
            HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> startActivity(
                Intent(
                    if (Build.VERSION.SDK_INT >= 34) {
                        "android.health.connect.action.HEALTH_HOME_SETTINGS"
                    } else {
                        "androidx.health.ACTION_HEALTH_CONNECT_SETTINGS"
                    },
                ),
            )
        }
    }

    private fun refreshHealthStatus() {
        lifecycleScope.launch {
            val platform = HealthConnectStatusReader(
                AndroidHealthConnectEnvironment(applicationContext),
            ).read()
            val sync = repository.healthConnectStatus()
            render(platform, sync)
        }
    }

    private fun render(platform: HealthConnectStatus, sync: HealthConnectSyncStatus) {
        val available = platform as? HealthConnectStatus.Available
        binding.healthAvailability.setText(
            when (platform) {
                HealthConnectStatus.Unsupported -> R.string.health_connect_unavailable
                HealthConnectStatus.ProviderUpdateRequired -> R.string.health_connect_update_required
                is HealthConnectStatus.Available -> R.string.health_connect_available
            },
        )
        binding.healthPermissions.setText(
            when {
                available == null -> R.string.health_connect_permissions_missing
                !available.permissions.canReadCapturedRecords -> R.string.health_connect_permissions_missing
                !available.permissions.canReadAllRecords -> R.string.health_connect_permissions_partial
                available.permissions.missingOptional.isNotEmpty() -> R.string.health_connect_optional_missing
                else -> R.string.health_connect_permissions_granted
            },
        )
        binding.healthSyncStatus.setText(
            if (sync.running) R.string.health_connect_running else R.string.health_connect_idle,
        )
        binding.healthLastSuccess.text = sync.lastSuccessEpochMillis?.let { timestamp ->
            getString(
                R.string.health_connect_last_success,
                DateFormat.getDateTimeInstance().format(Date(timestamp)),
            )
        } ?: getString(R.string.health_connect_never_synced)
        binding.healthLastFailure.text = sync.lastFailure?.let { failure ->
            getString(R.string.health_connect_last_failure, failure)
        } ?: getString(R.string.health_connect_no_failure)
        binding.healthGrantButton.isEnabled = platform != HealthConnectStatus.Unsupported
        binding.healthSyncButton.isEnabled = available?.permissions?.canReadCapturedRecords == true && !sync.running
        if (available?.permissions?.canReadCapturedRecords == true) {
            scheduler.ensurePeriodicSync()
        }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }
}
