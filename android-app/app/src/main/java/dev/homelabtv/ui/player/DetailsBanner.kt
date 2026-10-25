package dev.homelabtv.ui.player

import android.media.tv.TvTrackInfo
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import dev.homelabtv.data.ChannelGuide
import dev.homelabtv.data.PhysicalChannel
import dev.homelabtv.data.Program
import dev.homelabtv.data.XmltvTime
import dev.homelabtv.data.episodeLine
import dev.homelabtv.data.programAfter
import dev.homelabtv.data.programAt
import dev.homelabtv.theme.JellyfinBlue
import dev.homelabtv.theme.SurfaceColor
import dev.homelabtv.theme.SurfaceLight
import dev.homelabtv.theme.TextPrimary
import dev.homelabtv.theme.TextSecondary
import dev.homelabtv.ui.components.FlagBadge
import dev.homelabtv.ui.components.formatClock
import dev.homelabtv.ui.components.formatDay
import java.util.Locale
import kotlinx.coroutines.delay

/**
 * Detailed top banner (second INFO press). Two tabs, Channels-DVR style:
 * Info — artwork, current show details, and a small "Next up" row;
 * Options — audio track and subtitle overrides for the live broadcast.
 */
@Composable
fun DetailsBanner(
    channel: PhysicalChannel?,
    guide: ChannelGuide?,
    playerState: TvPlayerState,
    modifier: Modifier = Modifier,
) {
    val now = System.currentTimeMillis()
    val current = guide?.programAt(now)
    val next = guide?.programAfter(now)
    var tab by remember { mutableIntStateOf(0) }
    val firstFocus = remember { FocusRequester() }
    LaunchedEffect(Unit) {
        delay(100)
        try {
            firstFocus.requestFocus()
        } catch (_: Exception) {}
    }

    Column(
        modifier
            .fillMaxWidth()
            .background(
                Brush.verticalGradient(
                    0.0f to Color(0xF5101010),
                    0.85f to Color(0xD9101010),
                    1.0f to Color.Transparent,
                )
            )
            .padding(horizontal = 48.dp)
            .padding(top = 24.dp, bottom = 56.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "${channel?.numberForDisplay ?: ""} · ${guide?.displayName ?: channel?.displayName ?: ""}",
                color = JellyfinBlue,
                fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.weight(1f))
            BannerTab("Info", selected = tab == 0, onClick = { tab = 0 }, focusRequester = firstFocus)
            Spacer(Modifier.width(8.dp))
            BannerTab("Options", selected = tab == 1, onClick = { tab = 1 })
            Spacer(Modifier.width(20.dp))
            Column(horizontalAlignment = Alignment.End) {
                Text(formatClock(now), color = TextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                Text(formatDay(now), color = TextSecondary, fontSize = 12.sp)
            }
        }
        Spacer(Modifier.height(12.dp))

        if (tab == 0) {
            InfoTab(current = current, next = next, now = now)
        } else {
            OptionsTab(playerState = playerState)
        }

        Spacer(Modifier.height(12.dp))
        Text("INFO or BACK to close", color = TextSecondary.copy(alpha = 0.5f), fontSize = 11.sp)
    }
}

