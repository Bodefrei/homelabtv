package dev.homelabtv.ui.guide

import android.util.Log
import android.view.KeyEvent
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onKeyEvent
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import dev.homelabtv.data.ChannelGuide
import dev.homelabtv.data.GuideSlot
import dev.homelabtv.data.Program
import dev.homelabtv.data.XmltvTime
import dev.homelabtv.data.buildSlots
import dev.homelabtv.data.channelMajor
import dev.homelabtv.data.channelMinor
import dev.homelabtv.data.episodeLine
import dev.homelabtv.data.normalizeChannelNumber
import dev.homelabtv.theme.AppBackground
import dev.homelabtv.theme.JellyfinBlue
import dev.homelabtv.theme.SurfaceColor
import dev.homelabtv.theme.SurfaceLight
import dev.homelabtv.theme.TextPrimary
import dev.homelabtv.theme.TextSecondary
import dev.homelabtv.ui.components.FlagBadge
import dev.homelabtv.ui.components.formatClock
import dev.homelabtv.ui.components.formatDay
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private const val GUIDE_TAG = "HomelabGuide"
private const val DP_PER_MINUTE = 4f
private val CHANNEL_COL_WIDTH = 150.dp
private val ROW_HEIGHT = 64.dp
private const val HALF_HOUR_MS = 30L * 60 * 1000
private const val GUIDE_WINDOW_MS = 24L * 60 * 60 * 1000

// The live-video mini player floats over this corner of the hero panel; the
// actual TvView is positioned by MainActivity using these same values.
val GUIDE_MINI_PLAYER_WIDTH = 320.dp
val GUIDE_MINI_PLAYER_HEIGHT = 180.dp
val GUIDE_MINI_PLAYER_TOP = 20.dp
val GUIDE_MINI_PLAYER_END = 48.dp

private fun widthFor(startMillis: Long, stopMillis: Long): Dp =
    (((stopMillis - startMillis) / 60000f) * DP_PER_MINUTE).dp

private data class GridRow(
    val channel: ChannelGuide,
    val slots: List<GuideSlot>,
    val requesters: List<FocusRequester>,
)

/**
 * Jellyfin-style full-screen guide: focused-program hero and live mini player on
 * top, a shared-scroll timeline grid below with a "now" line.
 *
 * Vertical D-pad navigation is time-anchored: moving up/down lands on the slot in
 * the next row containing the anchor time, and only left/right movement changes
 * the anchor. This stops long program blocks from flinging focus sideways.
 */
