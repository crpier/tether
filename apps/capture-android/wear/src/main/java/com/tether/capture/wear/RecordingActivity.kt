package com.tether.capture.wear

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.view.MotionEvent
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.tether.capture.CaptureClient
import com.tether.capture.wear.databinding.ActivityRecordingBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * The tile's launch target: a hold-to-record button, mirroring the phone
 * app's MainActivity almost exactly. Press starts a MediaRecorder (m4a),
 * release stops and uploads to the shared voice-capture endpoint. Unlike the
 * phone app, losing foreground mid-hold (a notification, the watch going to
 * ambient, a keyguard prompt) must stop the mic immediately rather than wait
 * for a touch-up event that may never arrive.
 */
class RecordingActivity : AppCompatActivity() {
    private lateinit var binding: ActivityRecordingBinding
    private lateinit var settingsRepository: WearSettingsRepository

    private var recorder: MediaRecorder? = null
    private var outputFile: File? = null
    private var isRecording = false

    private val requestPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) {
                toast(getString(R.string.mic_permission_denied))
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityRecordingBinding.inflate(layoutInflater)
        setContentView(binding.root)
        settingsRepository = WearSettingsRepository(applicationContext)

        binding.settingsButton.setOnClickListener {
            startActivity(Intent(this, WearSettingsActivity::class.java))
        }

        binding.recordButton.setOnTouchListener { view, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    view.performClick()
                    onPressStart()
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    onPressStop()
                    true
                }
                else -> false
            }
        }
    }

    override fun onPause() {
        super.onPause()
        // The mic must never keep running once the activity isn't in front —
        // a notification, ambient mode, or a keyguard prompt can all steal
        // focus mid-hold without ever delivering ACTION_UP.
        if (isRecording) {
            stopRecording()?.delete()
            toast(getString(R.string.recording_interrupted))
        }
    }

    private fun onPressStart() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermission.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        startRecording()
    }

    private fun onPressStop() {
        if (!isRecording) return
        val file = stopRecording() ?: return
        upload(file)
    }

    private fun startRecording() {
        val file = File(cacheDir, "voice_${System.currentTimeMillis()}.m4a")
        val newRecorder =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                MediaRecorder(this)
            } else {
                @Suppress("DEPRECATION")
                MediaRecorder()
            }
        newRecorder.apply {
            setAudioSource(MediaRecorder.AudioSource.MIC)
            setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            setOutputFile(file.absolutePath)
            prepare()
            start()
        }
        recorder = newRecorder
        outputFile = file
        isRecording = true
        binding.recordButton.setText(R.string.recording)
    }

    private fun stopRecording(): File? {
        isRecording = false
        binding.recordButton.setText(R.string.hold_to_record)
        val file = outputFile
        try {
            recorder?.stop()
        } catch (_: RuntimeException) {
            // Stop can throw when the clip is too short to have produced any
            // frames; discard the (invalid) file and bail.
            file?.delete()
            recorder?.release()
            recorder = null
            outputFile = null
            toast(getString(R.string.recording_too_short))
            return null
        } finally {
            recorder?.release()
            recorder = null
            outputFile = null
        }
        return file
    }

    private fun upload(file: File) {
        binding.recordButton.isEnabled = false
        binding.statusText.setText(R.string.uploading)
        lifecycleScope.launch {
            val settings = settingsRepository.load()
            if (!settings.isConfigured()) {
                binding.recordButton.isEnabled = true
                binding.statusText.text = ""
                file.delete()
                toast(getString(R.string.not_configured))
                return@launch
            }
            val result =
                withContext(Dispatchers.IO) {
                    runCatching {
                        val request =
                            CaptureClient.buildVoiceRequest(settings.hostUrl, settings.token, file)
                        CaptureClient.client.newCall(request).execute().use { response ->
                            val payload = response.body?.string().orEmpty()
                            if (!response.isSuccessful) {
                                throw RuntimeException("HTTP ${response.code}")
                            }
                            CaptureClient.parseTranscript(payload)
                        }
                    }
                }
            file.delete()
            binding.recordButton.isEnabled = true
            result
                .onSuccess { transcript ->
                    binding.statusText.text = getString(R.string.transcript_prefix, transcript)
                }
                .onFailure { error ->
                    binding.statusText.text = ""
                    toast(getString(R.string.upload_failed, error.message ?: ""))
                }
        }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }
}
