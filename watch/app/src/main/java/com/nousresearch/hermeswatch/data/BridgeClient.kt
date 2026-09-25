package com.nousresearch.hermeswatch.data

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random

enum class LinkState { IDLE, CONNECTING, ONLINE, WAITING_RETRY, DISCONNECTED, ERROR }

/**
 * One WebSocket to the bridge, with reconnect, and nothing else.
 *
 * No business logic lives here: it decodes frames and emits them. The service
 * decides what an approval means, the UI decides what to draw.
 *
 * Reconnect uses exponential backoff with jitter (1 s → 60 s). Without jitter,
 * a watch and a phone that both reconnect on a bridge restart stay in lockstep
 * forever; with it, they de-synchronise in one or two attempts. There is no
 * separate "ping" loop: the bridge sends WebSocket pings and OkHttp answers
 * them, so an idle link costs nothing but the TCP keepalive.
 */
class BridgeClient(
    private val scope: CoroutineScope,
    private val label: String,
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // websockets: no read deadline
        .pingInterval(0, TimeUnit.MILLISECONDS) // the bridge pings, not us
        .retryOnConnectionFailure(true)
        .build()

    private val _frames = MutableSharedFlow<Frame>(
        replay = 0,
        extraBufferCapacity = 64,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    val frames: SharedFlow<Frame> = _frames.asSharedFlow()

    private val _state = MutableStateFlow(LinkState.IDLE)
    val state: StateFlow<LinkState> = _state.asStateFlow()

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError.asStateFlow()

    @Volatile
    private var socket: WebSocket? = null

    @Volatile
    private var lastHost: String = ""

    @Volatile
    private var lastPort: Int = 0

    @Volatile
    private var lastToken: String = ""

    @Volatile
    private var attempt = 0

    @Volatile
    private var closedByUs = false

    fun connect(host: String, port: Int, token: String) {
        closedByUs = false
        lastHost = host.trim()
        lastPort = port
        lastToken = token.trim()
        attempt = 0
        open(lastHost, lastPort, lastToken)
    }

    fun disconnect() {
        closedByUs = true
        socket?.close(1000, "client closed")
        socket = null
        _state.value = LinkState.DISCONNECTED
    }

    /** Send an approval decision. Returns false when there is no live socket. */
    fun answer(id: String, choice: String): Boolean =
        send(outboundAnswer(id, choice))

    fun requestSnapshot(): Boolean = send(outboundStatsRequest())

    private fun send(text: String): Boolean {
        val socket = this.socket ?: return false
        return socket.send(text)
    }

    private fun open(host: String, port: Int, token: String) {
        _state.value = LinkState.CONNECTING
        val request = Request.Builder()
            .url("ws://$host:$port/v1/watch?token=$token&device=$label")
            .build()
        socket = client.newWebSocket(request, Listener())
    }

    private fun scheduleReconnect() {
        if (closedByUs) return
        val delayMs = min(60_000L, (1_000L shl min(attempt, 6))) + Random.nextLong(0, 500)
        attempt += 1
        _state.value = LinkState.WAITING_RETRY
        scope.launch {
            delay(delayMs)
            if (!closedByUs && lastHost.isNotEmpty()) open(lastHost, lastPort, lastToken)
        }
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            attempt = 0
            _lastError.value = null
            _state.value = LinkState.ONLINE
            webSocket.send(outboundHello(label))
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val frame = runCatching { Protocol.json.decodeFromString(Frame.serializer(), text) }
                .getOrElse {
                    Log.w(TAG, "undecodable frame: ${text.take(120)}")
                    return
                }
            if (frame.isUnsupportedVersion()) {
                _lastError.value = "bridge speaks protocol ${frame.v}; this app speaks ${Protocol.VERSION}"
                _state.value = LinkState.ERROR
                return
            }
            if (frame.type == Protocol.TYPE_PING) {
                webSocket.send(outboundPong())
                return
            }
            _frames.tryEmit(frame)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            _lastError.value = t.message ?: t.javaClass.simpleName
            Log.w(TAG, "socket failed: ${t.message}")
            socket = null
            scheduleReconnect()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            socket = null
            if (closedByUs) {
                _state.value = LinkState.DISCONNECTED
            } else {
                scheduleReconnect()
            }
        }
    }

    companion object {
        private const val TAG = "BridgeClient"
    }
}
