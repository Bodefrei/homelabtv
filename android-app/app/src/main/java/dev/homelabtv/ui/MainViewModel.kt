package dev.homelabtv.ui

import android.app.Application
import android.content.Context
import android.os.Build
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import dev.homelabtv.data.ChannelGuide
import dev.homelabtv.data.ChannelRepository
import dev.homelabtv.data.DeviceChannelInventoryEntry
import dev.homelabtv.data.DeviceChannelInventoryRequest
import dev.homelabtv.data.DeviceChannelListResponse
import dev.homelabtv.data.DeviceChannelListSummary
import dev.homelabtv.data.DeviceCredentialStore
import dev.homelabtv.data.GuideRepository
import dev.homelabtv.data.GuideState
import dev.homelabtv.data.PairingPollRequest
import dev.homelabtv.data.PairingStartRequest
import dev.homelabtv.data.PhysicalChannel
import dev.homelabtv.data.Program
import dev.homelabtv.data.RetrofitClient
import dev.homelabtv.data.XmltvTime
import dev.homelabtv.data.calculateDeviceChannelInventoryHash
import dev.homelabtv.data.channelMajor
import dev.homelabtv.data.channelMinor
import dev.homelabtv.data.normalizeChannelNumber
import dev.homelabtv.data.numberPrefixComplete
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import java.net.URI

data class Reminder(val channel: String, val title: String, val startMillis: Long)

enum class NumberEntryMode {
    /** Learn from the scanned lineup: complete a prefix as soon as it's unambiguous. */
    AUTO,
    /** Majors are always two digits: 6.1 is typed "06". */
    LEADING_ZERO,
    /** A first digit above the configurable threshold completes the major by itself. */
    QUICK,
}

enum class ChannelListMode {
    TV,
    SERVER,
}

class MainViewModel(app: Application) : AndroidViewModel(app) {

    private val prefs = app.getSharedPreferences("homelab_prefs", Context.MODE_PRIVATE)
    private val guideRepository = GuideRepository(app)
    private val channelRepository = ChannelRepository(app)
    private val deviceCredentialStore = DeviceCredentialStore(app)
    private val gson = Gson()

    val guideState: StateFlow<GuideState> = guideRepository.state

    var physicalChannels by mutableStateOf(listOf<PhysicalChannel>())
        private set

    private var scannedChannels = emptyList<PhysicalChannel>()
    private var favoriteChannelIds: List<Long>? = null
    private var favoriteChannelNumbers: Map<Long, String> = emptyMap()

    var currentChannelIndex by mutableIntStateOf(0)
        private set

