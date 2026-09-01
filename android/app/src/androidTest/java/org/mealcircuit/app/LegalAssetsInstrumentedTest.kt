package org.mealcircuit.app

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class LegalAssetsInstrumentedTest {
    @Test
    fun legalDocumentsAreReadableFromTheInstalledApplication() {
        val assets = InstrumentationRegistry.getInstrumentation().targetContext.assets
        val expectedMarkers = mapOf(
            "legal/MEALCIRCUIT_LICENSE.txt" to "MIT License",
            "legal/THIRD_PARTY_NOTICES.md" to "Android third-party notices",
            "legal/APACHE-2.0.txt" to "END OF TERMS AND CONDITIONS",
            "legal/PRIVACY.md" to "MealCircuit has no telemetry",
            "legal/SECURITY.md" to "MealCircuit is local-first",
            "legal/DISCLAIMER.md" to "not a medical device",
        )

        assertEquals(
            expectedMarkers.keys.map { it.substringAfter("legal/") }.sorted(),
            assets.list("legal").orEmpty().sorted(),
        )
        expectedMarkers.forEach { (path, marker) ->
            val content = assets.open(path).bufferedReader(Charsets.UTF_8).use { it.readText() }
            assertTrue("$path is incomplete", content.contains(marker))
        }
    }
}
