package org.mealcircuit.app.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.AddCircle
import androidx.compose.material.icons.outlined.Home
import androidx.compose.material.icons.outlined.Inventory2
import androidx.compose.material.icons.outlined.MoreHoriz
import androidx.compose.material.icons.outlined.Today
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import org.mealcircuit.app.MainViewModel

enum class Destination(val label: String, val icon: ImageVector) {
    HOME("总览", Icons.Outlined.Home),
    DAILY("今日", Icons.Outlined.Today),
    CAPTURE("记录", Icons.Outlined.AddCircle),
    LIBRARY("食品库", Icons.Outlined.Inventory2),
    MORE("更多", Icons.Outlined.MoreHoriz),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MealCircuitApp(viewModel: MainViewModel) {
    var destination by rememberSaveable { mutableStateOf(Destination.HOME) }
    val host = remember { SnackbarHostState() }
    val message by viewModel.message.collectAsState()
    LaunchedEffect(message) {
        message?.let {
            host.showSnackbar(it.text)
            viewModel.dismissMessage()
        }
    }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val expanded = maxWidth >= 720.dp
        Scaffold(
            topBar = { TopAppBar(title = { Text(destination.label) }) },
            snackbarHost = { SnackbarHost(host) },
            bottomBar = {
                if (!expanded) NavigationBar {
                    Destination.entries.forEach { item ->
                        NavigationBarItem(
                            selected = item == destination,
                            onClick = { destination = item },
                            icon = { Icon(item.icon, contentDescription = null) },
                            label = { Text(item.label) },
                        )
                    }
                }
            },
        ) { padding ->
            Row(Modifier.fillMaxSize().padding(padding)) {
                if (expanded) NavigationRail {
                    Destination.entries.forEach { item ->
                        NavigationRailItem(
                            selected = item == destination,
                            onClick = { destination = item },
                            icon = { Icon(item.icon, contentDescription = null) },
                            label = { Text(item.label) },
                        )
                    }
                }
                Box(Modifier.weight(1f)) {
                    when (destination) {
                        Destination.HOME -> HomeScreen(viewModel)
                        Destination.DAILY -> DailyScreen(viewModel)
                        Destination.CAPTURE -> CaptureScreen(viewModel)
                        Destination.LIBRARY -> FoodLibraryScreen(viewModel)
                        Destination.MORE -> MoreScreen(viewModel)
                    }
                }
            }
        }
    }
}
