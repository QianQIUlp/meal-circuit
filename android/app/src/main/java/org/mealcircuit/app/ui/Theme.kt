package org.mealcircuit.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val LightColors = lightColorScheme(
    primary = Color(0xFF086B59),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFC7F1E4),
    onPrimaryContainer = Color(0xFF002018),
    secondary = Color(0xFF4B635B),
    background = Color(0xFFF7F9F7),
    surface = Color(0xFFFBFDFB),
    surfaceVariant = Color(0xFFE1E9E5),
    outline = Color(0xFF707975),
    error = Color(0xFFBA1A1A),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF8BD5C0),
    onPrimary = Color(0xFF00382D),
    primaryContainer = Color(0xFF005142),
    onPrimaryContainer = Color(0xFFA8F2DA),
    secondary = Color(0xFFB3CCC3),
    background = Color(0xFF101513),
    surface = Color(0xFF121816),
    surfaceVariant = Color(0xFF3F4945),
    outline = Color(0xFF89938F),
    error = Color(0xFFFFB4AB),
)

@Composable
fun MealCircuitTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors, content = content)
}
