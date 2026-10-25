package dev.homelabtv.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.SecureRandom
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class DeviceCredentialStore(context: Context) {
    private val preferences = context.getSharedPreferences("device_auth", Context.MODE_PRIVATE)

    @Synchronized
    fun deviceId(): String =
        preferences.getString(KEY_DEVICE_ID, null)
            ?: UUID.randomUUID().toString().also { id ->
                check(preferences.edit().putString(KEY_DEVICE_ID, id).commit()) {
                    "Could not persist the device ID"
                }
            }

    @Synchronized
    fun deviceSecret(): String =
        readEncrypted(KEY_DEVICE_SECRET) ?: ByteArray(32)
            .also(SecureRandom()::nextBytes)
            .let { Base64.encodeToString(it, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING) }
            .also { saveEncrypted(KEY_DEVICE_SECRET, it) }

    fun credential(): String? = readEncrypted(KEY_DEVICE_CREDENTIAL)

    fun saveCredential(credential: String) {
        require(credential.isNotBlank()) { "Device credential must not be empty" }
        saveEncrypted(KEY_DEVICE_CREDENTIAL, credential)
    }

    @Synchronized
    fun resetPairingSecret() {
        check(preferences.edit().remove(KEY_DEVICE_SECRET).remove(KEY_DEVICE_CREDENTIAL).commit()) {
            "Could not clear the stored device credentials"
        }
    }

    private fun saveEncrypted(key: String, value: String) {
        val cipher = Cipher.getInstance(CIPHER_TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
        val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        val encoded = Base64.encodeToString(
            cipher.iv + encrypted,
            Base64.NO_WRAP,
        )
        check(preferences.edit().putString(key, encoded).commit()) {
            "Could not persist encrypted device credentials"
        }
    }

    private fun readEncrypted(key: String): String? {
        val encoded = preferences.getString(key, null) ?: return null
        val combined = Base64.decode(encoded, Base64.NO_WRAP)
        require(combined.size > GCM_IV_LENGTH_BYTES) { "Stored device credential is invalid" }
        val iv = combined.copyOfRange(0, GCM_IV_LENGTH_BYTES)
        val ciphertext = combined.copyOfRange(GCM_IV_LENGTH_BYTES, combined.size)
        val cipher = Cipher.getInstance(CIPHER_TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(GCM_TAG_LENGTH_BITS, iv))
        return cipher.doFinal(ciphertext).toString(Charsets.UTF_8)
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEY_STORE).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEY_STORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    private companion object {
        const val ANDROID_KEY_STORE = "AndroidKeyStore"
        const val CIPHER_TRANSFORMATION = "AES/GCM/NoPadding"
        const val KEY_ALIAS = "homelabtv.device-credentials.v1"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_DEVICE_SECRET = "device_secret"
        const val KEY_DEVICE_CREDENTIAL = "device_credential"
        const val GCM_IV_LENGTH_BYTES = 12
        const val GCM_TAG_LENGTH_BITS = 128
    }
}
