package com.tether.capture.wear

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.tether.capture.CaptureSettings
import com.tether.capture.wear.databinding.ActivityWearSettingsBinding
import kotlinx.coroutines.launch

/**
 * A two-field form: host base URL + API token, persisted via DataStore.
 * Entered on the watch keyboard/voice-to-text since the watch authenticates
 * independently of the phone (mirrors app/SettingsActivity.kt).
 */
class WearSettingsActivity : AppCompatActivity() {
    private lateinit var binding: ActivityWearSettingsBinding
    private lateinit var repository: WearSettingsRepository

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityWearSettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        repository = WearSettingsRepository(applicationContext)

        lifecycleScope.launch {
            val current = repository.load()
            binding.hostInput.setText(current.hostUrl)
            binding.tokenInput.setText(current.token)
        }

        binding.saveButton.setOnClickListener {
            val settings =
                CaptureSettings.normalize(
                    binding.hostInput.text?.toString().orEmpty(),
                    binding.tokenInput.text?.toString().orEmpty(),
                )
            lifecycleScope.launch {
                repository.save(settings)
                Toast.makeText(
                    this@WearSettingsActivity,
                    getString(R.string.settings_saved),
                    Toast.LENGTH_SHORT,
                ).show()
                finish()
            }
        }
    }
}
