package com.tether.capture

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.tether.capture.databinding.ActivityHealthConnectRationaleBinding

class HealthConnectRationaleActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val binding = ActivityHealthConnectRationaleBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.closeButton.setOnClickListener { finish() }
    }
}