@Composable
fun GuideScreen(
    channels: List<ChannelGuide>,
    isOnline: Boolean,
    onTune: (ChannelGuide) -> Unit,
    modifier: Modifier = Modifier,
    initialChannelNumber: String? = null,
    reminderKeys: Set<String> = emptySet(),
    onToggleReminder: (ChannelGuide, Program) -> Unit = { _, _ -> },
) {
    val now = remember { System.currentTimeMillis() }
    val windowStart = remember(now) { now - now % HALF_HOUR_MS }
    val windowEnd = windowStart + GUIDE_WINDOW_MS
    val scrollState = rememberScrollState()
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()

    val rows =
        remember(channels, windowStart) {
            channels
                .sortedWith(compareBy({ channelMajor(it.physical_channel) }, { channelMinor(it.physical_channel) }))
                .map { ch ->
                    val slots = buildSlots(ch.safePrograms, windowStart, windowEnd)
                    GridRow(ch, slots, slots.map { FocusRequester() })
                }
        }

    var anchorTime by remember { mutableStateOf(now) }
    // Program is null when a "No listings" gap cell is focused
    var focusedCell by remember { mutableStateOf<Pair<ChannelGuide, Program?>?>(null) }

    val focusRow: (Int) -> Unit = { rowIndex ->
        if (rowIndex in rows.indices) {
            val row = rows[rowIndex]
            val slotIndex =
                row.slots.indexOfFirst { anchorTime >= it.startMillis && anchorTime < it.stopMillis }
                    .let { if (it >= 0) it else 0 }
            scope.launch {
                repeat(4) { attempt ->
                    try {
                        row.requesters[slotIndex].requestFocus()
                        Log.d(GUIDE_TAG, "focusRow($rowIndex) slot=$slotIndex ok on attempt $attempt")
                        return@launch
                    } catch (e: Exception) {
                        Log.d(GUIDE_TAG, "focusRow($rowIndex) attempt $attempt failed: $e")
                    }
                    // Row not composed yet (edge of the lazy list) — scroll it in, retry
                    try {
                        listState.scrollToItem(rowIndex)
                    } catch (e: Exception) {
                        Log.d(GUIDE_TAG, "scrollToItem($rowIndex) failed: $e")
                    }
                    delay(80)
                }
                // Never leave the guide with nothing focused — fall back to the top row
                try {
                    rows[0].requesters[0].requestFocus()
                    Log.d(GUIDE_TAG, "focusRow($rowIndex) gave up; fell back to row 0")
                } catch (e: Exception) {
                    Log.d(GUIDE_TAG, "row-0 fallback also failed: $e")
                }
            }
        }
    }

    LaunchedEffect(rows.isNotEmpty()) {
        if (rows.isNotEmpty()) {
            // Open on the channel currently playing, not the top of the list
            val target = normalizeChannelNumber(initialChannelNumber)
            val startRow =
                if (target.isEmpty()) 0
                else
                    rows.indexOfFirst { normalizeChannelNumber(it.channel.physical_channel) == target }
                        .let { if (it >= 0) it else 0 }
            Log.d(GUIDE_TAG, "open: initial=$initialChannelNumber target=$target startRow=$startRow rows=${rows.size}")
            if (startRow > 0) {
                try {
                    listState.scrollToItem(startRow)
                } catch (e: Exception) {
                    Log.d(GUIDE_TAG, "initial scrollToItem($startRow) failed: $e")
                }
            }
            delay(150)
            focusRow(startRow)
        }
    }

    Column(modifier.fillMaxSize()) {
        // Hero is transparent in the top-right corner so the underlying TvView
        // (positioned there by MainActivity) shows through as a mini player.
        HeroPanel(focusedCell)
        Column(Modifier.weight(1f).fillMaxWidth().background(AppBackground)) {
            if (rows.isEmpty()) {
                Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("No guide data yet", color = TextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                        Spacer(Modifier.height(8.dp))
                        Text(
                            if (isOnline) "Map your channels in the server web UI to fill the guide"
                            else "The server is unreachable and nothing is cached yet — the guide will appear after the first successful sync",
                            color = TextSecondary,
                            fontSize = 13.sp,
                        )
                    }
                }
            } else {
                TimeHeader(windowStart, windowEnd, now, scrollState)
                LazyColumn(state = listState, modifier = Modifier.weight(1f)) {
                    itemsIndexed(rows) { rowIndex, row ->
                        GuideRow(
                            row = row,
                            windowStart = windowStart,
                            now = now,
                            scrollState = scrollState,
                            onFocusCell = { slot ->
                                if (anchorTime < slot.startMillis || anchorTime >= slot.stopMillis) {
                                    anchorTime = maxOf(slot.startMillis, windowStart)
                                }
                                focusedCell = row.channel to (slot as? GuideSlot.Show)?.program
                            },
                            onClickCell = { onTune(row.channel) },
                            onLongClickShow = { program -> onToggleReminder(row.channel, program) },
                            reminderKeys = reminderKeys,
                            onMoveVertical = { direction -> focusRow(rowIndex + direction) },
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun HeroPanel(focused: Pair<ChannelGuide, Program?>?) {
    Box(Modifier.fillMaxWidth().height(220.dp)) {
        val backdrop = focused?.second?.backdrop_url
        if (backdrop != null) {
            // Backdrop sits behind the text on the left; the right side stays
            // clear for the mini player.
            Box(Modifier.fillMaxHeight().fillMaxWidth(0.55f).align(Alignment.CenterStart)) {
                AsyncImage(
                    model = backdrop,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
                Box(
                    Modifier.matchParentSize()
                        .background(
                            Brush.horizontalGradient(
                                0.0f to AppBackground.copy(alpha = 0.88f),
                                0.6f to AppBackground.copy(alpha = 0.72f),
                                1.0f to AppBackground,
                            )
                        )
                )
                Box(
                    Modifier.matchParentSize()
                        .background(
                            Brush.verticalGradient(
                                0.6f to Color.Transparent,
                                1.0f to AppBackground,
                            )
                        )
                )
            }
        }
        Column(
            Modifier.align(Alignment.CenterStart).padding(start = 40.dp).fillMaxWidth(0.52f)
        ) {
            if (focused != null) {
                val (channel, program) = focused
                Text(
                    "${channel.physical_channel ?: ""} · ${channel.displayName}",
                    color = JellyfinBlue,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    program?.title ?: "No program information",
                    color = if (program != null) TextPrimary else TextSecondary,
                    fontSize = 26.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                val episodeLine = program?.episodeLine() ?: ""
                if (episodeLine.isNotEmpty() || program?.flag != null) {
                    Spacer(Modifier.height(2.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        FlagBadge(program?.flag)
                        if (program?.flag != null && episodeLine.isNotEmpty()) Spacer(Modifier.width(8.dp))
                        if (episodeLine.isNotEmpty()) {
                            Text(
                                episodeLine,
                                color = JellyfinBlue,
                                fontSize = 14.sp,
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                }
                val start = XmltvTime.parse(program?.start)
                val stop = XmltvTime.parse(program?.stop)
                if (start != null && stop != null) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "${formatDay(start)} · ${formatClock(start)} – ${formatClock(stop)}",
                        color = TextSecondary,
                        fontSize = 13.sp,
                    )
                }
                if (!program?.description.isNullOrBlank()) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        program?.description ?: "",
                        color = TextSecondary,
                        fontSize = 13.sp,
                        lineHeight = 18.sp,
                        maxLines = 3,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            } else {
                Text("Program Guide", color = TextPrimary, fontSize = 26.sp, fontWeight = FontWeight.Bold)
            }
        }
        // Frame around the transparent hole where the live video shows through
        Box(
            Modifier.align(Alignment.TopEnd)
                .padding(top = GUIDE_MINI_PLAYER_TOP, end = GUIDE_MINI_PLAYER_END)
                .size(GUIDE_MINI_PLAYER_WIDTH, GUIDE_MINI_PLAYER_HEIGHT)
                .border(1.dp, Color(0x44FFFFFF))
        )
    }
}

@Composable
private fun TimeHeader(windowStart: Long, windowEnd: Long, now: Long, scrollState: ScrollState) {
    Row(Modifier.fillMaxWidth().height(26.dp)) {
        Box(Modifier.width(CHANNEL_COL_WIDTH))
        Box(Modifier.weight(1f).horizontalScroll(scrollState, enabled = false)) {
            Row {
                var t = windowStart
                while (t < windowEnd) {
                    Box(Modifier.width(widthFor(t, t + HALF_HOUR_MS))) {
                        Text(
                            formatClock(t),
                            color = TextSecondary,
                            fontSize = 11.sp,
                            modifier = Modifier.padding(start = 4.dp),
                        )
                    }
                    t += HALF_HOUR_MS
                }
            }
            NowLine(windowStart, now)
        }
    }
}

@Composable
private fun NowLine(windowStart: Long, now: Long) {
    Box(
        Modifier.offset(x = widthFor(windowStart, now))
            .width(2.dp)
            .fillMaxHeight()
            .background(JellyfinBlue.copy(alpha = 0.8f))
    )
}

/** Consume vertical D-pad presses so the grid can do time-anchored row jumps. */
private fun Modifier.verticalGuideNav(onMoveVertical: (Int) -> Unit): Modifier =
    onKeyEvent { event ->
        when (event.nativeKeyEvent.keyCode) {
            KeyEvent.KEYCODE_DPAD_DOWN -> {
                if (event.type == KeyEventType.KeyDown) onMoveVertical(1)
                true
            }
            KeyEvent.KEYCODE_DPAD_UP -> {
                if (event.type == KeyEventType.KeyDown) onMoveVertical(-1)
                true
            }
            else -> false
        }
    }

@Composable
private fun GuideRow(
    row: GridRow,
    windowStart: Long,
    now: Long,
    scrollState: ScrollState,
    onFocusCell: (GuideSlot) -> Unit,
    onClickCell: () -> Unit,
    onLongClickShow: (Program) -> Unit,
    reminderKeys: Set<String>,
    onMoveVertical: (Int) -> Unit,
) {
    val channel = row.channel
    Row(Modifier.fillMaxWidth().height(ROW_HEIGHT)) {
        Row(
            Modifier.width(CHANNEL_COL_WIDTH).fillMaxHeight().padding(horizontal = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (channel.logo_url != null) {
                AsyncImage(
                    model = channel.logo_url,
                    contentDescription = null,
                    modifier = Modifier.size(34.dp),
                )
                Spacer(Modifier.width(8.dp))
            }
            Column {
                Text(
                    channel.physical_channel ?: "",
                    color = TextPrimary,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    channel.displayName,
                    color = TextSecondary,
                    fontSize = 11.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
        Box(Modifier.weight(1f).horizontalScroll(scrollState)) {
            Row {
                row.slots.forEachIndexed { index, slot ->
                    when (slot) {
                        is GuideSlot.Show ->
                            ProgramBlock(
                                slot = slot,
                                now = now,
                                focusRequester = row.requesters[index],
                                hasReminder =
                                    "${normalizeChannelNumber(channel.physical_channel)}|${XmltvTime.parse(slot.program.start)}" in reminderKeys,
                                onFocus = { onFocusCell(slot) },
                                onClick = onClickCell,
                                onLongClick = { onLongClickShow(slot.program) },
                                onMoveVertical = onMoveVertical,
                            )
                        is GuideSlot.Gap ->
                            GapBlock(
                                slot = slot,
                                focusRequester = row.requesters[index],
                                onFocus = { onFocusCell(slot) },
                                onClick = onClickCell,
                                onMoveVertical = onMoveVertical,
                            )
                    }
                }
            }
            NowLine(windowStart, now)
        }
    }
}

@Composable
private fun ProgramBlock(
    slot: GuideSlot.Show,
    now: Long,
    focusRequester: FocusRequester,
    hasReminder: Boolean,
    onFocus: () -> Unit,
    onClick: () -> Unit,
    onLongClick: () -> Unit,
    onMoveVertical: (Int) -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    val isAiring = now in slot.startMillis until slot.stopMillis
    val isPast = slot.stopMillis <= now
    val isFuture = slot.startMillis > now
    Box(
        Modifier.width(widthFor(slot.startMillis, slot.stopMillis))
            .fillMaxHeight()
            .padding(2.dp)
            .focusRequester(focusRequester)
            .onFocusChanged { if (it.isFocused) onFocus() }
            // Long-press OK on a future show toggles its reminder; a current
            // show always tunes straight in — never a menu.
            .onPreviewKeyEvent { event ->
                val isOk =
                    event.nativeKeyEvent.keyCode == KeyEvent.KEYCODE_DPAD_CENTER ||
                        event.nativeKeyEvent.keyCode == KeyEvent.KEYCODE_ENTER
                if (isOk && isFuture && event.type == KeyEventType.KeyUp &&
                    event.nativeKeyEvent.eventTime - event.nativeKeyEvent.downTime >= 600
                ) {
                    onLongClick()
                    true
                } else {
                    false
                }
            }
            .verticalGuideNav(onMoveVertical)
            .clip(RoundedCornerShape(6.dp))
            .background(
                when {
                    focused -> Color.White
                    isAiring -> SurfaceLight
                    else -> SurfaceColor
                }
            )
            .then(
                if (isAiring && !focused) Modifier.border(1.dp, JellyfinBlue.copy(alpha = 0.6f), RoundedCornerShape(6.dp))
                else Modifier
            )
            .clickable(interactionSource = interaction, indication = null, onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 8.dp)
    ) {
        Column {
            Text(
                (if (hasReminder) "⏰ " else "") + (slot.program.title ?: ""),
                color =
                    when {
                        focused -> Color.Black
                        isPast -> TextSecondary
                        else -> TextPrimary
                    },
                fontSize = 13.sp,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            val flagSuffix = slot.program.flag?.let { " · $it" } ?: ""
            Text(
                "${formatClock(slot.startMillis)} – ${formatClock(slot.stopMillis)}$flagSuffix",
                color = if (focused) Color(0xB3000000) else TextSecondary,
                fontSize = 10.sp,
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun GapBlock(
    slot: GuideSlot.Gap,
    focusRequester: FocusRequester,
    onFocus: () -> Unit,
    onClick: () -> Unit,
    onMoveVertical: (Int) -> Unit,
) {
    // Focusable like a program cell — otherwise a channel with no listings
    // becomes a dead zone the D-pad can't move through.
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    Box(
        Modifier.width(widthFor(slot.startMillis, slot.stopMillis))
            .fillMaxHeight()
            .padding(2.dp)
            .focusRequester(focusRequester)
            .onFocusChanged { if (it.isFocused) onFocus() }
            .verticalGuideNav(onMoveVertical)
            .clip(RoundedCornerShape(6.dp))
            .background(if (focused) Color.White else Color(0xFF161616))
            .clickable(interactionSource = interaction, indication = null, onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 8.dp)
    ) {
        Text(
            "No listings",
            color = if (focused) Color(0xB3000000) else TextSecondary.copy(alpha = 0.35f),
            fontSize = 11.sp,
            maxLines = 1,
        )
    }
}
