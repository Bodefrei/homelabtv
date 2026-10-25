package dev.homelabtv

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.tv.TvInputInfo
import android.media.tv.TvInputManager
import android.os.Bundle
import android.util.Log
import android.view.KeyEvent
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onKeyEvent
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.homelabtv.data.forChannel
import dev.homelabtv.data.mergeGuide
import dev.homelabtv.theme.HomelabTVTheme
import dev.homelabtv.theme.JellyfinBlue
import dev.homelabtv.ui.MainViewModel
import dev.homelabtv.ui.ChannelListMode
import dev.homelabtv.ui.NumberEntryMode
import dev.homelabtv.ui.guide.GUIDE_MINI_PLAYER_END
import dev.homelabtv.ui.guide.GUIDE_MINI_PLAYER_HEIGHT
import dev.homelabtv.ui.guide.GUIDE_MINI_PLAYER_TOP
import dev.homelabtv.ui.guide.GUIDE_MINI_PLAYER_WIDTH
import dev.homelabtv.ui.guide.GuideScreen
import dev.homelabtv.ui.menu.MenuOverlay
import dev.homelabtv.ui.player.DetailsBanner
import dev.homelabtv.ui.player.NumberEntryOverlay
import dev.homelabtv.ui.player.PlayerSurface
import dev.homelabtv.ui.player.ReminderOverlay
import dev.homelabtv.ui.player.ZapperOverlay
import dev.homelabtv.ui.player.rememberTvPlayerState
import kotlinx.coroutines.delay

class MainActivity : ComponentActivity() {
    private var guideKeyHandler: (() -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Live TV is always "actively watched" — never let the screensaver kick in
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContent { HomelabTVTheme { HomelabApp() } }
    }

