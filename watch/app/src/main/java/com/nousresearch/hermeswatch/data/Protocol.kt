package com.nousresearch.hermeswatch.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

/**
 * The watch's view of the bridge protocol, mirroring `docs/protocol.md` and
 * `bridge/hermes_watch/protocol.py`.
 *
 * Parsing is deliberately shape-tolerant: frames arrive as a tagged union whose
 * payload varies by event, so this file parses one permissive [Frame] type and
 * exposes typed accessors that return `null` for anything absent. The client
 * renders a dash for `null` — never a zero — so a missing field can never be
 * mistaken for a real measurement.
 */
object Protocol {
    const val VERSION = 1

    const val TYPE_HELLO = "hello"
    const val TYPE_SNAPSHOT = "snapshot"
    const val TYPE_EVENT = "event"
    const val TYPE_STATS = "stats"
    const val TYPE_PING = "ping"
    const val TYPE_ERROR = "error"

    const val CLIENT_HELLO = "hello"
    const val CLIENT_ANSWER = "answer"
    const val CLIENT_STATS_REQUEST = "stats.request"
    const val CLIENT_PONG = "pong"

    const val EVENT_APPROVAL_REQUESTED = "approval.requested"
    const val EVENT_APPROVAL_RESOLVED = "approval.resolved"
    const val EVENT_QUESTION_PENDING = "question.pending"
    const val EVENT_QUESTION_RESOLVED = "question.resolved"
    const val EVENT_TURN_STARTED = "turn.started"
    const val EVENT_TURN_ENDED = "turn.ended"
    const val EVENT_TOOL_STARTED = "tool.started"
    const val EVENT_TOOL_FINISHED = "tool.finished"
    const val EVENT_SESSION_STARTED = "session.started"
    const val EVENT_SESSION_ENDED = "session.ended"
    const val EVENT_LOOP_STOPPED = "loop.stopped"
    /** Plain text pushed to the wrist: `hermes send`, cron delivery, the
     *  agent's send_message tool. Additive within v1 — an older client
     *  ignores an event it does not know. */
    const val EVENT_MESSAGE = "message"

    val CHOICES = listOf("once", "session", "always", "deny")

    val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        explicitNulls = false
    }
}

@Serializable
data class Frame(
    val v: Int = Protocol.VERSION,
    val type: String = "",
    val event: String? = null,
    val id: String? = null,
    val error: String? = null,
    val payload: JsonObject? = null,
    // handshake
    @SerialName("bridge_version") val bridgeVersion: String? = null,
    val protocol: Int? = null,
    val profile: String? = null,
    @SerialName("server_time") val serverTime: Double? = null,
    // snapshot / stats
    val ts: Double? = null,
    val session: JsonObject? = null,
    val context: JsonObject? = null,
    val usage: JsonObject? = null,
    val live: JsonObject? = null,
    val pending: List<JsonObject>? = null,
    val bridge: JsonObject? = null,
)

/** Everything the UI renders, flattened out of a snapshot frame. */
data class Snapshot(
    val sessionTitle: String?,
    val model: String?,
    val sessionTokens: Long?,
    val inputTokens: Long?,
    val outputTokens: Long?,
    val cachedTokens: Long?,
    val apiCalls: Int?,
    val toolCalls: Int?,
    val elapsedSeconds: Double?,
    val tokensPerSecondLive: Double?,
    val tokensPerSecondSessionAvg: Double?,
    val contextRemainingPercent: Double?,
    val contextUsedTokens: Long?,
    val contextWindowTokens: Long?,
    val contextSource: String?,
    val usage30dTokens: Long?,
    val usage30dSessions: Int?,
    val usage30dCostUsd: Double?,
    val agentState: String,
    val lastTool: String?,
    val pendingApprovals: List<PendingRequest>,
    val receivedAt: Long = System.currentTimeMillis(),
)

data class PendingRequest(
    val id: String,
    val kind: String,
    val command: String?,
    val description: String?,
    val remainingSeconds: Double?,
    val choices: List<String>,
)

