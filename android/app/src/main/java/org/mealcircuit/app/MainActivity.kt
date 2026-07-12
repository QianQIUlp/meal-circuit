package org.mealcircuit.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.lifecycle.viewmodel.compose.viewModel
import org.mealcircuit.app.ui.MealCircuitApp
import org.mealcircuit.app.ui.MealCircuitTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            MealCircuitTheme {
                MealCircuitApp(viewModel())
            }
        }
    }
}
