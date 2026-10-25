package dev.homelabtv.data

import android.content.Context
import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

data class GuideState(
    val channels: List<ChannelGuide> = emptyList(),
    val isOnline: Boolean = false,
    val lastUpdatedMillis: Long? = null,
    val hasLoadedCache: Boolean = false,
)

/**
 * Offline-first guide source: the UI always renders whatever is in [state],
 * which is seeded from the on-disk cache at startup and silently replaced
 * whenever a server refresh succeeds. A dead server only flips [GuideState.isOnline].
 */
class GuideRepository(context: Context) {
    private val gson = Gson()
    private val cacheFile = File(context.filesDir, "guide_cache.json")
    private val logoCacheDir = File(context.filesDir, "channel_logos")
    private val imageClient =
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .callTimeout(30, TimeUnit.SECONDS)
            .build()

    companion object {
        private const val TAG = "GuideRepository"
        private const val MAX_LOGO_BYTES = 10L * 1024 * 1024
        private val LOGO_EXTENSIONS = listOf(".jpg", ".png", ".webp", ".gif", ".svg")
    }

    private val _state = MutableStateFlow(GuideState())
    val state: StateFlow<GuideState> = _state

    suspend fun loadCache() =
        withContext(Dispatchers.IO) {
            val cached = readCache()
            _state.value =
                _state.value.copy(
                    channels = cached ?: emptyList(),
                    lastUpdatedMillis = if (cached != null) cacheFile.lastModified() else null,
                    hasLoadedCache = true,
                )
        }

    suspend fun refresh(serverUrl: String, deviceCredential: String? = null): Boolean =
        withContext(Dispatchers.IO) {
            try {
                val authorization = deviceCredential?.let { "Bearer $it" }
                val serverGuide = RetrofitClient.getInstance(serverUrl).getEnrichedGuide(authorization)
                val guide = cacheChannelLogos(serverGuide)
                cacheFile.writeText(gson.toJson(guide))
                _state.value =
                    _state.value.copy(
                        channels = guide,
                        isOnline = true,
                        lastUpdatedMillis = System.currentTimeMillis(),
                    )
                true
            } catch (e: Exception) {
                // Server down or bad URL: keep serving the cached guide
                _state.value = _state.value.copy(isOnline = false)
                false
            }
        }

    private suspend fun cacheChannelLogos(channels: List<ChannelGuide>): List<ChannelGuide> =
        coroutineScope {
            val semaphore = Semaphore(6)
            val logoUrls =
                channels.mapNotNull { it.logo_url }
                    .filter { it.isNotBlank() && !it.startsWith("file:") }
                    .distinct()
            val cachedLogos =
                logoUrls.map { url ->
                    async { url to semaphore.withPermit { downloadLogo(url) } }
                }.awaitAll().toMap()
            channels.map { channel ->
                val url = channel.logo_url
                if (url == null || url.startsWith("file:")) {
                    channel
                } else {
                    channel.copy(logo_url = cachedLogos[url] ?: url)
                }
            }
        }

    private fun downloadLogo(url: String): String? {
        if (!url.startsWith("http://") && !url.startsWith("https://")) return null
        if (!logoCacheDir.exists() && !logoCacheDir.mkdirs()) {
            Log.w(TAG, "Could not create channel logo cache directory")
            return null
        }

        val key =
            MessageDigest.getInstance("SHA-256")
                .digest(url.toByteArray(Charsets.UTF_8))
                .joinToString("") { "%02x".format(it) }
        LOGO_EXTENSIONS.forEach { extension ->
            val cached = File(logoCacheDir, "$key$extension")
            if (cached.isFile) return cached.toURI().toString()
        }

        val temporary = File(logoCacheDir, "$key.tmp")
        return try {
            val request = Request.Builder().url(url).build()
            imageClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Could not download channel logo: HTTP ${response.code}")
                    return null
                }
                val body = response.body ?: return null
                if (body.contentLength() > MAX_LOGO_BYTES) {
                    Log.w(TAG, "Channel logo exceeds the cache size limit")
                    return null
                }
                val extension =
                    when (body.contentType()?.subtype?.lowercase()) {
                        "jpeg", "jpg" -> ".jpg"
                        "png" -> ".png"
                        "webp" -> ".webp"
                        "gif" -> ".gif"
                        "svg+xml", "svg" -> ".svg"
                        else -> {
                            Log.w(TAG, "Unsupported channel logo content type: ${body.contentType()}")
                            return null
                        }
                    }
                var totalBytes = 0L
                body.byteStream().use { input ->
                    FileOutputStream(temporary).use { output ->
                        val buffer = ByteArray(8192)
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            totalBytes += count
                            if (totalBytes > MAX_LOGO_BYTES) {
                                Log.w(TAG, "Channel logo exceeds the cache size limit")
                                return null
                            }
                            output.write(buffer, 0, count)
                        }
                    }
                }
                if (totalBytes == 0L) {
                    Log.w(TAG, "Received an empty channel logo")
                    return null
                }
                val cached = File(logoCacheDir, "$key$extension")
                if (!temporary.renameTo(cached)) {
                    Log.w(TAG, "Could not store downloaded channel logo")
                    return null
                }
                cached.toURI().toString()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not download channel logo", e)
            null
        } finally {
            if (temporary.exists()) temporary.delete()
        }
    }

    private fun readCache(): List<ChannelGuide>? {
        if (!cacheFile.exists()) return null
        return try {
            val type = object : TypeToken<List<ChannelGuide>>() {}.type
            gson.fromJson(cacheFile.readText(), type)
        } catch (e: Exception) {
            null
        }
    }
}
