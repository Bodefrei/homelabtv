package dev.homelabtv.ui.guide

import android.media.tv.TvContract
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
import androidx.compose.foundation.layout.BoxWithConstraints
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
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.snapshotFlow
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
import dev.homelabtv.data.PhysicalChannel
import dev.homelabtv.data.Program
import dev.homelabtv.data.XmltvTime
import dev.homelabtv.data.buildSlots
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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private const val GUIDE_TAG = "HomelabGuide"
private val CHANNEL_COL_WIDTH = 150.dp
private val ROW_HEIGHT = 80.dp
private const val HALF_HOUR_MS = 30L * 60 * 1000
private const val QUARTER_HOUR_MS = 15L * 60 * 1000
private const val GUIDE_WINDOW_MS = 24L * 60 * 60 * 1000
private const val GUIDE_CHUNK_MS = 3L * 60 * 60 * 1000
private const val GUIDE_VIEWPORT_MINUTES = 165f

// The live-video mini player floats over this corner of the hero panel; the
// actual TvView is positioned by MainActivity using these same values.
val GUIDE_MINI_PLAYER_WIDTH = 320.dp
val GUIDE_MINI_PLAYER_HEIGHT = 180.dp
val GUIDE_MINI_PLAYER_TOP = 20.dp
val GUIDE_MINI_PLAYER_END = 0.dp

private fun widthFor(startMillis: Long, stopMillis: Long, dpPerMinute: Float): Dp =
    (((stopMillis - startMillis) / 60000f) * dpPerMinute).dp

private object GuideSlotCache {
    private var cachedChannels: List<ChannelGuide>? = null
    private var cachedWindowStart: Long? = null
    private val cachedChunks = LinkedHashMap<Pair<Int, Int>, List<GuideSlot>>(32, 0.75f, true)

    @Synchronized
    fun get(channels: List<ChannelGuide>, windowStart: Long, rowIndex: Int, chunkIndex: Int): List<GuideSlot>? =
        if (cachedChannels === channels && cachedWindowStart == windowStart) cachedChunks[rowIndex to chunkIndex] else null

