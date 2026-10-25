package dev.homelabtv.data

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Body
import retrofit2.http.Header
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query

data class PairingStartRequest(
    val device_id: String,
    val device_secret: String,
    val friendly_name: String,
    val app_version: String,
    val android_version: String,
    val manufacturer: String,
    val model: String,
)

data class PairingStartResponse(
    val device_id: String,
    val user_code: String,
    val expires_at: Long,
    val expires_in_seconds: Int,
)

data class PairingPollRequest(val device_secret: String)

data class PairingPollResponse(
    val status: String,
    val device_credential: String?,
)

data class RegisteredDevice(
    val id: String,
    val friendly_name: String,
    val pairing_status: String,
)

data class DeviceChannelInventoryEntry(
    val channel_id: Long,
    val input_id: String?,
    val display_number: String,
    val display_name: String,
    val original_display_name: String?,
    val service_type: String?,
    val channel_type: String?,
    val network_id: Long?,
    val transport_stream_id: Long?,
    val service_id: Long?,
    val internal_provider_id: String?,
)

data class DeviceChannelInventoryRequest(
    val inventory_hash: String,
    val channels: List<DeviceChannelInventoryEntry>,
)

data class DeviceChannelInventoryResponse(
    val changed: Boolean,
    val inventory_hash: String,
    val revision: Int,
    val channel_count: Int,
)

data class DeviceChannelListChannel(
    val logical_channel_id: String,
    val display_name: String?,
    val list_number: Int? = null,
    val channel_id: Long?,
    val display_number: String?,
    val device_channel_name: String?,
    val available: Boolean,
)

data class DeviceChannelListResponse(
    val active: Boolean,
    val list_id: String?,
    val name: String?,
    val channels: List<DeviceChannelListChannel>,
)

data class DeviceChannelListSummary(
    val id: String,
    val name: String,
    val entry_count: Int,
    val assigned: Boolean,
)

fun calculateDeviceChannelInventoryHash(channels: List<DeviceChannelInventoryEntry>): String {
    val bytes = ByteArrayOutputStream()
    for (channel in channels.sortedBy { it.channel_id }) {
        val values = listOf(
            channel.channel_id.toString(),
            channel.input_id,
            channel.display_number,
            channel.display_name,
            channel.original_display_name,
            channel.service_type,
            channel.channel_type,
            channel.network_id?.toString(),
            channel.transport_stream_id?.toString(),
            channel.service_id?.toString(),
            channel.internal_provider_id,
        )
        for (value in values) {
            if (value == null) {
                bytes.write(ByteBuffer.allocate(Int.SIZE_BYTES).putInt(-1).array())
            } else {
                val encoded = value.toByteArray(Charsets.UTF_8)
                bytes.write(ByteBuffer.allocate(Int.SIZE_BYTES).putInt(encoded.size).array())
                bytes.write(encoded)
            }
        }
    }
    return MessageDigest.getInstance("SHA-256").digest(bytes.toByteArray())
        .joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
}

interface HomelabApiService {
    @GET("api/guide")
    suspend fun getEnrichedGuide(@Header("Authorization") authorization: String?): List<ChannelGuide>

    @POST("api/v1/devices/pair")
    suspend fun startPairing(@Body request: PairingStartRequest): PairingStartResponse

    @POST("api/v1/devices/{device_id}/poll")
    suspend fun pollPairing(
        @Path("device_id") deviceId: String,
        @Body request: PairingPollRequest,
    ): PairingPollResponse

    @GET("api/v1/devices/me")
    suspend fun getCurrentDevice(@Header("Authorization") authorization: String): RegisteredDevice

    @GET("api/v1/devices/me/channel-lists")
    suspend fun getCurrentDeviceChannelLists(
        @Header("Authorization") authorization: String,
    ): List<DeviceChannelListSummary>

    @GET("api/v1/devices/me/channel-list")
    suspend fun getCurrentDeviceChannelList(
        @Header("Authorization") authorization: String,
        @Query("list_id") listId: String? = null,
    ): DeviceChannelListResponse

    @PUT("api/v1/devices/me/channels")
    suspend fun syncDeviceChannels(
        @Header("Authorization") authorization: String,
        @Body request: DeviceChannelInventoryRequest,
    ): DeviceChannelInventoryResponse
}

object RetrofitClient {
    private var service: HomelabApiService? = null
    private var currentUrl: String = ""

    // The first guide build after fresh XML data can take minutes while the
    // server enriches new titles against TMDB — don't flag it offline for that.
    private val httpClient =
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(5, TimeUnit.MINUTES)
            .callTimeout(6, TimeUnit.MINUTES)
            .followSslRedirects(false)
            .build()

    fun getInstance(baseUrl: String): HomelabApiService {
        val url = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        val cached = service
        if (cached == null || currentUrl != url) {
            currentUrl = url
            service =
                Retrofit.Builder()
                    .baseUrl(url)
                    .client(httpClient)
                    .addConverterFactory(GsonConverterFactory.create())
                    .build()
                    .create(HomelabApiService::class.java)
        }
        return service!!
    }
}
