package dev.homelabtv.ui.player

import android.media.tv.TvContract
import android.media.tv.TvTrackInfo
import android.media.tv.TvView
import android.view.ViewGroup
import android.widget.FrameLayout
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import coil.compose.AsyncImage
import dev.homelabtv.data.ChannelGuide
import dev.homelabtv.data.PhysicalChannel
import dev.homelabtv.data.XmltvTime
import dev.homelabtv.data.episodeLine
import dev.homelabtv.data.programAfter
import dev.homelabtv.data.programAt
import dev.homelabtv.theme.JellyfinBlue
import dev.homelabtv.theme.SurfaceColor
import dev.homelabtv.theme.TextPrimary
import dev.homelabtv.theme.TextSecondary
import dev.homelabtv.ui.components.formatClock
import dev.homelabtv.ui.components.formatDay

/**
 * Holds a handle to the live TvView plus the audio/subtitle tracks the current
 * broadcast reports, so UI like the details banner can list and switch them.
 */
class TvPlayerState {
    internal var tvView: TvView? = null

    var tracks by mutableStateOf<List<TvTrackInfo>>(emptyList())
        internal set

    var selectedAudioId by mutableStateOf<String?>(null)
        internal set

    var selectedSubtitleId by mutableStateOf<String?>(null)
        internal set