@Composable
private fun InfoTab(current: Program?, next: Program?, now: Long) {
    Row {
        if (current?.poster_url != null) {
            AsyncImage(
                model = current.poster_url,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.size(width = 104.dp, height = 156.dp).clip(RoundedCornerShape(8.dp)),
            )
            Spacer(Modifier.width(20.dp))
        }
        Column(Modifier.weight(1f)) {
            Text(
                current?.title ?: "No guide data",
                color = TextPrimary,
                fontSize = 24.sp,
                fontWeight = FontWeight.Bold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            val episodeLine = current?.episodeLine() ?: ""
            if (episodeLine.isNotEmpty() || current?.flag != null) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    FlagBadge(current?.flag)
                    if (current?.flag != null && episodeLine.isNotEmpty()) Spacer(Modifier.width(8.dp))
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
            val start = XmltvTime.parse(current?.start)
            val stop = XmltvTime.parse(current?.stop)
            if (start != null && stop != null && stop > start) {
                Spacer(Modifier.height(6.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(formatClock(start), color = TextSecondary, fontSize = 12.sp)
                    Spacer(Modifier.width(8.dp))
                    Box(
                        Modifier.width(260.dp)
                            .height(4.dp)
                            .clip(RoundedCornerShape(2.dp))
                            .background(Color(0x33FFFFFF))
                    ) {
                        Box(
                            Modifier.height(4.dp)
                                .width((260 * ((now - start).toFloat() / (stop - start)).coerceIn(0f, 1f)).dp)
                                .background(JellyfinBlue)
                        )
                    }
                    Spacer(Modifier.width(8.dp))
                    Text(formatClock(stop), color = TextSecondary, fontSize = 12.sp)
                }
            }
            if (!current?.description.isNullOrBlank()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    current?.description ?: "",
                    color = TextSecondary,
                    fontSize = 13.sp,
                    lineHeight = 18.sp,
                    maxLines = 3,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.fillMaxWidth(0.8f),
                )
            }
            if (next != null) {
                Spacer(Modifier.height(12.dp))
                Text("NEXT UP", color = TextSecondary.copy(alpha = 0.6f), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(4.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (next.poster_url != null) {
                        AsyncImage(
                            model = next.poster_url,
                            contentDescription = null,
                            contentScale = ContentScale.Crop,
                            modifier = Modifier.size(width = 36.dp, height = 54.dp).clip(RoundedCornerShape(4.dp)),
                        )
                    } else {
                        Box(
                            Modifier.size(width = 36.dp, height = 54.dp)
                                .clip(RoundedCornerShape(4.dp))
                                .background(SurfaceColor)
                        )
                    }
                    Spacer(Modifier.width(10.dp))
                    Column {
                        Text(
                            next.title ?: "",
                            color = TextPrimary,
                            fontSize = 14.sp,
                            fontWeight = FontWeight.SemiBold,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        val nextStart = XmltvTime.parse(next.start)
                        val nextMeta =
                            listOfNotNull(
                                    next.flag,
                                    next.episode_title,
                                    next.episode,
                                    nextStart?.let { formatClock(it) },
                                )
                                .joinToString(" · ")
                        if (nextMeta.isNotEmpty()) {
                            Text(
                                nextMeta,
                                color = TextSecondary,
                                fontSize = 12.sp,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun OptionsTab(playerState: TvPlayerState) {
    val audioTracks = playerState.tracks.filter { it.type == TvTrackInfo.TYPE_AUDIO }
    val subtitleTracks = playerState.tracks.filter { it.type == TvTrackInfo.TYPE_SUBTITLE }
    Column {
        TrackRow(label = "Audio") {
            if (audioTracks.isEmpty()) {
                Text("No tracks reported", color = TextSecondary, fontSize = 12.sp)
            } else {
                audioTracks.forEachIndexed { index, track ->
                    TrackChip(
                        label = trackLabel(track, index),
                        selected = track.id == playerState.selectedAudioId,
                        onClick = { playerState.selectAudio(track.id) },
                    )
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        TrackRow(label = "Subtitles") {
            TrackChip(
                label = "Off",
                selected = playerState.selectedSubtitleId == null,
                onClick = { playerState.selectSubtitle(null) },
            )
            subtitleTracks.forEachIndexed { index, track ->
                TrackChip(
                    label = trackLabel(track, index),
                    selected = track.id == playerState.selectedSubtitleId,
                    onClick = { playerState.selectSubtitle(track.id) },
                )
            }
        }
    }
}

@Composable
private fun BannerTab(
    label: String,
    selected: Boolean,
    onClick: () -> Unit,
    focusRequester: FocusRequester? = null,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    Box(
        Modifier
            .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
            .clip(RoundedCornerShape(6.dp))
            .background(if (focused) Color.White else if (selected) SurfaceLight else Color.Transparent)
            .then(
                if (selected && !focused) Modifier.border(1.dp, JellyfinBlue.copy(alpha = 0.7f), RoundedCornerShape(6.dp))
                else Modifier
            )
            .clickable(interactionSource = interaction, indication = null, onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 6.dp)
    ) {
        Text(
            label,
            color = if (focused) Color.Black else TextPrimary,
            fontSize = 13.sp,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

@Composable
private fun TrackRow(label: String, content: @Composable () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = TextSecondary, fontSize = 13.sp, modifier = Modifier.width(90.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            content()
        }
    }
}

@Composable
private fun TrackChip(
    label: String,
    selected: Boolean,
    onClick: () -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    Box(
        Modifier
            .clip(RoundedCornerShape(50))
            .background(
                when {
                    focused -> Color.White
                    selected -> JellyfinBlue.copy(alpha = 0.25f)
                    else -> SurfaceLight
                }
            )
            .then(
                if (selected && !focused) Modifier.border(1.dp, JellyfinBlue, RoundedCornerShape(50))
                else Modifier
            )
            .clickable(interactionSource = interaction, indication = null, onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 6.dp)
    ) {
        Text(label, color = if (focused) Color.Black else TextPrimary, fontSize = 13.sp)
    }
}

private fun trackLabel(track: TvTrackInfo, index: Int): String {
    val language = track.language
    return if (!language.isNullOrBlank()) {
        Locale(language).displayLanguage.replaceFirstChar { it.uppercase() }
    } else {
        "Track ${index + 1}"
    }
}
