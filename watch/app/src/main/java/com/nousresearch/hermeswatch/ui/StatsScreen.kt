package com.nousresearch.hermeswatch.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.Card
import androidx.wear.compose.material.Chip
import androidx.wear.compose.material.ChipDefaults
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.PositionIndicator
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText
import com.nousresearch.hermeswatch.data.BridgeSettings
import com.nousresearch.hermeswatch.data.LinkState
import com.nousresearch.hermeswatch.data.PendingRequest
import com.nousresearch.hermeswatch.data.Snapshot
import com.nousresearch.hermeswatch.service.WatchLinkService

/**
 * The live readout.
 *
 * Layout order is deliberate: **what needs you** first (an unanswered approval),
 * then the state line, then the throughput numbers, then the context bar, then
 * the slower-moving history. Anything not currently known renders as an em dash
 * rather than a zero, because "0 tok/s" and "we don't know" are different
 * statements and the watch must not conflate them.
 */
@Composable
fun StatsScreen(
    settings: BridgeSettings,
    onEditPairing: () -> Unit,
) {
    val snapshot by WatchLinkService.snapshot.collectAsState()
    val linkState by WatchLinkService.state.collectAsState()
    val context = LocalContext.current
    StatsScreenContent(
        snapshot = snapshot,
        linkState = linkState,
        settings = settings,
        onEditPairing = onEditPairing,
        onAnswer = { id, choice -> WatchLinkService.answer(context, id, choice) },
    )
}

@Composable
fun StatsScreenContent(
    snapshot: Snapshot?,
    linkState: LinkState,
    settings: BridgeSettings,
    onEditPairing: () -> Unit,
    onAnswer: (String, String) -> Unit,
) {
    val listState = rememberScalingLazyListState()
    val approval = snapshot?.pendingApprovals?.firstOrNull { it.kind == "approval" }

    Scaffold(
        timeText = { TimeText() },
        positionIndicator = { PositionIndicator(scalingLazyListState = listState) },
    ) {
        ScalingLazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize(),
            verticalArrangement = Arrangement.spacedBy(4.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            if (approval != null) {
                item { ApprovalCard(approval, onAnswer) }
            }

            item {
                Text(
                    text = snapshot?.sessionTitle ?: "Hermes",
                    style = MaterialTheme.typography.title3,
                    textAlign = TextAlign.Center,
                )
            }

            item {
                Text(
                    text = when (linkState) {
                        LinkState.ONLINE -> agentStateLabel(
                            snapshot?.agentState ?: "idle",
                            snapshot?.lastTool,
                        )
                        LinkState.CONNECTING -> "connecting…"
                        LinkState.WAITING_RETRY -> "reconnecting…"
                        LinkState.ERROR -> "bridge too new"
                        else -> "not connected"
                    },
                    style = MaterialTheme.typography.body1,
                    textAlign = TextAlign.Center,
                )
            }

            item {
                Text(
                    text = snapshot?.model ?: settings.host,
                    style = MaterialTheme.typography.caption1,
                    textAlign = TextAlign.Center,
                )
            }

            item {
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Metric("tok/s", formatRate(snapshot?.tokensPerSecondLive))
                    Metric("ctx", formatPercent(snapshot?.contextRemainingPercent))
                    Metric("time", formatDuration(snapshot?.elapsedSeconds))
                }
            }

            item { ContextBar(snapshot?.contextRemainingPercent, snapshot?.contextSource) }

            item {
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Metric("session", formatTokens(snapshot?.sessionTokens))
                    Metric("30d", formatTokens(snapshot?.usage30dTokens))
                }
            }

            item {
                Text(
                    text = buildString {
                        append("in ${formatTokens(snapshot?.inputTokens)}")
                        append(" · out ${formatTokens(snapshot?.outputTokens)}")
                        append(" · cached ${formatTokens(snapshot?.cachedTokens)}")
                    },
                    style = MaterialTheme.typography.caption1,
                    textAlign = TextAlign.Center,
                )
            }

            item {
                Text(
                    text = "${snapshot?.apiCalls ?: 0} calls · ${snapshot?.toolCalls ?: 0} tools" +
                        snapshot?.usage30dCostUsd?.takeIf { it > 0 }?.let { " · ${formatUsd(it)} 30d" }.orEmpty(),
                    style = MaterialTheme.typography.caption1,
                    textAlign = TextAlign.Center,
                )
            }

            item {
                Chip(
                    label = { Text("Bridge ${settings.host}:${settings.port}") },
                    onClick = onEditPairing,
                    colors = ChipDefaults.secondaryChipColors(),
                )
            }
        }
    }
}

@Composable
private fun Metric(label: String, value: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(text = value, style = MaterialTheme.typography.title2)
        Text(text = label, style = MaterialTheme.typography.caption2)
    }
}

/**
 * Context usage as a bar that fills as the window empties. Colour is the only
 * signal that changes at a glance: green, amber under 25% left, red under 10%.
 */
@Composable
private fun ContextBar(remainingPercent: Double?, source: String?) {
    val known = remainingPercent != null
    val fraction = ((remainingPercent ?: 0.0) / 100.0).coerceIn(0.0, 1.0).toFloat()
    val color = when {
        !known -> MaterialTheme.colors.onSurface.copy(alpha = 0.3f)
        remainingPercent!! < 10 -> MaterialTheme.colors.error
        remainingPercent < 25 -> Color(0xFFF5A623)
        else -> MaterialTheme.colors.primary
    }
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp),
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(6.dp)
                .clip(RoundedCornerShape(3.dp))
                .background(MaterialTheme.colors.onSurface.copy(alpha = 0.15f)),
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth(fraction)
                    .height(6.dp)
                    .clip(RoundedCornerShape(3.dp))
                    .background(color),
            )
        }
        Text(
            // Naming the source keeps the estimate honest on the screen itself.
            text = if (!known) "context window unknown"
            else "context left" + when (source) {
                "db_estimate" -> " (est.)"
                "live_approximate" -> " (approx.)"
                else -> ""
            },
            style = MaterialTheme.typography.caption2,
        )
    }
}

/**
 * The one interactive element: the same choices the host offered, no more.
 * Buttons come straight from `request.choices`, so a once-only approval cannot
 * be shown an "always" button.
 */
@Composable
private fun ApprovalCard(request: PendingRequest, onAnswer: (String, String) -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp),
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            // Colour carries the urgency; the Card itself keeps the theme's
            // surface so the text stays legible on every watch face.
            Text(
                "Needs approval",
                style = MaterialTheme.typography.title3,
                color = MaterialTheme.colors.error,
            )
            request.command?.let {
                Text(it, style = MaterialTheme.typography.body2, textAlign = TextAlign.Center)
            }
            request.description?.takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.caption1, textAlign = TextAlign.Center)
            }
            request.remainingSeconds?.let {
                Text("${it.toInt()}s left", style = MaterialTheme.typography.caption2)
            }
            request.choices.forEach { choice ->
                Chip(
                    label = { Text(choiceLabel(choice)) },
                    onClick = { onAnswer(request.id, choice) },
                    colors = if (choice == "deny") ChipDefaults.secondaryChipColors()
                    else ChipDefaults.primaryChipColors(),
                )
            }
        }
    }
}

private fun choiceLabel(choice: String): String = when (choice) {
    "once" -> "Allow once"
    "session" -> "Allow session"
    "always" -> "Always"
    else -> "Deny"
}
