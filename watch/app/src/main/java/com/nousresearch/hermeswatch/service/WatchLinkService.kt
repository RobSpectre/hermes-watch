package com.nousresearch.hermeswatch.service

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log
import com.nousresearch.hermeswatch.data.BridgeClient
import com.nousresearch.hermeswatch.data.BridgeSettings
import com.nousresearch.hermeswatch.data.Frame
import com.nousresearch.hermeswatch.data.LinkState
import com.nousresearch.hermeswatch.data.PendingRequest
import com.nousresearch.hermeswatch.data.Protocol
import com.nousresearch.hermeswatch.data.SettingsStore
import com.nousresearch.hermeswatch.data.Snapshot
import com.nousresearch.hermeswatch.data.TileCache
import com.nousresearch.hermeswatch.data.toPendingRequest
import com.nousresearch.hermeswatch.data.toSnapshot
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * The one long-lived component: it owns the WebSocket to the bridge.
 *
 * A foreground service is not optional here. Wear OS suspends ordinary
 * background work aggressively, and a socket that survives the screen turning
 * off requires a foreground service with an ongoing notification. `dataSync` is
 * the honest type — this is a low-rate control/data stream, not a Bluetooth or
 * media session.
 *
 * Everything else in the app reads state from here through [WatchLinkService.state]
 * or the cached snapshot; nothing else opens a socket.
 */
