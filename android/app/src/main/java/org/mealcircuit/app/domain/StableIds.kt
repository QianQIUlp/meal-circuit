package org.mealcircuit.app.domain

import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.UUID

private val MEALCIRCUIT_NAMESPACE = UUID.fromString("2a7c0c93-763f-4d3c-93f1-c8a5768da92a")

fun stableUuid(name: String): UUID {
    val namespace = ByteBuffer.allocate(16)
        .putLong(MEALCIRCUIT_NAMESPACE.mostSignificantBits)
        .putLong(MEALCIRCUIT_NAMESPACE.leastSignificantBits)
        .array()
    val digest = MessageDigest.getInstance("SHA-1").digest(namespace + name.toByteArray(Charsets.UTF_8))
    digest[6] = ((digest[6].toInt() and 0x0f) or 0x50).toByte()
    digest[8] = ((digest[8].toInt() and 0x3f) or 0x80).toByte()
    val bytes = ByteBuffer.wrap(digest.copyOf(16))
    return UUID(bytes.long, bytes.long)
}

fun preferenceId(kind: String) = "preferences_${stableUuid(kind)}"
fun taskInputId(taskId: String) = "task_input_${stableUuid(taskId)}"
