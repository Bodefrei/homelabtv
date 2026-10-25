package dev.homelabtv.data

import android.content.Context
import android.media.tv.TvContract
import android.media.tv.TvInputInfo
import android.media.tv.TvInputManager
import android.net.Uri

data class PhysicalChannel(
    val id: Long,
    val displayName: String,
    val displayNumber: String,
    val inputId: String?,
    val uri: Uri,
    val originalDisplayName: String? = null,
    val serviceType: String? = null,
    val channelType: String? = null,
    val networkId: Long? = null,
    val transportStreamId: Long? = null,
    val serviceId: Long? = null,
    val internalProviderId: String? = null,
    val serverListNumber: String? = null,
    val serverListName: String? = null,
) {
    val numberForDisplay: String
        get() = serverListNumber ?: displayNumber

    val nameForDisplay: String
        get() = serverListName ?: displayName
}

class ChannelRepository(private val context: Context) {

    fun getPhysicalChannels(): List<PhysicalChannel> {
        val tunerInputIds = tunerInputIds()
        val channels = mutableListOf<PhysicalChannel>()
        val projection =
            arrayOf(
                TvContract.Channels._ID,
                TvContract.Channels.COLUMN_DISPLAY_NAME,
                TvContract.Channels.COLUMN_DISPLAY_NUMBER,
                TvContract.Channels.COLUMN_INPUT_ID,
                TvContract.Channels.COLUMN_SERVICE_TYPE,
                TvContract.Channels.COLUMN_TYPE,
                TvContract.Channels.COLUMN_ORIGINAL_NETWORK_ID,
                TvContract.Channels.COLUMN_TRANSPORT_STREAM_ID,
                TvContract.Channels.COLUMN_SERVICE_ID,
                TvContract.Channels.COLUMN_INTERNAL_PROVIDER_ID,
            )
        try {
            context.contentResolver
                .query(TvContract.Channels.CONTENT_URI, projection, null, null, null)
                ?.use { cursor ->
                    val idIndex = cursor.getColumnIndex(TvContract.Channels._ID)
                    val nameIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_DISPLAY_NAME)
                    val numberIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_DISPLAY_NUMBER)
                    val inputIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_INPUT_ID)
                    val serviceTypeIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_SERVICE_TYPE)
                    val typeIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_TYPE)
                    val networkIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_ORIGINAL_NETWORK_ID)
                    val transportStreamIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_TRANSPORT_STREAM_ID)
                    val serviceIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_SERVICE_ID)
                    val providerIndex = cursor.getColumnIndex(TvContract.Channels.COLUMN_INTERNAL_PROVIDER_ID)
                    while (cursor.moveToNext()) {
                        // Streaming apps register promo "preview"/"other" channels in the
                        // same table (e.g. Google's "Movies & TV" on 1-1); those aren't
                        // tunable broadcasts — skip them.
                        val type = if (typeIndex >= 0) cursor.getString(typeIndex) else null
                        if (type == TvContract.Channels.TYPE_PREVIEW || type == TvContract.Channels.TYPE_OTHER) continue

                        val inputId = if (inputIndex >= 0) cursor.getString(inputIndex) else null
                        if (inputId != null && inputId.startsWith("com.google.android.videos")) continue
                        // If we know the device's hardware tuner inputs, only keep their channels
                        if (tunerInputIds.isNotEmpty() && inputId != null && inputId !in tunerInputIds) continue

                        val id = cursor.getLong(idIndex)
                        channels.add(
                            PhysicalChannel(
                                id = id,
                                displayName = cursor.getString(nameIndex) ?: "Unknown",
                                displayNumber = cursor.getString(numberIndex) ?: "0",
                                inputId = inputId,
                                uri = TvContract.buildChannelUri(id),
                                serviceType = serviceTypeIndex.takeIf { it >= 0 }?.let(cursor::getString),
                                channelType = type,
                                networkId = networkIndex.takeIf { it >= 0 && !cursor.isNull(it) }?.let(cursor::getLong),
                                transportStreamId = transportStreamIndex.takeIf { it >= 0 && !cursor.isNull(it) }?.let(cursor::getLong),
                                serviceId = serviceIndex.takeIf { it >= 0 && !cursor.isNull(it) }?.let(cursor::getLong),
                                internalProviderId = providerIndex.takeIf { it >= 0 }?.let(cursor::getString),
                            )
                        )
                    }
                }
        } catch (e: Exception) {
            e.printStackTrace()
        }
        // ATSC channels always live on subchannels (.1 and up); whole-number rows
        // (e.g. a bare "20" with no minor) are scan artifacts — drop them. Keep the
        // unfiltered list only if filtering would leave nothing (non-ATSC devices).
        val subchannels = channels.filter { minorNumber(it.displayNumber) >= 1 }
        val result = if (subchannels.isNotEmpty()) subchannels else channels
        return result.sortedWith(compareBy({ majorNumber(it.displayNumber) }, { minorNumber(it.displayNumber) }))
    }

    private fun tunerInputIds(): Set<String> {
        val manager = context.getSystemService(Context.TV_INPUT_SERVICE) as? TvInputManager ?: return emptySet()
        return manager.tvInputList
            .filter { it.type == TvInputInfo.TYPE_TUNER && !it.isPassthroughInput }
            .map { it.id }
            .toSet()
    }

    /** First real tuner input on the device — fallback when a channel row has no input id. */
    fun defaultTunerInputId(): String? {
        val manager = context.getSystemService(Context.TV_INPUT_SERVICE) as? TvInputManager ?: return null
        val inputs = manager.tvInputList
        return (inputs.firstOrNull { it.type == TvInputInfo.TYPE_TUNER && !it.isPassthroughInput }
            ?: inputs.firstOrNull { !it.isPassthroughInput })
            ?.id
    }

    private fun majorNumber(number: String): Int =
        number.replace('-', '.').substringBefore('.').trim().toIntOrNull() ?: Int.MAX_VALUE

    private fun minorNumber(number: String): Int =
        number.replace('-', '.').substringAfter('.', "0").trim().toIntOrNull() ?: 0
}