    fun selectAudio(trackId: String) {
        try {
            tvView?.selectTrack(TvTrackInfo.TYPE_AUDIO, trackId)
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    /** null turns captions off. */
    fun selectSubtitle(trackId: String?) {
        try {
            tvView?.setCaptionEnabled(trackId != null)
            tvView?.selectTrack(TvTrackInfo.TYPE_SUBTITLE, trackId)
            if (trackId == null) selectedSubtitleId = null
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }
}

@Composable
fun rememberTvPlayerState(): TvPlayerState = remember { TvPlayerState() }

/** Full-screen live broadcast via TvView, or a friendly fallback when no tuner exists. */
@Composable
fun PlayerSurface(
    channel: PhysicalChannel?,
    fallbackInputId: String?,
    playerState: TvPlayerState,
    modifier: Modifier = Modifier,
) {
    Box(modifier.background(Color.Black)) {
        if (channel == null) {
            Column(
                modifier = Modifier.align(Alignment.Center),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text("No TV tuner detected", color = TextPrimary, fontSize = 20.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(8.dp))
                Text(
                    "Connect an antenna input, or use the guide from the menu",
                    color = TextSecondary,
                    fontSize = 14.sp,
                )
            }
        } else {
            AndroidView(
                factory = { ctx ->
                    TvView(ctx).apply {
                        layoutParams =
                            FrameLayout.LayoutParams(
                                ViewGroup.LayoutParams.MATCH_PARENT,
                                ViewGroup.LayoutParams.MATCH_PARENT,
                            )
                        playerState.tvView = this
                        setCallback(
                            object : TvView.TvInputCallback() {
                                override fun onTracksChanged(inputId: String, tracks: MutableList<TvTrackInfo>) {
                                    playerState.tracks = tracks.toList()
                                }

                                override fun onTrackSelected(inputId: String, type: Int, trackId: String?) {
                                    when (type) {
                                        TvTrackInfo.TYPE_AUDIO -> playerState.selectedAudioId = trackId
                                        TvTrackInfo.TYPE_SUBTITLE -> playerState.selectedSubtitleId = trackId
                                    }
                                }
                            }
                        )
                    }
                },
                modifier = Modifier.fillMaxSize(),
                update = { tvView ->
                    val inputId = channel.inputId ?: fallbackInputId
                    // tag guards against re-tuning on every recomposition
                    if (inputId != null && tvView.tag != channel.uri) {
                        try {
                            tvView.tune(inputId, channel.uri)
                            tvView.tag = channel.uri
                        } catch (e: Exception) {
                            e.printStackTrace()
                        }
                    }
                },
            )
        }
    }
}

/** Banner when a reminded show is starting: OK tunes to it, BACK dismisses. */
@Composable
fun ReminderOverlay(title: String, channelNumber: String, modifier: Modifier = Modifier) {
    Column(
        modifier
            .padding(top = 32.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(Color(0xF2161616))
            .padding(horizontal = 28.dp, vertical = 16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("⏰ STARTING NOW", color = JellyfinBlue, fontSize = 12.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(4.dp))
        Text(
            title,
            color = TextPrimary,
            fontSize = 20.sp,
            fontWeight = FontWeight.Bold,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        Text("on channel $channelNumber", color = TextSecondary, fontSize = 13.sp)
        Spacer(Modifier.height(8.dp))
        Text("OK · Watch      BACK · Dismiss", color = TextSecondary, fontSize = 12.sp)
    }
}

/** Big on-screen readout while typing a channel number on the remote, e.g. "20.-". */
@Composable
fun NumberEntryOverlay(entry: String, modifier: Modifier = Modifier) {
    if (entry.isEmpty()) return
    Box(
        modifier
            .padding(40.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(Color(0xCC101010))
            .padding(horizontal = 28.dp, vertical = 14.dp)
    ) {
        val display = if (entry.endsWith('.')) "$entry–" else entry
        Text(
            display,
            color = TextPrimary,
            fontSize = 40.sp,
            fontWeight = FontWeight.Bold,
        )
    }
}

/**
 * Channel-switch info bar: logo, number, current program with progress, up next.
 * [showClock] adds the system time and date on the right (INFO-triggered only).
 */
@Composable
fun ZapperOverlay(
    visible: Boolean,
    channel: PhysicalChannel?,
    guide: ChannelGuide?,
    showClock: Boolean = false,
    modifier: Modifier = Modifier,
) {
    AnimatedVisibility(
        visible = visible && channel != null,
        enter = fadeIn() + slideInVertically { it / 2 },
        exit = fadeOut() + slideOutVertically { it / 2 },
        modifier = modifier,
    ) {
        val now = System.currentTimeMillis()
        val current = guide?.programAt(now)
        val next = guide?.programAfter(now)
        var logoSource by remember(channel?.id, guide?.logo_url) {
            mutableStateOf(if (guide?.logo_url.isNullOrBlank()) 1 else 0)
        }
        Box(
            Modifier.fillMaxWidth()
                .background(Brush.verticalGradient(listOf(Color.Transparent, Color(0xE6000000))))
                .padding(horizontal = 48.dp, vertical = 32.dp)
        ) {
            Row(verticalAlignment = Alignment.Top) {
                Box(
                    Modifier.size(72.dp).clip(RoundedCornerShape(8.dp)).background(SurfaceColor),
                    contentAlignment = Alignment.Center,
                ) {
                    val logoModel =
                        when (logoSource) {
                            0 -> guide?.logo_url
                            1 -> channel?.let { TvContract.buildChannelLogoUri(it.id) }
                            else -> null
                        }
                    if (logoModel != null) {
                        AsyncImage(
                            model = logoModel,
                            contentDescription = null,
                            contentScale = ContentScale.Fit,
                            modifier = Modifier.fillMaxSize().padding(8.dp),
                            onError = {
                                logoSource = if (logoSource == 0 && channel != null) 1 else 2
                            },
                        )
                    } else {
                        Text(
                            channel?.numberForDisplay ?: "",
                            color = TextPrimary,
                            fontSize = 20.sp,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }
                Spacer(Modifier.width(20.dp))
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.Bottom) {
                        Text(
                            channel?.numberForDisplay ?: "",
                            color = JellyfinBlue,
                            fontSize = 22.sp,
                            fontWeight = FontWeight.Bold,
                        )
                        Spacer(Modifier.width(10.dp))
                        Text(
                            guide?.displayName ?: channel?.displayName ?: "",
                            color = TextSecondary,
                            fontSize = 16.sp,
                        )
                    }
                    Spacer(Modifier.height(4.dp))
                    val episodeLine = current?.episodeLine() ?: ""
                    Text(
                        buildAnnotatedString {
                            append(current?.title ?: "No guide data")
                            if (episodeLine.isNotEmpty()) {
                                withStyle(SpanStyle(color = TextSecondary, fontSize = 14.sp)) {
                                    append(" | ")
                                    append(episodeLine)
                                }
                            }
                        },
                        color = TextPrimary,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    val start = XmltvTime.parse(current?.start)
                    val stop = XmltvTime.parse(current?.stop)
                    if (start != null && stop != null && stop > start) {
                        Spacer(Modifier.height(8.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(formatClock(start), color = TextSecondary, fontSize = 12.sp)
                            Spacer(Modifier.width(8.dp))
                            Box(
                                Modifier.weight(1f)
                                    .height(4.dp)
                                    .clip(RoundedCornerShape(2.dp))
                                    .background(Color(0x33FFFFFF))
                            ) {
                                Box(
                                    Modifier.fillMaxHeight()
                                        .fillMaxWidth(((now - start).toFloat() / (stop - start)).coerceIn(0f, 1f))
                                        .background(JellyfinBlue)
                                )
                            }
                            Spacer(Modifier.width(8.dp))
                            Text(formatClock(stop), color = TextSecondary, fontSize = 12.sp)
                        }
                    }
                    if (next != null) {
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "Next · ${next.title}",
                            color = TextSecondary,
                            fontSize = 13.sp,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
                if (showClock) {
                    Spacer(Modifier.width(24.dp))
                    Column(horizontalAlignment = Alignment.End) {
                        Text(
                            formatClock(now),
                            color = TextPrimary,
                            fontSize = 22.sp,
                            fontWeight = FontWeight.Bold,
                        )
                        Text(
                            formatDay(now),
                            color = TextSecondary,
                            fontSize = 13.sp,
                        )
                    }
                }
            }
        }
    }
}
