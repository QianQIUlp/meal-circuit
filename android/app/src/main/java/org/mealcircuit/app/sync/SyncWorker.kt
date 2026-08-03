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
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.SerializationException

enum class SyncFailureDisposition { RETRY, FAILURE }

internal val SYNC_EXISTING_WORK_POLICY = ExistingWorkPolicy.APPEND_OR_REPLACE

fun syncFailureDisposition(error: Throwable): SyncFailureDisposition = when (error) {
    is IllegalArgumentException, is IllegalStateException, is SecurityException,
    is GeneralSecurityException, is SerializationException, is PermanentAssetException ->
        SyncFailureDisposition.FAILURE
    is SyncHttpException -> if (error.status in setOf(408, 425, 429) || error.status >= 500) {
        SyncFailureDisposition.RETRY
    } else SyncFailureDisposition.FAILURE
    is IOException -> SyncFailureDisposition.RETRY
    else -> SyncFailureDisposition.FAILURE
}

class SyncWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val application = applicationContext as org.mealcircuit.app.MealCircuitApplication
        return try {
            val summary = application.runSync() ?: return Result.success()
            if (summary.deferredAssetTransfer) enqueueUnmeteredFollowUp(application)
            when {
                summary.transientAssetFailures > 0 -> Result.retry()
                summary.permanentAssetFailures > 0 -> Result.failure()
                else -> Result.success()
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Throwable) {
            if (syncFailureDisposition(error) == SyncFailureDisposition.RETRY) Result.retry() else Result.failure()
        }
    }

    companion object {
        fun buildRequest(networkType: NetworkType = NetworkType.CONNECTED): OneTimeWorkRequest =
            OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(networkType).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, Duration.ofSeconds(30))
                .build()

        fun enqueue(context: Context) {
            val request = buildRequest()
            WorkManager.getInstance(context).enqueueUniqueWork(
                "mealcircuit-sync",
                SYNC_EXISTING_WORK_POLICY,
                request,
            )
        }

        fun enqueueUnmeteredFollowUp(context: Context) {
            val request = buildRequest(NetworkType.UNMETERED)
            WorkManager.getInstance(context).enqueueUniqueWork(
                "mealcircuit-sync-unmetered",
                ExistingWorkPolicy.REPLACE,
                request,
            )
        }
    }
}
