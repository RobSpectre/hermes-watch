package com.nousresearch.hermeswatch.ui

/** Formatting helpers. Every one of them renders an em dash for "not known". */

const val DASH = "—"

fun formatTokens(tokens: Long?): String {
    if (tokens == null) return DASH
    return when {
        tokens >= 1_000_000 -> "%.1fM".format(tokens / 1_000_000.0)
        tokens >= 1_000 -> "%.1fk".format(tokens / 1_000.0)
        else -> tokens.toString()
    }
}

fun formatRate(tokensPerSecond: Double?): String =
    if (tokensPerSecond == null) DASH else "%.1f".format(tokensPerSecond)

fun formatPercent(percent: Double?): String =
    if (percent == null) DASH else "%.0f%%".format(percent)

fun formatUsd(amount: Double?): String =
    if (amount == null || amount <= 0.0) DASH else "$%.2f".format(amount)

fun formatDuration(seconds: Double?): String {
    if (seconds == null) return DASH
    val total = seconds.toLong()
    return when {
        total < 60 -> "${total}s"
        total < 3600 -> "${total / 60}m"
        else -> "${total / 3600}h${(total % 3600) / 60}m"
    }
}

/**
 * The state line the user actually reads. "tool: terminal" is more useful than
 * "thinking", so the tool name wins when there is one.
 */
fun agentStateLabel(state: String, lastTool: String?): String = when (state) {
    "thinking" -> "thinking…"
    "tool" -> lastTool?.let { "running $it" } ?: "running a tool"
    "waiting_approval" -> "waiting for you"
    "waiting_input" -> "asked you a question"
    "idle" -> "idle"
    "offline" -> "bridge unreachable"
    else -> state
}
