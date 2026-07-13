package org.mealcircuit.app.io

import java.io.InputStream
import java.io.OutputStream

const val MAX_MANAGED_ASSET_BYTES = 10 * 1024 * 1024

/** API 26-compatible equivalent of InputStream.readNBytes(length). */
fun InputStream.readUpTo(length: Int): ByteArray {
    require(length >= 0)
    if (length == 0) return ByteArray(0)
    val buffer = ByteArray(length)
    var offset = 0
    while (offset < length) {
        val count = read(buffer, offset, length - offset)
        if (count < 0) break
        if (count == 0) {
            val single = read()
            if (single < 0) break
            buffer[offset++] = single.toByte()
        } else {
            offset += count
        }
    }
    return if (offset == length) buffer else buffer.copyOf(offset)
}

/** Read a user-controlled stream without allowing it to grow beyond the declared limit. */
fun InputStream.readBounded(maxBytes: Int): ByteArray {
    require(maxBytes >= 0 && maxBytes < Int.MAX_VALUE)
    val value = readUpTo(maxBytes + 1)
    require(value.size <= maxBytes) { "input exceeds $maxBytes bytes" }
    return value
}

fun InputStream.copyToBounded(output: OutputStream, maxBytes: Long): Long {
    require(maxBytes >= 0)
    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
    var total = 0L
    while (true) {
        val count = read(buffer)
        if (count < 0) return total
        if (count == 0) continue
        total += count
        require(total <= maxBytes) { "input exceeds $maxBytes bytes" }
        output.write(buffer, 0, count)
    }
}
