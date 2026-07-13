package org.mealcircuit.app.sync

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class SecretVault(context: Context) {
    private val preferences = context.getSharedPreferences("wrapped_secrets", Context.MODE_PRIVATE)
    private val alias = "MealCircuit.wrap.v1"

    private fun wrappingKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true)
                    .build()
            )
            generateKey()
        }
    }

    fun put(name: String, value: ByteArray) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, wrappingKey())
        cipher.updateAAD(aad(name))
        val wrapped = cipher.iv + cipher.doFinal(value)
        check(preferences.edit().putString(name, Base64.getEncoder().encodeToString(wrapped)).commit()) {
            "Unable to persist wrapped secret"
        }
    }

    fun get(name: String): ByteArray? {
        val encoded = preferences.getString(name, null) ?: return null
        return runCatching {
            val wrapped = Base64.getDecoder().decode(encoded)
            Cipher.getInstance("AES/GCM/NoPadding").run {
                init(Cipher.DECRYPT_MODE, wrappingKey(), GCMParameterSpec(128, wrapped.copyOfRange(0, 12)))
                updateAAD(aad(name))
                doFinal(wrapped.copyOfRange(12, wrapped.size))
            }
        }.getOrNull()
    }

    fun delete(name: String) {
        check(preferences.edit().remove(name).commit()) { "Unable to delete wrapped secret" }
    }

    private fun aad(name: String) = "MealCircuit Secret v1\u0000$name".toByteArray()
}
