package com.tether.capture

/**
 * The two things the client needs to talk to a host: where it lives and the
 * bearer token to authenticate with. Kept as a plain immutable value so its
 * normalization/validation is unit-testable without DataStore or Android.
 */
data class CaptureSettings(
    val hostUrl: String = "",
    val token: String = "",
) {
    /** True once both fields are non-blank — the client can attempt a capture. */
    fun isConfigured(): Boolean = hostUrl.isNotBlank() && token.isNotBlank()

    companion object {
        /** Trim user input and strip a trailing slash so URL joins stay clean. */
        fun normalize(hostUrl: String, token: String): CaptureSettings =
            CaptureSettings(hostUrl = hostUrl.trim().trimEnd('/'), token = token.trim())
    }
}