/** Nullable numeric helpers: absent stays absent. */
private fun JsonObject.long(key: String): Long? = this[key]?.jsonPrimitive?.longOrNull
private fun JsonObject.int(key: String): Int? = this[key]?.jsonPrimitive?.intOrNull
private fun JsonObject.dbl(key: String): Double? = this[key]?.jsonPrimitive?.doubleOrNull
private fun JsonObject.str(key: String): String? =
    this[key]?.let { if (it is JsonObject) null else it.jsonPrimitive.content }

private fun JsonElement?.obj(): JsonObject? = (this as? JsonObject)

fun Frame.toSnapshot(): Snapshot? {
    val sessionObj = session ?: return null
    val tokens = sessionObj["tokens"].obj()
    val rate = sessionObj["tok_per_s"].obj()
    val window = context
    val usageObj = usage
    return Snapshot(
        sessionTitle = sessionObj.str("title"),
        model = sessionObj.str("model"),
        sessionTokens = tokens?.long("total"),
        inputTokens = tokens?.long("input"),
        outputTokens = tokens?.long("output"),
        cachedTokens = tokens?.long("cached_read"),
        apiCalls = sessionObj.int("api_call_count"),
        toolCalls = sessionObj.int("tool_call_count"),
        elapsedSeconds = sessionObj.dbl("elapsed_s"),
        tokensPerSecondLive = rate?.dbl("live"),
        tokensPerSecondSessionAvg = rate?.dbl("session_avg"),
        contextRemainingPercent = window?.dbl("remaining_pct"),
        contextUsedTokens = window?.long("used_tokens"),
        contextWindowTokens = window?.long("window_tokens"),
        contextSource = window?.str("source"),
        usage30dTokens = usageObj?.get("tokens").obj()?.long("total"),
        usage30dSessions = usageObj?.int("session_count"),
        usage30dCostUsd = usageObj?.dbl("cost_usd"),
        agentState = live?.str("agent_state") ?: "offline",
        lastTool = live?.str("last_tool"),
        pendingApprovals = (pending ?: emptyList()).mapNotNull { it.toPendingRequest() },
    )
}

private fun JsonObject.toPendingRequest(): PendingRequest? {
    val id = str("id") ?: return null
    // `payload` is the *outer* frame's field; inside this receiver it has to be
    // looked up explicitly.
    val body = this["payload"].obj()
    val choices = this["choices"]?.let { element ->
        runCatching { element.jsonArray.map { it.jsonPrimitive.content } }.getOrNull()
    } ?: emptyList()
    return PendingRequest(
        id = id,
        kind = str("kind") ?: "approval",
        command = body?.str("command"),
        description = body?.str("description"),
        remainingSeconds = dbl("remaining_s"),
        choices = choices,
    )
}

/** An approval prompt lifted out of an `approval.requested` event. */
fun Frame.toPendingRequest(): PendingRequest? = payload?.toPendingRequest()?.copy(id = id ?: "")

/** True when the bridge is newer than this client understands. */
fun Frame.isUnsupportedVersion(): Boolean = v > Protocol.VERSION

// --- outbound frames --------------------------------------------------------
// Built as JSON objects rather than through [Frame]: outgoing frames carry
// fields (`choice`, `label`, `session_id`) that no inbound frame declares, and
// inventing them on the shared model would make the tolerant parser tolerant of
// its own bugs.

private fun outbound(type: String, vararg fields: Pair<String, String>): String =
    buildJsonObject {
        put("v", Protocol.VERSION)
        put("type", type)
        fields.forEach { (key, value) -> put(key, value) }
    }.toString()

fun outboundAnswer(id: String, choice: String): String =
    outbound(Protocol.CLIENT_ANSWER, "id" to id, "choice" to choice)

fun outboundHello(label: String): String = outbound(Protocol.CLIENT_HELLO, "label" to label)

fun outboundPong(): String = outbound(Protocol.CLIENT_PONG)

fun outboundStatsRequest(): String = outbound(Protocol.CLIENT_STATS_REQUEST)