    fun setGuideKeyHandler(handler: (() -> Unit)?) {
        guideKeyHandler = handler
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.keyCode in GUIDE_KEYS && event.action in setOf(KeyEvent.ACTION_DOWN, KeyEvent.ACTION_UP)) {
            Log.d(
                "HomelabKeys",
                "activity keyCode=${event.keyCode} scanCode=${event.scanCode} action=${event.action}",
            )
            val handler = guideKeyHandler
            if (handler != null) {
                if (event.action == KeyEvent.ACTION_UP) handler()
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }
}

// Not exposed as a Manifest.permission constant in current SDKs
private const val READ_TV_LISTINGS_PERMISSION = "android.permission.READ_TV_LISTINGS"

private val GUIDE_KEYS = setOf(KeyEvent.KEYCODE_GUIDE, KeyEvent.KEYCODE_HELP)

private val HANDLED_KEYS =
    setOf(
        KeyEvent.KEYCODE_DPAD_UP,
        KeyEvent.KEYCODE_DPAD_DOWN,
        KeyEvent.KEYCODE_CHANNEL_UP,
        KeyEvent.KEYCODE_CHANNEL_DOWN,
        KeyEvent.KEYCODE_DPAD_CENTER,
        KeyEvent.KEYCODE_ENTER,
        KeyEvent.KEYCODE_INFO,
        KeyEvent.KEYCODE_LAST_CHANNEL,
        KeyEvent.KEYCODE_DPAD_LEFT,
    ) + GUIDE_KEYS

private fun digitFor(keyCode: Int): Int? =
    when (keyCode) {
        in KeyEvent.KEYCODE_0..KeyEvent.KEYCODE_9 -> keyCode - KeyEvent.KEYCODE_0
        in KeyEvent.KEYCODE_NUMPAD_0..KeyEvent.KEYCODE_NUMPAD_9 -> keyCode - KeyEvent.KEYCODE_NUMPAD_0
        else -> null
    }

private enum class Overlay {
    NONE,
    GUIDE,
    MENU,
    SETTINGS,
}

@Composable
private fun HomelabApp(viewModel: MainViewModel = viewModel()) {
    val context = LocalContext.current
    val activity = context as? MainActivity
    val guideState by viewModel.guideState.collectAsStateWithLifecycle()
    var overlay by remember { mutableStateOf(Overlay.NONE) }
    val playerState = rememberTvPlayerState()

    // Without READ_TV_LISTINGS the TvProvider hides every channel we didn't create,
    // so the tuner looks missing even when it's fine. Ask once on launch.
    val permissionLauncher =
        rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) viewModel.reloadChannels(forceInventoryUpload = true)
        }
    LaunchedEffect(Unit) {
        val status = ContextCompat.checkSelfPermission(context, READ_TV_LISTINGS_PERMISSION)
        if (status != PackageManager.PERMISSION_GRANTED) {
            permissionLauncher.launch(READ_TV_LISTINGS_PERMISSION)
        } else {
            viewModel.reloadChannels(forceInventoryUpload = true)
        }
    }

    // Re-read the channel list whenever the app returns to the foreground, so a
    // channel rescan done in the TV's setup screens is picked up automatically
    LifecycleResumeEffect(Unit) {
        viewModel.reloadChannels()
        onPauseOrDispose {}
    }

    // The channel banner is shown on startup and after channel changes.
    var zapCounter by remember { mutableIntStateOf(0) }
    var zapperVisible by remember { mutableStateOf(false) }
    var showDetails by remember { mutableStateOf(false) }
    var numberEntry by remember { mutableStateOf("") }
    LaunchedEffect(zapCounter) {
        if (zapCounter > 0) {
            zapperVisible = true
            delay(4000)
            zapperVisible = false
        }
    }
    // The detailed banner times itself out
    LaunchedEffect(showDetails) {
        if (showDetails) {
            delay(10_000)
            showDetails = false
        }
    }
    val onInfoPressed: () -> Unit = {
        numberEntry = ""
        showDetails = false
        overlay = if (overlay == Overlay.GUIDE) Overlay.NONE else Overlay.GUIDE
    }

    // Direct channel entry from the remote's number keys. When a prefix (major
    // or minor) is unambiguous for the current mode it completes instantly;
    // otherwise a 2s pause commits whatever was typed. The decimal part maxes
    // out at two digits (hundredths).
    DisposableEffect(activity) {
        activity?.setGuideKeyHandler {
            numberEntry = ""
            showDetails = false
            overlay = if (overlay == Overlay.GUIDE) Overlay.NONE else Overlay.GUIDE
        }
        onDispose { activity?.setGuideKeyHandler(null) }
    }
    val commitEntry: () -> Unit = {
        val target = numberEntry.trimEnd('.')
        numberEntry = ""
        if (target.isNotEmpty() && viewModel.tuneToNumber(target)) {
            showDetails = false
        }
    }
    val appendDigit: (Int) -> Unit = { digit ->
        var entry = numberEntry
        if ('.' in entry) {
            if (entry.substringAfter('.').length < 2) entry += digit
        } else {
            entry += digit
            if (viewModel.isMajorEntryComplete(entry)) entry += "."
        }
        numberEntry = entry
    }
    LaunchedEffect(numberEntry) {
        if (numberEntry.isEmpty()) return@LaunchedEffect
        if (viewModel.isMinorEntryComplete(numberEntry)) {
            commitEntry()
        } else {
            delay(2000)
            commitEntry()
        }
    }

    // Small bottom-left feedback toast (reminders set/removed, captions on/off)
    var toast by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(toast) {
        if (toast != null) {
            delay(3000)
            toast = null
        }
    }
    LaunchedEffect(viewModel.dueReminder) {
        if (viewModel.dueReminder != null) {
            delay(45_000)
            viewModel.dismissReminder()
        }
    }

    val rootFocus = remember { FocusRequester() }
    LaunchedEffect(overlay, showDetails) {
        if (overlay == Overlay.NONE && !showDetails) {
            try {
                rootFocus.requestFocus()
            } catch (_: Exception) {}
        }
    }

    val currentChannel = viewModel.currentChannel
    LaunchedEffect(currentChannel?.id) {
        if (currentChannel != null) {
            showDetails = false
            zapCounter++
        }
    }
    val currentGuide = guideState.channels.forChannel(currentChannel)
    val guideChannels =
        remember(viewModel.physicalChannels, guideState.channels, viewModel.channelListMode) {
            mergeGuide(
                viewModel.physicalChannels,
                guideState.channels,
                restrictToPhysical = viewModel.channelListMode == ChannelListMode.SERVER,
            )
        }

    Box(
        Modifier.fillMaxSize()
            .background(Color.Black)
            .focusRequester(rootFocus)
            .focusable()
            // Handle BACK before the focus system sees it — otherwise the first
            // press just clears the focus highlight and a second press is needed.
            .onPreviewKeyEvent { event ->
                when (event.nativeKeyEvent.keyCode) {
                    KeyEvent.KEYCODE_BACK -> {
                        if (event.type == KeyEventType.KeyUp) {
                            when {
                                viewModel.dueReminder != null && overlay == Overlay.NONE -> viewModel.dismissReminder()
                                showDetails -> showDetails = false
                                overlay == Overlay.SETTINGS -> overlay = Overlay.MENU
                                overlay != Overlay.NONE -> overlay = Overlay.NONE
                                else -> overlay = Overlay.MENU
                            }
                        }
                        true
                    }
                    in GUIDE_KEYS -> {
                        if (event.type == KeyEventType.KeyUp) {
                            numberEntry = ""
                            showDetails = false
                            overlay = if (overlay == Overlay.GUIDE) Overlay.NONE else Overlay.GUIDE
                        }
                        true
                    }
                    else -> false
                }
            }
            .onKeyEvent { event ->
                val keyCode = event.nativeKeyEvent.keyCode
                if (showDetails) {
                    // D-pad and OK navigate the banner's own controls; INFO/GUIDE are ours
                    return@onKeyEvent when (keyCode) {
                        KeyEvent.KEYCODE_INFO -> {
                            if (event.type == KeyEventType.KeyUp) onInfoPressed()
                            true
                        }
                        else -> false
                    }
                }
                if (overlay != Overlay.NONE) {
                    return@onKeyEvent false
                }
                val digit = digitFor(keyCode)
                if (digit == null && keyCode !in HANDLED_KEYS) {
                    // Surfaced in logcat so unknown remote buttons are easy to identify
                    if (event.type == KeyEventType.KeyUp) Log.d("HomelabKeys", "unhandled key: $keyCode")
                    return@onKeyEvent false
                }
                // Swallow KeyDown as well as KeyUp, so the system's own guide or
                // channel UIs never react to keys this app owns (e.g. Sony's guide
                // popping up over ours on KEYCODE_GUIDE)
                if (event.type != KeyEventType.KeyUp) return@onKeyEvent true
                when {
                    digit != null -> appendDigit(digit)
                    keyCode == KeyEvent.KEYCODE_DPAD_UP || keyCode == KeyEvent.KEYCODE_CHANNEL_UP -> {
                        numberEntry = ""
                        viewModel.zap(1)
                    }
                    keyCode == KeyEvent.KEYCODE_DPAD_DOWN || keyCode == KeyEvent.KEYCODE_CHANNEL_DOWN -> {
                        numberEntry = ""
                        viewModel.zap(-1)
                    }
                    keyCode == KeyEvent.KEYCODE_LAST_CHANNEL -> {
                        numberEntry = ""
                        viewModel.jumpBack()
                    }
                    keyCode == KeyEvent.KEYCODE_DPAD_LEFT -> {
                        // D-pad left doubles as last-channel for remotes without the key
                        viewModel.jumpBack()
                    }
                    keyCode == KeyEvent.KEYCODE_DPAD_CENTER || keyCode == KeyEvent.KEYCODE_ENTER -> {
                        // Long-press OK opens the detailed program banner.
                        val isLongPress =
                            event.nativeKeyEvent.eventTime - event.nativeKeyEvent.downTime >= 600
                        when {
                            viewModel.dueReminder != null -> viewModel.watchReminder()
                            numberEntry.isNotEmpty() -> commitEntry()
                            isLongPress -> {
                                zapperVisible = false
                                showDetails = true
                            }
                            else -> zapperVisible = false
                        }
                    }
                    keyCode == KeyEvent.KEYCODE_INFO -> onInfoPressed()
                }
                true
            }
    ) {
        // While the guide is open the TvView shrinks into the hero's top-right
        // corner (the guide leaves that region transparent as a mini player)
        val playerModifier =
            if (overlay == Overlay.GUIDE) {
                Modifier.align(Alignment.TopEnd)
                    .padding(top = GUIDE_MINI_PLAYER_TOP, end = GUIDE_MINI_PLAYER_END)
                    .size(GUIDE_MINI_PLAYER_WIDTH, GUIDE_MINI_PLAYER_HEIGHT)
            } else {
                Modifier.fillMaxSize()
            }
        PlayerSurface(
            channel = currentChannel,
            fallbackInputId = viewModel.fallbackInputId,
            playerState = playerState,
            modifier = playerModifier,
        )

        ZapperOverlay(
            visible = zapperVisible && overlay == Overlay.NONE && !showDetails,
            channel = currentChannel,
            guide = currentGuide,
            showClock = false,
            modifier = Modifier.align(Alignment.BottomCenter),
        )

        NumberEntryOverlay(entry = numberEntry, modifier = Modifier.align(Alignment.TopEnd))

        if (showDetails && overlay == Overlay.NONE) {
            DetailsBanner(
                channel = currentChannel,
                guide = currentGuide,
                playerState = playerState,
                modifier = Modifier.align(Alignment.TopCenter),
            )
        }

        when (overlay) {
            Overlay.GUIDE ->
                GuideScreen(
                    channels = guideChannels,
                    isOnline = guideState.isOnline,
                    initialChannelNumber = currentChannel?.displayNumber,
                    onTune = { guide ->
                        if (viewModel.tuneTo(guide)) {
                            overlay = Overlay.NONE
                        }
                    },
                    reminderKeys = viewModel.reminders.map { "${it.channel}|${it.startMillis}" }.toSet(),
                    onToggleReminder = { channel, program ->
                        val added = viewModel.toggleReminder(channel, program)
                        toast =
                            if (added) "⏰ Reminder set · ${program.title}"
                            else "Reminder removed · ${program.title}"
                    },
                )
            Overlay.MENU,
            Overlay.SETTINGS ->
                MenuOverlay(
                    showSettings = overlay == Overlay.SETTINGS,
                    isOnline = guideState.isOnline,
                    lastUpdatedMillis = guideState.lastUpdatedMillis,
                    serverUrl = viewModel.serverUrl,
                    pairingCode = viewModel.pairingCode,
                    pairingStatus = viewModel.pairingStatus,
                    pairingInProgress = viewModel.pairingInProgress,
                    devicePaired = viewModel.devicePaired,
                    inventorySyncStatus = viewModel.inventorySyncStatus,
                    inventorySyncInProgress = viewModel.inventorySyncInProgress,
                    channelListStatus = viewModel.channelListStatus,
                    channelListModeLabel = viewModel.channelListModeLabel,
                    serverChannelLists = viewModel.serverChannelLists,
                    serverChannelListsLoading = viewModel.serverChannelListsLoading,
                    selectedServerChannelListId = viewModel.selectedServerChannelListId,
                    isServerChannelListSelected = viewModel.channelListMode == ChannelListMode.SERVER,
                    tunerInfo =
                        currentChannel?.let {
                            "${it.displayName} (${it.inputId ?: viewModel.fallbackInputId ?: "no input id"})"
                        } ?: "No tuner detected",
                    onServerUrlChange = viewModel::updateServerUrl,
                    onPairDevice = viewModel::startDevicePairing,
                    onSyncChannelInventory = { viewModel.syncChannelInventory(forceUpload = true) },
                    onSelectChannelList = viewModel::selectChannelList,
                    onResume = { overlay = Overlay.NONE },
                    onOpenGuide = { overlay = Overlay.GUIDE },
                    onRefresh = { viewModel.refreshNow() },
                    onOpenSettings = { overlay = Overlay.SETTINGS },
                    onCloseSettings = {
                        viewModel.refreshNow()
                        overlay = Overlay.MENU
                    },
                    onReloadChannels = {
                        viewModel.reloadChannels()
                        overlay = Overlay.NONE
                    },
                    onScanChannels = {
                        val manager = context.getSystemService(Context.TV_INPUT_SERVICE) as? TvInputManager
                        val tuner =
                            manager?.tvInputList?.firstOrNull {
                                it.type == TvInputInfo.TYPE_TUNER && !it.isPassthroughInput
                            }
                        val setupIntent = tuner?.createSetupIntent()
                        if (setupIntent != null) {
                            setupIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            try {
                                context.startActivity(setupIntent)
                            } catch (e: Exception) {
                                e.printStackTrace()
                            }
                        }
                    },
                    entryModeLabel =
                        when (viewModel.numberEntryMode) {
                            NumberEntryMode.AUTO -> "Number Entry: Auto (learns your lineup)"
                            NumberEntryMode.LEADING_ZERO -> "Number Entry: Leading Zero (06.x)"
                            NumberEntryMode.QUICK -> "Number Entry: Quick"
                        },
                    onCycleEntryMode = viewModel::cycleNumberEntryMode,
                    quickThresholdLabel =
                        if (viewModel.numberEntryMode == NumberEntryMode.QUICK)
                            "Quick Rule: first digit above ${viewModel.quickThreshold} completes"
                        else null,
                    onCycleQuickThreshold = viewModel::cycleQuickThreshold,
                    onRestartApp = {
                        val pm = context.packageManager
                        val relaunch =
                            pm.getLeanbackLaunchIntentForPackage(context.packageName)
                                ?: pm.getLaunchIntentForPackage(context.packageName)
                        relaunch?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
                        if (relaunch != null) context.startActivity(relaunch)
                        Runtime.getRuntime().exit(0)
                    },
                    onExit = { (context as? ComponentActivity)?.finish() },
                )
            Overlay.NONE -> {}
        }

        viewModel.dueReminder?.let { due ->
            if (overlay == Overlay.NONE) {
                ReminderOverlay(
                    title = due.title,
                    channelNumber = due.channel,
                    modifier = Modifier.align(Alignment.TopCenter),
                )
            }
        }

        toast?.let { message ->
            Box(
                Modifier.align(Alignment.BottomStart)
                    .padding(32.dp)
                    .background(Color(0xF2161616), RoundedCornerShape(8.dp))
                    .padding(horizontal = 16.dp, vertical = 10.dp)
            ) {
                Text(message, color = Color.White, fontSize = 14.sp)
            }
        }

        // Dark boot screen with a spinner instead of any bright flash
        if (!guideState.hasLoadedCache) {
            Box(
                Modifier.fillMaxSize().background(Color(0xFF181818)),
                contentAlignment = Alignment.Center,
            ) {
                CircularProgressIndicator(color = JellyfinBlue)
            }
        }
    }
}
