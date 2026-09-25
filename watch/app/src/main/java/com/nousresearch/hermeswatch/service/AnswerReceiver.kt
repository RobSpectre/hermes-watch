package com.nousresearch.hermeswatch.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.app.PendingIntent
import androidx.core.content.ContextCompat

/**
 * Receives taps on the notification's approve/deny buttons.
 *
 * It does not talk to the socket itself: it routes the decision back into
 * [WatchLinkService]. That keeps every WebSocket write on one thread, and it
 * means the answer still works if the service was killed and the button was
 * tapped before the notification was reaped (the service is restarted, finds no
 * live request for that id, and the bridge rejects the stale answer — correct
 * behaviour).
 */
class AnswerReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val id = intent.getStringExtra(EXTRA_ID) ?: return
        val choice = intent.getStringExtra(EXTRA_CHOICE) ?: return
        val serviceIntent = Intent(context, WatchLinkService::class.java).apply {
            action = WatchLinkService.ACTION_ANSWER
            putExtra(EXTRA_ID, id)
            putExtra(EXTRA_CHOICE, choice)
        }
        ContextCompat.startForegroundService(context, serviceIntent)
    }

    companion object {
        const val EXTRA_ID = "request_id"
        const val EXTRA_CHOICE = "choice"
        const val REQUEST_CODE_BASE = 2000

        fun pendingIntent(context: Context, id: String, choice: String): PendingIntent {
            val intent = Intent(context, AnswerReceiver::class.java).apply {
                putExtra(EXTRA_ID, id)
                putExtra(EXTRA_CHOICE, choice)
            }
            // Distinct request codes per (id, choice) so the system does not
            // collapse four different buttons into one PendingIntent.
            val requestCode = REQUEST_CODE_BASE + (id.hashCode() * 31 + choice.hashCode()) and 0xFFFF
            return PendingIntent.getBroadcast(
                context,
                requestCode,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        }
    }
}