    var serverUrl by mutableStateOf(prefs.getString("server_url", DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL)
        private set

    var numberEntryMode by mutableStateOf(loadNumberEntryMode())
        private set

    var quickThreshold by mutableIntStateOf(prefs.getInt("quick_threshold", 5).coerceIn(1, 8))
        private set

    var pairingCode by mutableStateOf<String?>(null)
        private set

    var pairingStatus by mutableStateOf("")
        private set

    var pairingInProgress by mutableStateOf(false)
        private set

    var devicePaired by mutableStateOf(false)
        private set

    var inventorySyncStatus by mutableStateOf("")
        private set

    var channelListStatus by mutableStateOf("")
        private set

    var serverChannelLists by mutableStateOf(loadCachedServerChannelLists())
        private set

    var serverChannelListsLoading by mutableStateOf(false)
        private set

    var selectedServerChannelListId by mutableStateOf(
        prefs.getString("selected_server_channel_list_id", null)
    )
        private set

    var channelListMode by mutableStateOf(
        prefs.getString("channel_list_mode", ChannelListMode.TV.name)
            ?.let { runCatching { ChannelListMode.valueOf(it) }.getOrNull() }
            ?: ChannelListMode.TV
    )
        private set

    var inventorySyncInProgress by mutableStateOf(false)
        private set

    fun cycleNumberEntryMode() {
        numberEntryMode =
            when (numberEntryMode) {
                NumberEntryMode.AUTO -> NumberEntryMode.LEADING_ZERO
                NumberEntryMode.LEADING_ZERO -> NumberEntryMode.QUICK
                NumberEntryMode.QUICK -> NumberEntryMode.AUTO
            }
        prefs.edit().putString("number_entry_mode", numberEntryMode.name).apply()
    }

    fun cycleQuickThreshold() {
        quickThreshold = if (quickThreshold >= 8) 1 else quickThreshold + 1
        prefs.edit().putInt("quick_threshold", quickThreshold).apply()
    }

    private fun loadNumberEntryMode(): NumberEntryMode {
        prefs.getString("number_entry_mode", null)?.let { stored ->
            try {
                return NumberEntryMode.valueOf(stored)
            } catch (_: IllegalArgumentException) {}
        }
        // Migrate the old boolean setting; new installs default to AUTO
        return if (prefs.getBoolean("number_entry_quick", false)) NumberEntryMode.QUICK
        else NumberEntryMode.AUTO
    }

    private fun lineupMajors(): Set<String> =
        physicalChannels
            .map { normalizeChannelNumber(it.numberForDisplay).substringBefore('.') }
            .map { it.trimStart('0').ifEmpty { "0" } }
            .toSet()

    private fun lineupMinors(major: Int): Set<String> =
        physicalChannels
            .filter { channelMajor(it.numberForDisplay) == major }
            .map { normalizeChannelNumber(it.numberForDisplay).substringAfter('.', "") }
            .filter { it.isNotEmpty() }
            .toSet()

    /** Should the typed major prefix auto-complete (jump to the decimal part)? */
    fun isMajorEntryComplete(prefix: String): Boolean {
        if (prefix.isEmpty()) return false
        return when (numberEntryMode) {
            NumberEntryMode.LEADING_ZERO -> prefix.length >= 2
            NumberEntryMode.QUICK ->
                prefix.length >= 2 || prefix.first() - '0' > quickThreshold
            NumberEntryMode.AUTO -> {
                val majors = lineupMajors()
                when {
                    majors.isEmpty() -> prefix.length >= 2 // no lineup to learn from
                    prefix == "0" -> false // explicit leading-zero entry still works
                    prefix.startsWith("0") -> true // "06" -> major 6
                    else -> numberPrefixComplete(prefix, majors) || prefix.length >= 3
                }
            }
        }
    }

    /** Should a typed "major.minor" entry commit immediately (no 2s wait)? */
    fun isMinorEntryComplete(entry: String): Boolean {
        val minorPart = entry.substringAfter('.', "")
        if (minorPart.length >= 2) return true
        if (minorPart.isEmpty() || numberEntryMode != NumberEntryMode.AUTO) return false
        val major = entry.substringBefore('.').toIntOrNull() ?: return false
        val minors = lineupMinors(major)
        return minors.isNotEmpty() && numberPrefixComplete(minorPart, minors)
    }

    val fallbackInputId: String? by lazy { channelRepository.defaultTunerInputId() }

    val currentChannel: PhysicalChannel?
        get() = physicalChannels.getOrNull(currentChannelIndex)

    init {
        try {
            devicePaired = deviceCredentialStore.credential() != null
        } catch (e: Exception) {
            pairingStatus = "Gespeicherte Geräteanmeldung kann nicht gelesen werden: ${e.message ?: "Keystore-Fehler"}"
        }
        scannedChannels = channelRepository.getPhysicalChannels()
        applyFavoriteChannelOrder()
        viewModelScope.launch {
            guideRepository.loadCache()
            while (true) {
                refreshGuideAndChannelList()
                delay(REFRESH_INTERVAL_MS)
            }
        }
        viewModelScope.launch {
            // Delay first: Main.immediate would otherwise run checkReminders()
            // synchronously inside the constructor, before the reminder
            // properties below are initialized
            while (true) {
                delay(15_000)
                checkReminders()
            }
        }
    }

    fun refreshNow() {
        viewModelScope.launch { refreshGuideAndChannelList() }
    }

    fun selectChannelList(listId: String?) {
        if (listId == null) {
            channelListMode = ChannelListMode.TV
            prefs.edit().putString("channel_list_mode", channelListMode.name).apply()
            applyFavoriteChannelOrder()
            channelListStatus = "TV-Kanalliste wird verwendet."
            return
        }

        selectedServerChannelListId = listId
        channelListMode = ChannelListMode.SERVER
        prefs.edit()
            .putString("channel_list_mode", channelListMode.name)
            .putString("selected_server_channel_list_id", listId)
            .apply()
        favoriteChannelIds = null
        favoriteChannelNumbers = emptyMap()
        applyFavoriteChannelOrder()
        channelListStatus = "Serverliste wird geladen …"
        viewModelScope.launch {
            val credential =
                try {
                    deviceCredentialStore.credential()
                } catch (e: Exception) {
                    channelListStatus = "Geräteanmeldung kann nicht gelesen werden: ${e.message ?: "Keystore-Fehler"}"
                    return@launch
                }
            if (credential == null) {
                channelListStatus = "Für Serverlisten muss dieser Fernseher gekoppelt sein."
                return@launch
            }
            refreshDeviceChannelList(credential)
        }
    }

    val channelListModeLabel: String
        get() =
            if (channelListMode == ChannelListMode.SERVER) {
                val selectedName = serverChannelLists.firstOrNull { it.id == selectedServerChannelListId }?.name
                "Senderliste: ${selectedName ?: "Serverliste"}"
            } else {
                "Senderliste: TV"
            }

    private suspend fun refreshGuideAndChannelList() {
        val credential =
            try {
                deviceCredentialStore.credential()
            } catch (e: Exception) {
                channelListStatus = "Geräteanmeldung kann nicht gelesen werden: ${e.message ?: "Keystore-Fehler"}"
                guideRepository.refresh(serverUrl)
                return
            }
        coroutineScope {
            val guideRefresh = async { guideRepository.refresh(serverUrl, credential) }
            if (credential == null) {
                guideRefresh.await()
                favoriteChannelIds = null
                applyFavoriteChannelOrder()
                channelListStatus = ""
            } else {
                val listRefresh = async { refreshDeviceChannelList(credential) }
                guideRefresh.await()
                listRefresh.await()
            }
        }
    }

    private suspend fun refreshDeviceChannelList(credential: String) {
        serverChannelListsLoading = true
        try {
            val api = RetrofitClient.getInstance(serverUrl)
            val authorization = "Bearer $credential"
            var listOptionsError: Exception? = null
            try {
                val lists = api.getCurrentDeviceChannelLists(authorization)
                serverChannelLists = lists
                prefs.edit().putString("server_channel_lists", gson.toJson(lists)).apply()
            } catch (e: Exception) {
                listOptionsError = e
            }
            val selectedListId =
                if (listOptionsError == null) {
                    selectedServerChannelListId
                        ?.takeIf { id -> serverChannelLists.any { it.id == id } }
                        ?: serverChannelLists.firstOrNull { it.assigned }?.id
                } else {
                    null
                }
            if (selectedServerChannelListId != null && listOptionsError == null && selectedListId == null) {
                selectedServerChannelListId = null
                prefs.edit().remove("selected_server_channel_list_id").apply()
            }
            val response = api.getCurrentDeviceChannelList(authorization, selectedListId)
            if (listOptionsError != null) {
                selectedServerChannelListId = response.list_id
                val editor = prefs.edit()
                if (response.list_id == null) {
                    editor.remove("selected_server_channel_list_id")
                } else {
                    editor.putString("selected_server_channel_list_id", response.list_id)
                }
                editor.apply()
            } else if (selectedServerChannelListId == null && response.list_id != null) {
                selectedServerChannelListId = response.list_id
                prefs.edit().putString("selected_server_channel_list_id", response.list_id).apply()
            }
            applyDeviceChannelList(
                response,
                listOptionsError?.let {
                    "Listen-Auswahl konnte nicht geladen werden: ${it.message ?: it.javaClass.simpleName}"
                },
            )
        } catch (e: Exception) {
            channelListStatus =
                "Favoritenliste konnte nicht synchronisiert werden: ${e.message ?: e.javaClass.simpleName}"
        } finally {
            serverChannelListsLoading = false
        }
    }

    private fun loadCachedServerChannelLists(): List<DeviceChannelListSummary> =
        try {
            val json = prefs.getString("server_channel_lists", null) ?: return emptyList()
            gson.fromJson<List<DeviceChannelListSummary>>(
                json,
                object : TypeToken<List<DeviceChannelListSummary>>() {}.type,
            ) ?: emptyList()
        } catch (_: Exception) {
            emptyList()
        }

    private fun applyDeviceChannelList(response: DeviceChannelListResponse, warning: String? = null) {
        favoriteChannelIds =
            if (response.active) {
                response.channels.mapNotNull { it.channel_id }.distinct()
            } else {
                null
            }
        favoriteChannelNumbers =
            if (response.active) {
                response.channels
                    .mapIndexedNotNull { index, channel ->
                        channel.channel_id?.let { it to (channel.list_number ?: index + 1).toString() }
                    }
                    .toMap()
            } else {
                emptyMap()
            }
        applyFavoriteChannelOrder()
        val status =
            if (channelListMode == ChannelListMode.TV) {
                "TV-Kanalliste wird verwendet. Die Server-Favoritenliste ist verfügbar."
            } else if (response.active) {
                "Favoritenliste „${response.name ?: "Unbenannt"}“ geladen: ${physicalChannels.size} von ${response.channels.size} Sendern verfügbar."
            } else {
                "Keine Serverliste verfügbar; die TV-Kanalliste bleibt aktiv."
            }
        channelListStatus = listOfNotNull(status, warning).joinToString(" ")
    }

    private fun applyFavoriteChannelOrder() {
        val currentChannelId = currentChannel?.id
        physicalChannels =
            if (channelListMode == ChannelListMode.SERVER && favoriteChannelIds != null) {
                favoriteChannelIds.orEmpty().mapNotNull { id ->
                    scannedChannels.firstOrNull { it.id == id }
                        ?.copy(serverListNumber = favoriteChannelNumbers[id])
                }
            } else {
                scannedChannels
            }
        currentChannelIndex =
            currentChannelId?.let { id -> physicalChannels.indexOfFirst { it.id == id }.takeIf { it >= 0 } }
                ?: restoreLastChannelIndex()
    }

    /** Re-query TvContract, e.g. after READ_TV_LISTINGS is granted or a channel rescan. */
    fun reloadChannels(forceInventoryUpload: Boolean = false) {
        val stayOn = currentChannel?.id
        scannedChannels = channelRepository.getPhysicalChannels()
        applyFavoriteChannelOrder()
        // Keep the viewer on the same channel number across a rescan when it still
        // exists; otherwise fall back to whatever we last persisted, then index 0.
        currentChannelIndex =
            stayOn?.let { id -> physicalChannels.indexOfFirst { it.id == id }.takeIf { it >= 0 } }
                ?: restoreLastChannelIndex()
        syncChannelInventory(forceUpload = forceInventoryUpload)
    }

    fun syncChannelInventory(forceUpload: Boolean = false) {
        if (inventorySyncInProgress || !devicePaired) return
        val credential =
            try {
                deviceCredentialStore.credential()
            } catch (e: Exception) {
                inventorySyncStatus =
                    "Geräteanmeldung kann nicht gelesen werden: ${e.message ?: "Keystore-Fehler"}"
                return
            } ?: return
        val channels = scannedChannels.map { it.toInventoryEntry() }
        val inventoryHash = calculateDeviceChannelInventoryHash(channels)
        inventorySyncInProgress = true
        viewModelScope.launch {
            try {
                val baseUrl = validatedServerUrl(serverUrl)
                val cacheKey = "inventory_hash:$baseUrl"
                if (!forceUpload && prefs.getString(cacheKey, null) == inventoryHash) {
                    inventorySyncStatus = "Kanalinventar ist aktuell."
                    return@launch
                }
                val response =
                    RetrofitClient.getInstance(baseUrl).syncDeviceChannels(
                        authorization = "Bearer $credential",
                        request = DeviceChannelInventoryRequest(inventoryHash, channels),
                    )
                check(response.inventory_hash == inventoryHash) {
                    "Der Server hat das Kanalinventar nicht bestätigt."
                }
                check(prefs.edit().putString(cacheKey, inventoryHash).commit()) {
                    "Der lokale Synchronisierungsstatus konnte nicht gespeichert werden."
                }
                inventorySyncStatus =
                    "Kanalinventar synchronisiert: ${response.channel_count} Sender (Revision ${response.revision})."
                guideRepository.refresh(baseUrl, credential)
                refreshDeviceChannelList(credential)
            } catch (e: Exception) {
                inventorySyncStatus =
                    "Kanalinventar-Synchronisierung fehlgeschlagen: ${e.message ?: e.javaClass.simpleName}"
            } finally {
                inventorySyncInProgress = false
                if (calculateDeviceChannelInventoryHash(scannedChannels.map { it.toInventoryEntry() }) != inventoryHash) {
                    syncChannelInventory()
                }
            }
        }
    }

    private fun PhysicalChannel.toInventoryEntry() =
        DeviceChannelInventoryEntry(
            channel_id = id,
            input_id = inputId,
            display_number = displayNumber,
            display_name = displayName,
            original_display_name = originalDisplayName,
            service_type = serviceType,
            channel_type = channelType,
            network_id = networkId,
            transport_stream_id = transportStreamId,
            service_id = serviceId,
            internal_provider_id = internalProviderId,
        )

    private fun restoreLastChannelIndex(): Int {
        val saved = prefs.getString("last_channel", null) ?: return 0
        return indexOfChannelNumber(saved) ?: 0
    }

    private fun indexOfChannelNumber(displayNumber: String): Int? {
        val target = normalizeChannelNumber(displayNumber)
        return physicalChannels
            .indexOfFirst { normalizeChannelNumber(it.displayNumber) == target }
            .takeIf { it >= 0 }
    }

    fun updateServerUrl(url: String) {
        serverUrl = url
        prefs.edit().putString("server_url", url).apply()
    }

    fun startDevicePairing() {
        if (pairingInProgress) return
        if (devicePaired) {
            pairingStatus = "Dieses Gerät ist bereits gekoppelt."
            return
        }

        pairingInProgress = true
        pairingCode = null
        pairingStatus = "Verbindung zum Server wird hergestellt …"
        viewModelScope.launch {
            try {
                val baseUrl = validatedServerUrl(serverUrl)
                val deviceId = deviceCredentialStore.deviceId()
                val deviceSecret = deviceCredentialStore.deviceSecret()
                val appInfo = getApplication<Application>().packageManager
                    .getPackageInfo(getApplication<Application>().packageName, 0)
                val request =
                    PairingStartRequest(
                        device_id = deviceId,
                        device_secret = deviceSecret,
                        friendly_name = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
                        app_version = appInfo.versionName ?: "unknown",
                        android_version = Build.VERSION.RELEASE ?: "unknown",
                        manufacturer = Build.MANUFACTURER,
                        model = Build.MODEL,
                    )
                val api = dev.homelabtv.data.RetrofitClient.getInstance(baseUrl)
                val pairing = api.startPairing(request)
                pairingCode = pairing.user_code
                pairingStatus = "Code im Webinterface eingeben und das Gerät freigeben."

                val deadline = System.currentTimeMillis() + pairing.expires_in_seconds * 1000L
                while (System.currentTimeMillis() < deadline) {
                    delay(PAIRING_POLL_INTERVAL_MS)
                    val result = api.pollPairing(
                        deviceId,
                        PairingPollRequest(device_secret = deviceSecret),
                    )
                    when (result.status) {
                        "pending" -> Unit
                        "approved" -> Unit
                        "paired" -> {
                            val credential = result.device_credential
                                ?: error("Der Server hat kein neues Geräte-Credential geliefert.")
                            deviceCredentialStore.saveCredential(credential)
                            devicePaired = true
                            pairingCode = null
                            pairingStatus = "Gerät gekoppelt. Serveranmeldung wird überprüft …"
                            val registeredDevice = api.getCurrentDevice("Bearer $credential")
                            pairingStatus = "Gerät gekoppelt: ${registeredDevice.friendly_name}"
                            syncChannelInventory(forceUpload = true)
                            return@launch
                        }
                        "rejected" -> error("Die Kopplungsanfrage wurde abgelehnt.")
                        "expired" -> error("Der Kopplungscode ist abgelaufen. Bitte erneut starten.")
                        else -> error("Unbekannter Pairing-Status vom Server: ${result.status}")
                    }
                }
                error("Der Kopplungscode ist abgelaufen. Bitte erneut starten.")
            } catch (e: Exception) {
                pairingCode = null
                pairingStatus =
                    if (devicePaired) {
                        "Gerät gekoppelt, aber die Serverprüfung ist fehlgeschlagen: ${e.message ?: e.javaClass.simpleName}"
                    } else {
                        "Kopplung fehlgeschlagen: ${e.message ?: e.javaClass.simpleName}"
                    }
            } finally {
                pairingInProgress = false
            }
        }
    }

    private fun validatedServerUrl(value: String): String {
        val url = value.trim()
        val uri = URI(url)
        require(uri.scheme.equals("http", ignoreCase = true) || uri.scheme.equals("https", ignoreCase = true)) {
            "Server-URL muss mit http:// oder https:// beginnen."
        }
        require(!uri.host.isNullOrBlank() && uri.userInfo == null && uri.rawQuery == null && uri.rawFragment == null) {
            "Server-URL enthält ungültige oder nicht unterstützte Bestandteile."
        }
        return if (url.endsWith("/")) url else "$url/"
    }

    // ----- channel selection (tracks the previous channel for jump-back) -----

    private var previousChannelIndex = -1

    private fun selectChannel(index: Int) {
        if (index == currentChannelIndex || index !in physicalChannels.indices) return
        previousChannelIndex = currentChannelIndex
        currentChannelIndex = index
        // Remember by channel number (stable across rescans, unlike the index)
        prefs.edit().putString("last_channel", physicalChannels[index].displayNumber).apply()
    }

    /** Swap back to the channel watched before this one ("last channel" key). */
    fun jumpBack(): Boolean {
        if (previousChannelIndex !in physicalChannels.indices) return false
        selectChannel(previousChannelIndex)
        return true
    }

    fun zap(delta: Int) {
        val size = physicalChannels.size
        if (size == 0) return
        selectChannel(((currentChannelIndex + delta) % size + size) % size)
    }

    /**
     * Tune directly to a typed channel number like "7.1" or "20.3". A major-only
     * entry ("13", "7") goes to that station's .1 subchannel, falling back to the
     * lowest subchannel that exists. False if no match.
     */
    fun tuneToNumber(entry: String): Boolean {
        val cleaned = entry.trim().trimEnd('.')
        if (cleaned.isEmpty()) return false
        val major = cleaned.substringBefore('.').toIntOrNull() ?: return false
        val minor = cleaned.substringAfter('.', "").toIntOrNull()

        val majorMatches = physicalChannels.withIndex().filter { majorOf(it.value) == major }
        val match =
            if (minor != null) {
                majorMatches.firstOrNull { minorOf(it.value) == minor }
            } else {
                majorMatches.firstOrNull { minorOf(it.value) == 1 }
                    ?: majorMatches.minByOrNull { minorOf(it.value) }
            }
        if (match == null) return false
        selectChannel(match.index)
        return true
    }

    private fun majorOf(channel: PhysicalChannel): Int = channelMajor(channel.numberForDisplay)

    private fun minorOf(channel: PhysicalChannel): Int = channelMinor(channel.numberForDisplay)

    /** Tune to the physical channel a guide row is mapped to. False if no tuner match. */
    fun tuneTo(guide: ChannelGuide): Boolean {
        val target = normalizeChannelNumber(guide.physical_channel)
        val index = physicalChannels.indexOfFirst { normalizeChannelNumber(it.displayNumber) == target }
        if (index >= 0) {
            selectChannel(index)
            return true
        }
        return false
    }

    // ----- program reminders -----

    var reminders by mutableStateOf(loadReminders())
        private set

    /** The reminder whose show is starting right now, if any — drives the banner. */
    var dueReminder by mutableStateOf<Reminder?>(null)
        private set

    private fun loadReminders(): List<Reminder> =
        try {
            val json = prefs.getString("reminders", null)
            if (json == null) emptyList()
            else gson.fromJson(json, object : TypeToken<List<Reminder>>() {}.type)
        } catch (e: Exception) {
            emptyList()
        }

    private fun saveReminders() {
        prefs.edit().putString("reminders", gson.toJson(reminders)).apply()
    }

    /** @return true if a reminder was added, false if an existing one was removed. */
    fun toggleReminder(channel: ChannelGuide, program: Program): Boolean {
        val start = XmltvTime.parse(program.start) ?: return false
        val number = normalizeChannelNumber(channel.physical_channel)
        val existing = reminders.firstOrNull { it.channel == number && it.startMillis == start }
        reminders =
            if (existing != null) reminders - existing
            else reminders + Reminder(number, program.title ?: "Program", start)
        saveReminders()
        return existing == null
    }

    private fun checkReminders() {
        val now = System.currentTimeMillis()
        // Drop reminders whose show is long underway
        val stale = reminders.filter { it.startMillis + STALE_REMINDER_MS < now }
        if (stale.isNotEmpty()) {
            reminders = reminders - stale.toSet()
            saveReminders()
        }
        if (dueReminder == null) {
            val due = reminders.firstOrNull { it.startMillis - 30_000 <= now }
            if (due != null) {
                dueReminder = due
                reminders = reminders - due
                saveReminders()
            }
        }
    }

    fun watchReminder() {
        dueReminder?.let { tuneToNumber(it.channel) }
        dueReminder = null
    }

    fun dismissReminder() {
        dueReminder = null
    }

    companion object {
        const val DEFAULT_SERVER_URL = ""
        private const val PAIRING_POLL_INTERVAL_MS = 3_000L
        private const val REFRESH_INTERVAL_MS = 15L * 60 * 1000
        private const val STALE_REMINDER_MS = 5L * 60 * 1000
    }
}
