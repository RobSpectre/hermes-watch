package com.nousresearch.hermeswatch

import android.app.Application
import androidx.core.app.NotificationManagerCompat

class HermesWatchApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Channels are created eagerly so the first approval can notify even if
        // the service has not started yet on this boot.
        com.nousresearch.hermeswatch.service.Notifier(this).ensureChannels()
    }

    /** Whether the user has granted notification permission (Wear OS 3+ runtime). */
    fun canNotify(): Boolean = NotificationManagerCompat.from(this).areNotificationsEnabled()
}
