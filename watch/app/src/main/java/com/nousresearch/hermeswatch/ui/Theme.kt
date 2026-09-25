package com.nousresearch.hermeswatch.ui

import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.wear.compose.material.MaterialTheme

/**
 * The few colours this app attaches meaning to.
 *
 * Explicit constants rather than a themed palette: the point of the stats screen
 * is that the same state looks the same on every watch, so these must not come
 * from whatever colour scheme the system chose.
 */
object HermesColors {
    /** Healthy: the link is up and context has room. */
    val Active = Color(0xFF5EE6C4)

    /** Context is meaningfully consumed. */
    val Warning = Color(0xFFF5A623)

    /** Needs a human, or the context is nearly gone. */
    val Alert = Color(0xFFFF6B6B)

    /** Nothing to report. */
    val Muted = Color(0xFF9AA4B2)

    /** Body text on the app's dark background. */
    val Text = Color(0xFFE6EAF2)

    /** Background of a text field. */
    val Field = Color(0xFF232A38)
}

@Composable
fun HermesWatchTheme(content: @Composable () -> Unit) {
    // Default MaterialTheme colours only. The colours that carry meaning are set
    // explicitly where they are drawn, rather than relying on a themed `primary`
    // whose definition moves between wear-compose releases.
    MaterialTheme(content = content)
}
