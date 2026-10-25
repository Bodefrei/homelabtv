package dev.homelabtv.data

import android.content.Context
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext

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
                val guide = RetrofitClient.getInstance(serverUrl).getEnrichedGuide(authorization)
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
