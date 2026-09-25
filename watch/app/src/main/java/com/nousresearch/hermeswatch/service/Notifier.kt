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
        // Channels we have superseded. Recreating a channel with the same id
        // does *not* reliably apply new settings -- the system restored the old
        // mVibrationPattern when this file first tried it -- so the alerting
        // channels are versioned and the previous ids are cleaned up here. The
        // user sees one set of channels either way.
        RETIRED_CHANNELS.forEach(manager::deleteNotificationChannel)
        ensureChannel(
            manager, CHANNEL_APPROVALS, R.string.channel_approvals,
            R.string.channel_approvals_description, NotificationManager.IMPORTANCE_HIGH, ALERT_PATTERN,
        )
        ensureChannel(
            manager, CHANNEL_MESSAGES, R.string.channel_messages,
            R.string.channel_messages_description, NotificationManager.IMPORTANCE_HIGH, ALERT_PATTERN,
        )
        ensureChannel(
            manager, CHANNEL_STATUS, R.string.channel_status,
            R.string.channel_status_description, NotificationManager.IMPORTANCE_LOW, null,
        )
    }

    /**
     * Create a channel, or recreate it when a setting Android will not let us
     * change in place differs.
     *
     * A channel's vibration pattern is fixed at creation: an app may rename and
     * re-describe a channel later, but not change how it buzzes, so an installed
     * app would keep the old pattern forever. Deleting and recreating under the
     * same id was not enough either -- the system restored the previous pattern
     * -- hence the versioned ids: a new id is the only thing that reliably takes
     * effect, which is why the constant above carries a version.
     */
    private fun ensureChannel(
        manager: NotificationManager,
        id: String,
        nameRes: Int,
        descRes: Int,
        importance: Int,
        pattern: LongArray?,
    ) {
        val existing = manager.getNotificationChannel(id)
        if (existing != null) {
            val current = existing.vibrationPattern
            val matches = if (pattern == null) current == null else current != null && current.contentEquals(pattern)
            if (matches) return
            manager.deleteNotificationChannel(id)
        }
        manager.createNotificationChannel(
            NotificationChannel(id, context.getString(nameRes), importance).apply {
                description = context.getString(descRes)
                if (pattern == null) {
                    setShowBadge(false)
                } else {
                    enableVibration(true)
                    vibrationPattern = pattern
                }
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

    /**
     * A message Hermes pushed to the watch: `hermes send`, a cron job's
     * delivery, or the agent's own send_message. No actions — there is nothing
     * to decide — and one notification id, so the newest message replaces the
     * previous one instead of stacking up on a 2-inch screen.
     */
    fun showMessage(text: String) {
        val notification = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(R.drawable.ic_hermes)
            .setContentTitle(context.getString(R.string.title_message))
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(contentIntent())
            .build()
        notify(MESSAGE_NOTIFICATION_ID, notification)
    }

    /**
     * The agent stopped working: the end of a turn, or of the session.
     *
     * Opt-in on the host (`extra.notify_turn_finished`), because it is one buzz
     * per turn -- wanted when you are waiting on a long job, noise otherwise.
     * Reuses the notifications channel so there is one mute switch for
     * everything Hermes says, and its own id so it does not overwrite a message.
     */
    fun showTurnFinished(reason: String?) {
        val clean = (reason ?: "").trim()
        val body = if (clean.isEmpty() || clean == "completed") {
            context.getString(R.string.turn_finished_body)
        } else {
            context.getString(R.string.turn_stopped_body, clean)
        }
        val notification = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(R.drawable.ic_hermes)
            .setContentTitle(context.getString(R.string.title_turn_finished))
            .setContentText(body)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(contentIntent())
            .build()
        notify(TURN_NOTIFICATION_ID, notification)
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
        // Only measured values appear here. The previous fallback substituted the
        // word "idle" for a missing rate, so an idle watch read "idle · idle" and
        // a working one read "thinking · idle" -- which both looks like a stuck
        // app and asserts a state nobody observed.
        val text = snapshot?.let {
            val parts = mutableListOf(it.agentState)
            it.tokensPerSecondLive?.let { value -> parts += "%.0f tok/s".format(value) }
            it.contextRemainingPercent?.let { value -> parts += "%.0f%% ctx".format(value) }
            parts.joinToString(" · ")
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
        //: Versioned: the vibration pattern is fixed when a channel is created,
        //: so a change to it needs a new id rather than a recreated one.
        const val CHANNEL_APPROVALS = "hermes-approvals-v2"
        const val CHANNEL_MESSAGES = "hermes-messages-v2"
        val RETIRED_CHANNELS = listOf("hermes-approvals", "hermes-messages")
        const val CHANNEL_STATUS = "hermes-status"
        const val APPROVAL_NOTIFICATION_ID = 1001
        const val QUESTION_NOTIFICATION_ID = 1002
        const val LINK_NOTIFICATION_ID = 1003
        const val MESSAGE_NOTIFICATION_ID = 1004
        const val TURN_NOTIFICATION_ID = 1005

        /**
         * A double buzz: two short pulses rather than one.
         *
         * Set on the channel, not on the notification: from Android 8 the
         * channel owns vibration, so a pattern passed to the notification is
         * ignored. It is what makes a Hermes alert feel different from a chat
         * message in the wrist-blind moment that matters.
         */
        val ALERT_PATTERN = longArrayOf(0L, 220L, 120L, 220L)
        const val FOREGROUND_NOTIFICATION_ID = 1000
    }
}