class WatchLinkService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var settings: SettingsStore
    private lateinit var client: BridgeClient
    private lateinit var notifier: Notifier

    @Volatile
    private var currentSnapshot: Snapshot? = null

    override fun onCreate() {
        super.onCreate()
        settings = SettingsStore(this)
        notifier = Notifier(this)
        notifier.ensureChannels()
        client = BridgeClient(scope, label = android.os.Build.MODEL ?: "wear-os")
        startForeground(
            Notifier.FOREGROUND_NOTIFICATION_ID,
            notifier.statusNotification(null),
        )
        observeSettings()
        observeFrames()
        observeLinkState()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_ANSWER -> handleAnswer(intent)
            ACTION_RECONNECT -> reconnect()
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        client.disconnect()
        scope.cancel()
        super.onDestroy()
    }

    private fun observeSettings() {
        scope.launch {
            settings.settings.collectLatest { config ->
                currentSettings = config
                if (config.isComplete) {
                    client.connect(config.host, config.port, config.token)
                } else {
                    Log.i(TAG, "pairing incomplete; not connecting")
                }
            }
        }
    }

    private fun observeFrames() {
        scope.launch {
            client.frames.collect { frame -> handleFrame(frame) }
        }
    }

    private fun observeLinkState() {
        scope.launch {
            client.state.collect { state ->
                _state.value = state
                if (state != LinkState.ONLINE) {
                    currentSnapshot = null
                }
                refreshForegroundNotification()
            }
        }
    }

    private fun handleFrame(frame: Frame) {
        when (frame.type) {
            Protocol.TYPE_SNAPSHOT, Protocol.TYPE_STATS -> {
                val snapshot = frame.toSnapshot()
                if (snapshot != null) {
                    currentSnapshot = snapshot
                    _snapshot.value = snapshot
                    // Cached synchronously for the tile, which cannot suspend.
                    TileCache.write(this@WatchLinkService, Protocol.json.encodeToString(Frame.serializer(), frame))
                    refreshForegroundNotification()

                    // The bridge redelivers open requests in every snapshot, so
                    // a watch that reconnects mid-approval re-posts the prompt.
                    // Prefer one we can answer: a request presented on another
                    // surface also lands here, without choices, and posting that
                    // over the actionable one leaves a card with no buttons.
                    val approvals = snapshot.pendingApprovals.filter { it.kind == "approval" }
                    (approvals.firstOrNull { it.choices.isNotEmpty() } ?: approvals.firstOrNull())
                        ?.let(notifier::showApproval)
                        ?: notifier.clearApproval()
                }
            }

            Protocol.TYPE_EVENT -> handleEvent(frame)

            Protocol.TYPE_ERROR -> Log.w(TAG, "bridge error: ${frame.error}")

            Protocol.TYPE_HELLO -> Log.i(TAG, "bridge ${frame.bridgeVersion} protocol ${frame.protocol}")
        }
    }

    private fun handleEvent(frame: Frame) {
        when (frame.event) {
            Protocol.EVENT_APPROVAL_REQUESTED -> frame.toPendingRequest()?.let { notifier.showApproval(it) }
            Protocol.EVENT_APPROVAL_RESOLVED -> notifier.clearApproval()
            Protocol.EVENT_QUESTION_PENDING -> {
                val question = frame.payload?.get("question")?.toString()?.trim('"') ?: return
                notifier.showQuestion(question)
            }
            Protocol.EVENT_QUESTION_RESOLVED -> notifier.clearQuestion()
            // Sent only when the host opted in (`extra.notify_turn_finished`):
            // "the agent stopped" is wanted when waiting on a long job and noise
            // otherwise, so the source decides, not the watch.
            Protocol.EVENT_LOOP_STOPPED, Protocol.EVENT_TURN_ENDED, Protocol.EVENT_SESSION_ENDED -> {
                val reason = frame.payload?.get("reason")?.toString()?.trim('"')
                notifier.showTurnFinished(reason)
            }
            Protocol.EVENT_MESSAGE -> {
                val text = frame.payload?.get("text")?.toString()?.trim('"') ?: return
                if (text.isNotBlank()) notifier.showMessage(text)
            }
            else -> Unit // turn/tool/session churn is visible in the stats, not a notification
        }
    }

    private fun handleAnswer(intent: Intent) {
        val id = intent.getStringExtra(AnswerReceiver.EXTRA_ID) ?: return
        val choice = intent.getStringExtra(AnswerReceiver.EXTRA_CHOICE) ?: return
        val sent = client.answer(id, choice)
        Log.i(TAG, "answer $id=$choice sent=$sent")
        if (sent) {
            // Optimistically clear: the bridge confirms with approval.resolved,
            // which is the authoritative path, but the button must feel immediate.
            notifier.clearApproval()
        } else {
            // No live socket. Say so plainly rather than failing silently; the
            // request stays open on the bridge, so answering later still works.
            notifier.showLinkProblem("Not connected — answer on the terminal")
        }
    }

    private fun reconnect() {
        val config = currentSettings
        if (config != null && config.isComplete) client.connect(config.host, config.port, config.token)
    }

    private fun refreshForegroundNotification() {
        // Only when the text actually changes. This used to re-post on every
        // snapshot -- every 1.5-5s -- which churned the notification shade and
        // made the status line impossible to dismiss: it reappeared on the next
        // tick. Nothing is lost by waiting for a real change, because the text
        // only says what the state and the measurements already say.
        val snapshot = currentSnapshot
        val text = notifier.statusText(snapshot)
        if (text == lastForegroundText) return
        lastForegroundText = text
        runCatching {
            val manager = getSystemService(android.app.NotificationManager::class.java)
            manager?.notify(Notifier.FOREGROUND_NOTIFICATION_ID, notifier.statusNotification(snapshot))
        }
    }

    @Volatile
    private var lastForegroundText: String? = null

    @Volatile
    private var currentSettings: BridgeSettings? = null

    companion object {
        private const val TAG = "WatchLinkService"
        const val ACTION_ANSWER = "com.nousresearch.hermeswatch.ANSWER"
        const val ACTION_RECONNECT = "com.nousresearch.hermeswatch.RECONNECT"

        /** Send a decision from the UI. Same path as a notification button tap. */
        fun answer(context: android.content.Context, id: String, choice: String) {
            val intent = Intent(context, WatchLinkService::class.java).apply {
                action = ACTION_ANSWER
                putExtra(AnswerReceiver.EXTRA_ID, id)
                putExtra(AnswerReceiver.EXTRA_CHOICE, choice)
            }
            androidx.core.content.ContextCompat.startForegroundService(context, intent)
        }

        /** Process-wide view of the link, for the UI and the tile. */
        private val _state = MutableStateFlow(LinkState.IDLE)
        val state: StateFlow<LinkState> = _state.asStateFlow()

        private val _snapshot = MutableStateFlow<Snapshot?>(null)
        val snapshot: StateFlow<Snapshot?> = _snapshot.asStateFlow()
    }
}
