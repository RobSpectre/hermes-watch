package com.nousresearch.hermeswatch.ui

import androidx.compose.runtime.Composable
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.darkColors
import androidx.compose.ui.graphics.Color

/**
 * A dark, high-contrast palette for quick glances outdoors. Deliberately not
 * dynamic-colour: a glanceable readout needs the same colour for "context is
 * running out" on every watch, rather than whatever wallpaper the user picked.
 */
private val HermesColors = darkColors(
    primary = Color(0xFF5EE6C4),
    onPrimary = Color(0xFF00201A),
    secondary = Color(0xFF9AA4B2),
    background = Color(0xFF0B0E14),
    onBackground = Color(0xFFE6EAF2),
    surface = Color(0xFF141924),
    onSurface = Color(0xFFE6EAF2),
    error = Color(0xFFFF6B6B),
)

@Composable
fun HermesWatchTheme(content: @Composable () -> Unit) {
    MaterialTheme(colors = HermesColors, content = content)
}
