package com.nousresearch.hermeswatch.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.nousresearch.hermeswatch.R
import com.nousresearch.hermeswatch.data.PendingRequest
import com.nousresearch.hermeswatch.data.Snapshot
import com.nousresearch.hermeswatch.ui.MainActivity

/**
 * All notifications live here, because the presentation of "the agent is
 * blocked" is the product's whole interaction model and it should be readable
 * in one file.
 *
 * Two channels with very different importance:
 *  * **approvals** — HIGH. Buzzes, has action buttons, and carries
 *    `setTimeoutAfter` matching the bridge's own deadline so the prompt
 *    disappears from the watch at the same moment the request expires on the
 *    bridge. A prompt that outlives its request is worse than no prompt.
 *  * **status** — LOW, silent, ongoing. Quiet signal that the link is up.
 *
 * Button rendering note: Wear OS renders notification actions as buttons, but
 * the exact presentation depends on the device and the number of actions. This
 * is the part of the app most likely to need adjustment on real hardware.
 */
class Notifier(private val context: Context) {

    fun ensureChannels() {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_APPROVALS,
                context.getString(R.string.channel_approvals),
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = context.getString(R.string.channel_approvals_description)
                enableVibration(true)
            },
        )
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_STATUS,
                context.getString(R.string.channel_status),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = context.getString(R.string.channel_status_description)
                setShowBadge(false)
            },
        )
    }

    fun showApproval(request: PendingRequest) {
        val actions = request.choices.map { choice ->
            val label = when (choice) {
                "once" -> context.getString(R.string.action_approve_once)
                "session" -> context.getString(R.string.action_approve_session)
                "always" -> context.getString(R.string.action_approve_always)
                else -> context.getString(R.string.action_deny)
            }
            NotificationCompat.Action.Builder(
                iconFor(choice),
                label,
                AnswerReceiver.pendingIntent(context, request.id, choice),
            ).build()
        }

        val body = buildString {
            request.command?.let { append(it) }
            request.description?.takeIf { it.isNotBlank() }?.let {
                if (isNotEmpty()) append('\n')
                append(it)
            }
        }

        val notification = NotificationCompat.Builder(context, CHANNEL_APPROVALS)
            .setSmallIcon(R.drawable.ic_hermes)
            .setContentTitle(context.getString(R.string.title_needs_approval))
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setOngoing(true)
            .setOnlyAlertOnce(false)
            .apply { request.remainingSeconds?.let { setTimeoutAfter((it * 1000).toLong()) } }
            .setContentIntent(contentIntent())
            .apply { actions.take(3).forEach { addAction(it) } }
            .build()

        notify(APPROVAL_NOTIFICATION_ID, notification)
    }

    fun showQuestion(question: String) {
        val notification = NotificationCompat.Builder(context, CHANNEL_APPROVALS)
            .setSmallIcon(R.drawable.ic_hermes)
            .setContentTitle(context.getString(R.string.title_question))
            .setContentText(question)
            .setStyle(NotificationCompat.BigTextStyle().bigText(question))
            // No actions: v1 cannot answer a question from a plugin, so offering
            // a button that cannot work would be worse than none. Tapping opens
            // the app, which says plainly to go back to the terminal.
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(contentIntent())
            .build()
        notify(QUESTION_NOTIFICATION_ID, notification)
    }

    fun clearApproval() {
        NotificationManagerCompat.from(context).cancel(APPROVAL_NOTIFICATION_ID)
    }

    fun clearQuestion() {
        NotificationManagerCompat.from(context).cancel(QUESTION_NOTIFICATION_ID)
    }

    /** Something the user needs to know about the link itself, not the agent. */
    fun showLinkProblem(message: String, id: Int = LINK_NOTIFICATION_ID) {
        val notification = NotificationCompat.Builder(context, CHANNEL_STATUS)
            .setSmallIcon(R.drawable.ic_hermes)
            .setContentTitle(context.getString(R.string.app_name))
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(contentIntent())
            .build()
        notify(id, notification)
    }

    /** Ongoing, silent "the link is up" notification for the foreground service. */
    fun statusNotification(snapshot: Snapshot?): android.app.Notification {
        val text = snapshot?.let {
            val rate = it.tokensPerSecondLive?.let { value -> "%.0f tok/s".format(value) } ?: "idle"
            val remaining = it.contextRemainingPercent?.let { value -> " · %.0f%% ctx".format(value) } ?: ""
            "${it.agentState} · $rate$remaining"
        } ?: "connecting…"
        return NotificationCompat.Builder(context, CHANNEL_STATUS)
            .setSmallIcon(R.drawable.ic_hermes)
            .setContentTitle(context.getString(R.string.app_name))
            .setContentText(text)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .setShowWhen(false)
            .setContentIntent(contentIntent())
            .build()
    }

    private fun iconFor(choice: String): Int =
        if (choice == "deny") android.R.drawable.ic_menu_close_clear_cancel
        else android.R.drawable.ic_menu_send

    private fun contentIntent(): PendingIntent = PendingIntent.getActivity(
        context,
        0,
        Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    private fun notify(id: Int, notification: android.app.Notification) {
        // POST_NOTIFICATIONS is runtime-granted on Wear OS 3+; if it was denied
        // the notification is silently dropped, which is the user's choice.
        runCatching {
            NotificationManagerCompat.from(context).notify(id, notification)
        }
    }

    companion object {
        const val CHANNEL_APPROVALS = "hermes-approvals"
        const val CHANNEL_STATUS = "hermes-status"
        const val APPROVAL_NOTIFICATION_ID = 1001
        const val QUESTION_NOTIFICATION_ID = 1002
        const val LINK_NOTIFICATION_ID = 1003
        const val FOREGROUND_NOTIFICATION_ID = 1000
    }
}
