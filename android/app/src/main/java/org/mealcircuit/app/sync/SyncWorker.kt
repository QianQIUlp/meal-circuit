package org.mealcircuit.app.sync

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OneTimeWorkRequest
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.time.Duration
import java.security.GeneralSecurityException
import kotlinx.serialization.SerializationException

enum class SyncFailureDisposition { RETRY, FAILURE }

fun syncFailureDisposition(error: Throwable): SyncFailureDisposition = when (error) {
    is IllegalArgumentException, is IllegalStateException, is SecurityException,
    is GeneralSecurityException, is SerializationException -> SyncFailureDisposition.FAILURE
    is SyncHttpException -> if (error.status in setOf(401, 403, 409, 426)) {
        SyncFailureDisposition.FAILURE
    } else {
        SyncFailureDisposition.RETRY
    }
    else -> SyncFailureDisposition.RETRY
}

class SyncWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val application = applicationContext as org.mealcircuit.app.MealCircuitApplication
        val engine = application.syncEngineOrNull() ?: return Result.success()
        return runCatching { engine.run() }.fold(
            onSuccess = { Result.success() },
            onFailure = { error -> if (syncFailureDisposition(error) == SyncFailureDisposition.RETRY) Result.retry() else Result.failure() },
        )
    }

    companion object {
        fun buildRequest(): OneTimeWorkRequest = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, Duration.ofSeconds(30))
                .build()

        fun enqueue(context: Context) {
            val request = buildRequest()
            WorkManager.getInstance(context).enqueueUniqueWork(
                "mealcircuit-sync",
                ExistingWorkPolicy.KEEP,
                request,
            )
        }
    }
}
