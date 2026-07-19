package com.tether.capture

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The share-target entry point. Android hands us shared text (a note, a URL);
 * we POST it to the host's text-capture endpoint and finish immediately. No UI
 * beyond a success/failure toast — this activity has no window of its own.
 */
class ShareActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val shared = extractSharedText(intent)
        if (shared.isNullOrBlank()) {
            toast(getString(R.string.nothing_to_share))
            finish()
            return
        }
        capture(shared)
    }

    private fun extractSharedText(intent: Intent?): String? {
        if (intent?.action != Intent.ACTION_SEND) return null
        return intent.getStringExtra(Intent.EXTRA_TEXT)
    }

    private fun capture(content: String) {
        val repository = SettingsRepository(applicationContext)
        lifecycleScope.launch {
            val settings = repository.load()
            if (!settings.isConfigured()) {
                toast(getString(R.string.not_configured))
                finish()
                return@launch
            }
            val result =
                withContext(Dispatchers.IO) {
                    runCatching {
                        val request =
                            CaptureClient.buildTextRequest(settings.hostUrl, settings.token, content)
                        CaptureClient.client.newCall(request).execute().use { response ->
                            if (!response.isSuccessful) {
                                throw RuntimeException("HTTP ${response.code}")
                            }
                        }
                    }
                }
            result
                .onSuccess { toast(getString(R.string.captured)) }
                .onFailure { error -> toast(getString(R.string.capture_failed, error.message ?: "")) }
            finish()
        }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }
}
