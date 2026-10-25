package dev.homelabtv.ui.menu

import androidx.compose.foundation.background
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.homelabtv.theme.JellyfinBlue
import dev.homelabtv.theme.SurfaceLight
import dev.homelabtv.theme.TextPrimary
import dev.homelabtv.theme.TextSecondary
import dev.homelabtv.ui.components.MenuListItem
import dev.homelabtv.ui.components.StatusPill
import kotlinx.coroutines.delay

/** Left-side navigation panel, Jellyfin drawer style. */
@Composable
fun MenuOverlay(
    showSettings: Boolean,
    isOnline: Boolean,
    lastUpdatedMillis: Long?,
    serverUrl: String,
    pairingCode: String?,
    pairingStatus: String,
    pairingInProgress: Boolean,
    devicePaired: Boolean,
    inventorySyncStatus: String,
    inventorySyncInProgress: Boolean,
    channelListStatus: String,
    channelListModeLabel: String,
    tunerInfo: String,
    onServerUrlChange: (String) -> Unit,
    onPairDevice: () -> Unit,
    onSyncChannelInventory: () -> Unit,
    onCycleChannelListMode: () -> Unit,
    onResume: () -> Unit,
    onOpenGuide: () -> Unit,
    onRefresh: () -> Unit,
    onOpenSettings: () -> Unit,
    onCloseSettings: () -> Unit,
    onReloadChannels: () -> Unit,
    onScanChannels: () -> Unit,
    entryModeLabel: String,
    onCycleEntryMode: () -> Unit,
    quickThresholdLabel: String?,
    onCycleQuickThreshold: () -> Unit,
    onRestartApp: () -> Unit,
    onExit: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val firstItem = remember { FocusRequester() }
    val pairingItem = remember { FocusRequester() }
    val settingsScrollState = rememberScrollState()
    LaunchedEffect(showSettings) {
        delay(100)
        try {
            if (showSettings && !devicePaired) pairingItem.requestFocus()
            else firstItem.requestFocus()
        } catch (_: Exception) {}
    }
    LaunchedEffect(pairingCode) {
        if (pairingCode != null) settingsScrollState.animateScrollTo(0)
    }

    Row(modifier.fillMaxSize()) {
        Column(
            Modifier.width(380.dp)
                .fillMaxHeight()
                .background(Color(0xF2161616))
                .verticalScroll(settingsScrollState)
                .padding(32.dp)
        ) {
            Row {
                Text("Homelab", color = TextPrimary, fontSize = 24.sp, fontWeight = FontWeight.Bold)
                Text("TV", color = JellyfinBlue, fontSize = 24.sp, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.height(10.dp))
            StatusPill(isOnline, lastUpdatedMillis)
            Spacer(Modifier.height(32.dp))

            if (showSettings) {
                Text("Settings", color = TextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(16.dp))
                MenuListItem(
                    when {
                        devicePaired -> "Device paired"
                        pairingInProgress -> "Pairing in progress…"
                        else -> "Pair this TV"
                    },
                    Icons.Default.Settings,
                    onPairDevice,
                    focusRequester = if (devicePaired) null else pairingItem,
                )
                if (pairingCode != null) {
                    Column(
                        Modifier.fillMaxWidth()
                            .background(Color(0xFF102C3A), RoundedCornerShape(8.dp))
                            .padding(16.dp)
                    ) {
                        Text(
                            "Pairing code — enter it in the HomelabTV web interface:",
                            color = TextSecondary,
                            fontSize = 12.sp,
                        )
                        Text(
                            pairingCode,
                            color = JellyfinBlue,
                            fontSize = 40.sp,
                            fontWeight = FontWeight.Bold,
                            letterSpacing = 8.sp,
                            modifier = Modifier.padding(vertical = 4.dp),
                        )
                        Text(
                            "The code expires after five minutes.",
                            color = TextSecondary,
                            fontSize = 12.sp,
                        )
                    }
                }
                if (pairingStatus.isNotBlank()) {
                    Text(pairingStatus, color = TextSecondary, fontSize = 12.sp)
                }
                Spacer(Modifier.height(16.dp))
                MenuListItem("Reload Channel List", Icons.Default.Refresh, onReloadChannels, focusRequester = firstItem)
                MenuListItem(channelListModeLabel, Icons.AutoMirrored.Filled.List, onCycleChannelListMode)
                Text(
                    "Die Serverliste wird nur verwendet, wenn du sie hier auswählst.",
                    color = TextSecondary,
                    fontSize = 12.sp,
                )
                MenuListItem("Scan Channels (TV Setup)", Icons.Default.Search, onScanChannels)
                MenuListItem(entryModeLabel, Icons.Default.Edit, onCycleEntryMode)
                if (quickThresholdLabel != null) {
                    MenuListItem(quickThresholdLabel, Icons.Default.Add, onCycleQuickThreshold)
                }
                Spacer(Modifier.height(16.dp))
                TextField(
                    value = serverUrl,
                    onValueChange = onServerUrlChange,
                    label = { Text("Server URL") },
                    singleLine = true,
                    colors =
                        TextFieldDefaults.colors(
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary,
                            focusedContainerColor = SurfaceLight,
                            unfocusedContainerColor = SurfaceLight,
                            focusedIndicatorColor = JellyfinBlue,
                            focusedLabelColor = JellyfinBlue,
                        ),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                if (devicePaired) {
                    MenuListItem(
                        if (inventorySyncInProgress) "Syncing Channel Inventory…" else "Sync Channel Inventory",
                        Icons.Default.Refresh,
                        onSyncChannelInventory,
                    )
                    if (inventorySyncStatus.isNotBlank()) {
                        Text(inventorySyncStatus, color = TextSecondary, fontSize = 12.sp)
                    }
                    if (channelListStatus.isNotBlank()) {
                        Text(channelListStatus, color = TextSecondary, fontSize = 12.sp)
                    }
                }
                Spacer(Modifier.height(16.dp))
                Text("Tuner input", color = TextSecondary, fontSize = 12.sp)
                Text(tunerInfo, color = TextPrimary, fontSize = 13.sp)
                Spacer(Modifier.height(24.dp))
                MenuListItem("Restart App", Icons.Default.Build, onRestartApp)
                MenuListItem("Back", Icons.AutoMirrored.Filled.ArrowBack, onCloseSettings)
            } else {
                MenuListItem("Resume", Icons.Default.PlayArrow, onResume, focusRequester = firstItem)
                MenuListItem("Program Guide", Icons.AutoMirrored.Filled.List, onOpenGuide)
                MenuListItem("Refresh Guide", Icons.Default.Refresh, onRefresh)
                MenuListItem("Settings", Icons.Default.Settings, onOpenSettings)
                Spacer(Modifier.weight(1f))
                MenuListItem("Exit App", Icons.AutoMirrored.Filled.ExitToApp, onExit)
            }
        }
        Box(
            Modifier.weight(1f)
                .fillMaxHeight()
                .background(Brush.horizontalGradient(listOf(Color(0xB3000000), Color.Transparent)))
        )
    }
}
