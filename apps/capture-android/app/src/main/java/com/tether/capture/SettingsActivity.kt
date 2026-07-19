package com.tether.capture

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.tether.capture.databinding.ActivitySettingsBinding
import kotlinx.coroutines.launch

/** A two-field form: host base URL + API token, persisted via DataStore. */
class SettingsActivity : AppCompatActivity() {
    private lateinit var binding: ActivitySettingsBinding
    private lateinit var repository: SettingsRepository

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        repository = SettingsRepository(applicationContext)

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
                    this@SettingsActivity,
                    getString(R.string.settings_saved),
                    Toast.LENGTH_SHORT,
                ).show()
                finish()
            }
        }
    }
}
