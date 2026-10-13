package com.intraforge.pay.pilot

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/** IntraForge brand tokens (DESIGN.md, dark scheme): deep navy canvas, raised surfaces, blue primary. */
private val colors = darkColorScheme(
    primary = Color(0xFF60A5FA),
    onPrimary = Color(0xFF0B0E24),
    background = Color(0xFF0B0E24),
    onBackground = Color(0xFFE8EAF6),
    surface = Color(0xFF151935),
    onSurface = Color(0xFFE8EAF6),
    onSurfaceVariant = Color(0xFFA9B0D0),
    outline = Color(0xFF39406B),
    error = Color(0xFFF87171),
)

@Composable
fun PilotTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = colors, content = content)
}