    @Synchronized
    fun put(channels: List<ChannelGuide>, windowStart: Long, rowIndex: Int, chunkIndex: Int, slots: List<GuideSlot>) {
        if (cachedChannels !== channels || cachedWindowStart != windowStart) {
            cachedChannels = channels
            cachedWindowStart = windowStart
            cachedChunks.clear()
        }
        cachedChunks[rowIndex to chunkIndex] = slots
    }
}

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
    physicalChannels: List<PhysicalChannel> = emptyList(),
    modifier: Modifier = Modifier,
    initialChannelNumber: String? = null,
    reminderKeys: Set<String> = emptySet(),
    onToggleReminder: (ChannelGuide, Program) -> Unit = { _, _ -> },
) {
    val now = remember { System.currentTimeMillis() }
    val windowStart = remember(now) {
        val minimumStart = now - QUARTER_HOUR_MS
        minimumStart - minimumStart % QUARTER_HOUR_MS
    }
    val windowEnd = windowStart + GUIDE_WINDOW_MS
    var loadedChunkCount by remember(channels, windowStart) { mutableStateOf(1) }
    val scrollState = rememberScrollState()
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()

    val preparedSlots = remember(channels, windowStart) { mutableStateMapOf<Int, List<GuideSlot>>() }
    val rowRequesters = remember(channels, windowStart) { mutableStateMapOf<Int, List<FocusRequester>>() }
    val initialRow =
        remember(channels, initialChannelNumber) {
            val target = normalizeChannelNumber(initialChannelNumber)
            if (target.isEmpty()) 0
            else channels.indexOfFirst { normalizeChannelNumber(it.physical_channel) == target }.let { if (it >= 0) it else 0 }
        }

    var anchorTime by remember { mutableStateOf(now) }
    var hasPreparedInitialRow by remember(channels, windowStart, initialRow) { mutableStateOf(false) }
    // Program is null when a "No listings" gap cell is focused
    var focusedCell by remember { mutableStateOf<Pair<ChannelGuide, Program?>?>(null) }

    val focusRow: (Int) -> Unit = { rowIndex ->
        if (rowIndex in channels.indices) {
            scope.launch {
                listState.scrollToItem(rowIndex)
                repeat(20) { attempt ->
                    val slots = preparedSlots[rowIndex]
                    val requesters = rowRequesters[rowIndex]
                    if (slots != null && requesters != null) {
                        val slotIndex =
                            slots.indexOfFirst { anchorTime >= it.startMillis && anchorTime < it.stopMillis }
                                .let { if (it >= 0) it else 0 }
                        try {
                            requesters[slotIndex].requestFocus()
                            Log.d(GUIDE_TAG, "focusRow($rowIndex) slot=$slotIndex ok on attempt $attempt")
                            return@launch
                        } catch (e: Exception) {
                            Log.d(GUIDE_TAG, "focusRow($rowIndex) attempt $attempt failed: $e")
                        }
                    }
                    delay(50)
                }
                Log.w(GUIDE_TAG, "focusRow($rowIndex) timed out waiting for lazy row")
            }
        }
    }

    LaunchedEffect(channels, initialRow, windowStart) {
        if (channels.isNotEmpty()) {
            listState.scrollToItem(initialRow)
            focusRow(initialRow)
        }
    }

    LaunchedEffect(channels, initialRow, windowStart, loadedChunkCount, hasPreparedInitialRow) {
        if (!hasPreparedInitialRow) return@LaunchedEffect

        val rowOrder = channels.indices.sortedBy { kotlin.math.abs(it - initialRow) }
        for (rowIndex in rowOrder) {
            for (chunkIndex in 0 until loadedChunkCount) {
                if (GuideSlotCache.get(channels, windowStart, rowIndex, chunkIndex) == null) {
                    val chunkStart = windowStart + chunkIndex * GUIDE_CHUNK_MS
                    val chunkEnd = minOf(chunkStart + GUIDE_CHUNK_MS, windowEnd)
                    val slots = withContext(Dispatchers.Default) {
                        buildSlots(channels[rowIndex].safePrograms, chunkStart, chunkEnd)
                    }
                    GuideSlotCache.put(channels, windowStart, rowIndex, chunkIndex, slots)
                }
            }
            delay(10)
        }
    }

    LaunchedEffect(scrollState, windowEnd) {
        snapshotFlow { scrollState.value to scrollState.maxValue }
            .collect { (scrollPosition, maxScroll) ->
                if (scrollPosition > 0 && maxScroll - scrollPosition <= 300 &&
                    windowStart + loadedChunkCount * GUIDE_CHUNK_MS < windowEnd
                ) {
                    loadedChunkCount++
                }
            }
    }

    BoxWithConstraints(modifier.fillMaxSize()) {
      val dpPerMinute = ((maxWidth - CHANNEL_COL_WIDTH).value / GUIDE_VIEWPORT_MINUTES).coerceAtLeast(1f)
      Column(Modifier.fillMaxSize()) {
        // Hero is transparent in the top-right corner so the underlying TvView
        // (positioned there by MainActivity) shows through as a mini player.
        HeroPanel(focusedCell)
        Column(Modifier.weight(1f).fillMaxWidth().background(AppBackground)) {
            if (channels.isEmpty()) {
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
                val loadedEnd = minOf(windowStart + loadedChunkCount * GUIDE_CHUNK_MS, windowEnd)
                TimeHeader(windowStart, loadedEnd, now, scrollState, dpPerMinute)
                LazyColumn(state = listState, modifier = Modifier.weight(1f)) {
                    itemsIndexed(
                        items = channels,
                        key = { index, channel -> channel.physical_channel ?: channel.xmltv_id ?: index.toString() },
                    ) { rowIndex, channel ->
                        val rowSlots = mutableListOf<GuideSlot>()
                        val rowRequestersForSlots = mutableListOf<FocusRequester>()
                        repeat(loadedChunkCount) { chunkIndex ->
                            val chunkStart = windowStart + chunkIndex * GUIDE_CHUNK_MS
                            val chunkEnd = minOf(chunkStart + GUIDE_CHUNK_MS, windowEnd)
                            var chunkSlots by remember(channels, rowIndex, windowStart, chunkIndex) {
                                mutableStateOf(
                                    GuideSlotCache.get(channels, windowStart, rowIndex, chunkIndex)
                                )
                            }
                            LaunchedEffect(channels, rowIndex, windowStart, chunkIndex) {
                                if (chunkSlots == null) {
                                    val calculated = withContext(Dispatchers.Default) {
                                        buildSlots(channel.safePrograms, chunkStart, chunkEnd)
                                    }
                                    GuideSlotCache.put(channels, windowStart, rowIndex, chunkIndex, calculated)
                                    chunkSlots = calculated
                                }
                            }
                            if (chunkSlots != null) {
                                val chunkRequesters = remember(channels, rowIndex, chunkIndex, chunkSlots!!.size) {
                                    chunkSlots!!.map { FocusRequester() }
                                }
                                rowSlots.addAll(chunkSlots!!)
                                rowRequestersForSlots.addAll(chunkRequesters)
                            }
                        }
                        if (rowSlots.isEmpty()) {
                            Box(Modifier.fillMaxWidth().height(ROW_HEIGHT))
                        } else {
                            SideEffect {
                                preparedSlots[rowIndex] = rowSlots
                                rowRequesters[rowIndex] = rowRequestersForSlots
                                if (rowIndex == initialRow) hasPreparedInitialRow = true
                            }
                            DisposableEffect(channels, rowIndex, windowStart) {
                                onDispose {
                                    preparedSlots.remove(rowIndex)
                                    rowRequesters.remove(rowIndex)
                                }
                            }
                            GuideRow(
                                channel = channel,
                                physicalChannel =
                                    physicalChannels.firstOrNull {
                                        normalizeChannelNumber(it.displayNumber) ==
                                            normalizeChannelNumber(channel.physical_channel)
                                    },
                                slots = rowSlots,
                                requesters = rowRequestersForSlots,
                                windowStart = windowStart,
                                now = now,
                                scrollState = scrollState,
                                dpPerMinute = dpPerMinute,
                                onFocusCell = { slot ->
                                    if (anchorTime < slot.startMillis || anchorTime >= slot.stopMillis) {
                                        anchorTime = maxOf(slot.startMillis, windowStart)
                                    }
                                    focusedCell = channel to (slot as? GuideSlot.Show)?.program
                                },
                                onClickCell = { onTune(channel) },
                                onLongClickShow = { program -> onToggleReminder(channel, program) },
                                reminderKeys = reminderKeys,
                                onMoveVertical = { direction -> focusRow(rowIndex + direction) },
                            )
                        }
                    }
                }
            }
        }
      }
      if (channels.isNotEmpty() && !hasPreparedInitialRow) {
          Box(
              Modifier.fillMaxSize().background(AppBackground.copy(alpha = 0.94f)),
              contentAlignment = Alignment.Center,
          ) {
              Column(horizontalAlignment = Alignment.CenterHorizontally) {
                  CircularProgressIndicator(color = JellyfinBlue)
                  Spacer(Modifier.height(16.dp))
                  Text("EPG wird vorbereitet …", color = TextPrimary, fontSize = 16.sp)
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
            Modifier.align(Alignment.CenterStart).padding(start = 40.dp).fillMaxWidth(0.62f)
        ) {
            if (focused != null) {
                val (channel, program) = focused
                Text(
                    "${channel.display_number ?: channel.physical_channel ?: ""} · ${channel.displayName}",
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
                    maxLines = 2,
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
                        fontSize = 14.sp,
                        lineHeight = 18.sp,
                        maxLines = 4,
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
private fun TimeHeader(
    windowStart: Long,
    windowEnd: Long,
    now: Long,
    scrollState: ScrollState,
    dpPerMinute: Float,
) {
    Row(Modifier.fillMaxWidth().height(38.dp)) {
        Box(Modifier.width(CHANNEL_COL_WIDTH))
        Box(Modifier.weight(1f).horizontalScroll(scrollState, enabled = false)) {
            Box(Modifier.width(widthFor(windowStart, windowEnd, dpPerMinute)).fillMaxHeight()) {
                var t = (windowStart / HALF_HOUR_MS + 1) * HALF_HOUR_MS
                while (t < windowEnd) {
                    val position = widthFor(windowStart, t, dpPerMinute)
                    if (formatClock(t) == "00:00") {
                        Column(Modifier.offset(x = position).padding(start = 4.dp)) {
                            Text(
                                formatClock(t),
                                color = TextSecondary,
                                fontSize = 11.sp,
                            )
                            Text(
                                formatDay(t),
                                color = TextSecondary,
                                fontSize = 10.sp,
                                maxLines = 1,
                            )
                        }
                    } else {
                        Text(
                            formatClock(t),
                            color = TextSecondary,
                            fontSize = 11.sp,
                            modifier = Modifier.offset(x = position).padding(start = 4.dp),
                        )
                    }
                    t += HALF_HOUR_MS
                }
                NowLine(windowStart, now, dpPerMinute)
            }
        }
    }
}

@Composable
private fun NowLine(windowStart: Long, now: Long, dpPerMinute: Float) {
    Box(
        Modifier.offset(x = widthFor(windowStart, now, dpPerMinute))
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
    channel: ChannelGuide,
    physicalChannel: PhysicalChannel?,
    slots: List<GuideSlot>,
    requesters: List<FocusRequester>,
    windowStart: Long,
    now: Long,
    scrollState: ScrollState,
    dpPerMinute: Float,
    onFocusCell: (GuideSlot) -> Unit,
    onClickCell: () -> Unit,
    onLongClickShow: (Program) -> Unit,
    reminderKeys: Set<String>,
    onMoveVertical: (Int) -> Unit,
) {
    var logoSource by remember(channel.physical_channel, channel.logo_url) {
        mutableStateOf(if (channel.logo_url.isNullOrBlank()) 1 else 0)
    }
    Row(Modifier.fillMaxWidth().height(ROW_HEIGHT)) {
        Row(
            Modifier.width(CHANNEL_COL_WIDTH).fillMaxHeight().padding(horizontal = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            val logoModel =
                when (logoSource) {
                    0 -> channel.logo_url
                    1 -> physicalChannel?.let { TvContract.buildChannelLogoUri(it.id) }
                    else -> null
                }
            Box(
                Modifier.size(34.dp).clip(RoundedCornerShape(4.dp)).background(SurfaceLight),
                contentAlignment = Alignment.Center,
            ) {
                if (logoModel != null) {
                    AsyncImage(
                        model = logoModel,
                        contentDescription = null,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier.fillMaxSize().padding(3.dp),
                        onError = {
                            logoSource = if (logoSource == 0 && physicalChannel != null) 1 else 2
                        },
                    )
                } else {
                    Text(
                        channel.display_number ?: channel.physical_channel ?: "",
                        color = TextSecondary,
                        fontSize = 9.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Clip,
                    )
                }
            }
            Spacer(Modifier.width(8.dp))
            Column {
                Text(
                    channel.display_number ?: channel.physical_channel ?: "",
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
                slots.forEachIndexed { index, slot ->
                    when (slot) {
                        is GuideSlot.Show ->
                            ProgramBlock(
                                slot = slot,
                                now = now,
                                focusRequester = requesters[index],
                                hasReminder =
                                    "${normalizeChannelNumber(channel.physical_channel)}|${XmltvTime.parse(slot.program.start)}" in reminderKeys,
                                onFocus = { onFocusCell(slot) },
                                onClick = onClickCell,
                                onLongClick = { onLongClickShow(slot.program) },
                                onMoveVertical = onMoveVertical,
                                dpPerMinute = dpPerMinute,
                            )
                        is GuideSlot.Gap ->
                            GapBlock(
                                slot = slot,
                                focusRequester = requesters[index],
                                onFocus = { onFocusCell(slot) },
                                onClick = onClickCell,
                                onMoveVertical = onMoveVertical,
                                dpPerMinute = dpPerMinute,
                            )
                    }
                }
            }
            NowLine(windowStart, now, dpPerMinute)
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
    dpPerMinute: Float,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    val isAiring = now in slot.startMillis until slot.stopMillis
    val isPast = slot.stopMillis <= now
    val isFuture = slot.startMillis > now
    val isRerun = slot.program.flag.equals("Rerun", ignoreCase = true)
    val compactFlag = when {
        isRerun -> "↻"
        slot.program.flag.isNullOrBlank() -> ""
        else -> slot.program.flag.take(1).uppercase()
    }
    Box(
        Modifier.width(widthFor(slot.startMillis, slot.stopMillis, dpPerMinute))
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
            .padding(2.dp)
    ) {
        BoxWithConstraints(Modifier.fillMaxSize()) {
            val cellWidth = maxWidth
            val horizontalPadding = if (cellWidth >= 110.dp) 8.dp else 2.dp
            val footer =
                when {
                    cellWidth >= 130.dp ->
                        "${formatClock(slot.startMillis)} – ${formatClock(slot.stopMillis)}" +
                            (slot.program.flag?.let { " · ${it.uppercase()}" } ?: "")
                    cellWidth >= 82.dp ->
                        "${formatClock(slot.startMillis)}–${formatClock(slot.stopMillis)}" +
                            (if (compactFlag.isNotEmpty()) " $compactFlag" else "")
                    else ->
                        formatClock(slot.startMillis) + (if (compactFlag.isNotEmpty()) " $compactFlag" else "")
                }
            Column(
                Modifier.fillMaxSize()
                    .padding(horizontal = horizontalPadding, vertical = 5.dp)
            ) {
                Text(
                    (if (hasReminder) "⏰ " else "") + (slot.program.title ?: ""),
                    color =
                        when {
                            focused -> Color.Black
                            isPast -> TextSecondary
                            else -> TextPrimary
                        },
                    fontSize = if (cellWidth >= 82.dp) 13.sp else 9.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.weight(1f))
                Text(
                    footer,
                    color = if (focused) Color(0xB3000000) else TextSecondary,
                    fontSize = if (cellWidth >= 130.dp) 10.sp else 9.sp,
                    maxLines = 1,
                    softWrap = false,
                )
            }
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
    dpPerMinute: Float,
) {
    // Focusable like a program cell — otherwise a channel with no listings
    // becomes a dead zone the D-pad can't move through.
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    Box(
        Modifier.width(widthFor(slot.startMillis, slot.stopMillis, dpPerMinute))
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
