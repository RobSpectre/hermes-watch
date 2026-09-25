package com.nousresearch.hermeswatch.tile

import androidx.wear.protolayout.ColorBuilders.argb
import androidx.wear.protolayout.DimensionBuilders.dp
import androidx.wear.protolayout.DimensionBuilders.expand
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.LayoutElementBuilders.Box
import androidx.wear.protolayout.LayoutElementBuilders.Column
import androidx.wear.protolayout.LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER
import androidx.wear.protolayout.LayoutElementBuilders.VERTICAL_ALIGN_CENTER
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.material.Text
import androidx.wear.protolayout.material.Typography
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import androidx.wear.tiles.TimelineBuilders
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.nousresearch.hermeswatch.data.Frame
import com.nousresearch.hermeswatch.data.Protocol
import com.nousresearch.hermeswatch.data.Snapshot
import com.nousresearch.hermeswatch.data.TileCache
import com.nousresearch.hermeswatch.data.toSnapshot

/**
 * A glanceable tile: agent state, throughput, context left, session tokens.
 *
 * The tile opens **no socket** and does not bind to the service — it renders the
 * last snapshot the service cached in [TileCache]. Tiles are drawn by the system
 * on its own schedule, so a tile that needed a live connection would show
 * nothing most of the time and would keep the radio awake trying. Everything
 * here is synchronous by necessity; the API hands back a `ListenableFuture`, and
 * the value is already on disk.
 *
 * The layout is built from raw protolayout elements rather than `PrimaryLayout`:
 * it needs no device parameters, which keeps this file independent of the
 * device-parameter type that differs between the `tiles` and `protolayout`
 * artifacts.
 */
class StatsTileService : TileService() {

    override fun onTileRequest(requestParams: RequestBuilders.TileRequest): ListenableFuture<TileBuilders.Tile> {
        val tile = TileBuilders.Tile.Builder()
            .setResourcesVersion(RESOURCES_VERSION)
            .setTileTimeline(
                TimelineBuilders.Timeline.fromLayoutElement(tileLayout(readSnapshot())),
            )
            .build()
        return Futures.immediateFuture(tile)
    }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest,
    ): ListenableFuture<ResourceBuilders.Resources> =
        Futures.immediateFuture(ResourceBuilders.Resources.Builder().setVersion(RESOURCES_VERSION).build())

    private fun readSnapshot(): Snapshot? {
        val json = TileCache.readJson(this) ?: return null
        return runCatching {
            Protocol.json.decodeFromString(Frame.serializer(), json).toSnapshot()
        }.getOrNull()
    }

    private fun tileLayout(snapshot: Snapshot?): LayoutElementBuilders.LayoutElement {
        val state = snapshot?.agentState ?: "offline"
        val stateColor = when (state) {
            "waiting_approval", "waiting_input" -> COLOR_ALERT
            "thinking", "tool" -> COLOR_ACTIVE
            else -> COLOR_MUTED
        }
        val rate = snapshot?.tokensPerSecondLive?.let { "%.1f tok/s".format(it) } ?: "— tok/s"
        val context = snapshot?.contextRemainingPercent?.let { "%.0f%% context".format(it) } ?: "context —"
        val tokens = snapshot?.sessionTokens?.let(::formatCompact) ?: "—"

        val column = Column.Builder()
            .setHorizontalAlignment(HORIZONTAL_ALIGN_CENTER)
            .addContent(
                Text.Builder(this, state.replace('_', ' '))
                    .setTypography(Typography.TYPOGRAPHY_TITLE3)
                    .setColor(argb(stateColor))
                    .build(),
            )
            .addContent(spacer(4f))
            .addContent(
                Text.Builder(this, rate)
                    .setTypography(Typography.TYPOGRAPHY_BODY1)
                    .build(),
            )
            .addContent(
                Text.Builder(this, context)
                    .setTypography(Typography.TYPOGRAPHY_BODY2)
                    .build(),
            )
            .addContent(
                Text.Builder(this, "$tokens this session")
                    .setTypography(Typography.TYPOGRAPHY_CAPTION1)
                    .build(),
            )
            .build()

        return Box.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .setVerticalAlignment(VERTICAL_ALIGN_CENTER)
            .addContent(column)
            .build()
    }

    private fun spacer(heightDp: Float): Box =
        Box.Builder().setWidth(dp(1f)).setHeight(dp(heightDp)).build()

    private fun formatCompact(value: Long): String = when {
        value >= 1_000_000 -> "%.1fM".format(value / 1_000_000.0)
        value >= 1_000 -> "%.0fk".format(value / 1_000.0)
        else -> value.toString()
    }

    companion object {
        private const val RESOURCES_VERSION = "1"
        private const val COLOR_ACTIVE = 0xFF5EE6C4.toInt()
        private const val COLOR_ALERT = 0xFFFF6B6B.toInt()
        private const val COLOR_MUTED = 0xFF9AA4B2.toInt()
    }
}
