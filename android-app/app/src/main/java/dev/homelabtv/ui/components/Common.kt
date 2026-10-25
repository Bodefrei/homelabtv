package dev.homelabtv.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.homelabtv.theme.JellyfinBlue
import dev.homelabtv.theme.JellyfinPurple
import dev.homelabtv.theme.LiveRed
import dev.homelabtv.theme.OfflineAmber
import dev.homelabtv.theme.OnlineGreen
import dev.homelabtv.theme.TextPrimary
import dev.homelabtv.theme.TextSecondary
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

fun formatClock(millis: Long): String = SimpleDateFormat("HH:mm", Locale.GERMANY).format(Date(millis))

fun formatDay(millis: Long): String = SimpleDateFormat("EEE, d. MMM", Locale.GERMANY).format(Date(millis))

fun ageText(sinceMillis: Long): String {
    val minutes = ((System.currentTimeMillis() - sinceMillis) / 60000L).coerceAtLeast(0)
    return when {
        minutes < 1 -> "just now"
        minutes < 60 -> "${minutes}m ago"
        minutes < 60 * 24 -> "${minutes / 60}h ago"
        else -> "${minutes / (60 * 24)}d ago"
    }
}

/** Accent color for an airing-status flag ("New", "Live", "Rerun", ...). */
fun flagColor(flag: String): Color =
    when (flag.lowercase(Locale.US)) {
        "new" -> OnlineGreen
        "live" -> LiveRed
        "premiere" -> JellyfinPurple
        "news" -> JellyfinBlue
        else -> TextSecondary // Rerun and anything unrecognized stay muted
    }

/** Airing-status chip ("NEW", "RERUN", "PREMIERE", ...); renders nothing without a flag. */
@Composable
fun FlagBadge(flag: String?, modifier: Modifier = Modifier) {
    if (flag.isNullOrBlank()) return
    val color = flagColor(flag)
    Text(
        flag.uppercase(Locale.US),
        color = color,
        fontSize = 10.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = 1.sp,
        modifier =
            modifier
                .clip(RoundedCornerShape(4.dp))
                .background(color.copy(alpha = 0.16f))
                .border(1.dp, color.copy(alpha = 0.7f), RoundedCornerShape(4.dp))
                .padding(horizontal = 6.dp, vertical = 1.dp),
    )
}

/** Small "Server online" / "Offline · guide from 3h ago" chip. */
@Composable
fun StatusPill(isOnline: Boolean, lastUpdatedMillis: Long?, modifier: Modifier = Modifier) {
    Row(
        modifier =
            modifier
                .clip(RoundedCornerShape(50))
                .background(Color(0x99000000))
                .padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier.size(8.dp)
                .clip(CircleShape)
                .background(if (isOnline) OnlineGreen else OfflineAmber)
        )
        Spacer(Modifier.width(8.dp))
        val text =
            when {
                isOnline -> "Server online"
                lastUpdatedMillis != null -> "Offline · guide from ${ageText(lastUpdatedMillis)}"
                else -> "Offline · no guide data"
            }
        Text(text, color = TextSecondary, fontSize = 12.sp)
    }
}

/** Jellyfin-style menu row: transparent at rest, white with black text when focused. */
@Composable
fun MenuListItem(
    label: String,
    icon: ImageVector,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    focusRequester: FocusRequester? = null,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    Row(
        modifier =
            modifier
                .fillMaxWidth()
                .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
                .clip(RoundedCornerShape(8.dp))
                .background(if (focused) Color.White else Color.Transparent)
                .clickable(interactionSource = interaction, indication = null, onClick = onClick)
                .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            icon,
            contentDescription = null,
            tint = if (focused) Color.Black else JellyfinBlue,
            modifier = Modifier.size(20.dp),
        )
        Spacer(Modifier.width(16.dp))
        Text(label, color = if (focused) Color.Black else TextPrimary, fontSize = 16.sp)
    }
}
