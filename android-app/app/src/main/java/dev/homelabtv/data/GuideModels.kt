package dev.homelabtv.data

import java.text.ParseException
import java.text.SimpleDateFormat
import java.util.Locale

data class Program(
    val title: String?,
    val start: String?,
    val stop: String?,
    val description: String?,
    val poster_url: String?,
    val backdrop_url: String?,
    /** Season/episode as text, e.g. "S2 E6" (from XMLTV episode-num) */
    val episode: String? = null,
    /** Episode name (from XMLTV sub-title) */
    val episode_title: String? = null,
    /** Airing status badge: "New", "Rerun", "Premiere", "Live", or "News" */
    val flag: String? = null,
)

/** "Episode Name · S2 E6" — whatever parts exist, episode name first. */
fun Program.episodeLine(): String =
    listOfNotNull(episode_title, episode).joinToString(" · ")

data class ChannelGuide(
    val physical_channel: String?,
    val xmltv_id: String?,
    val name: String?,
    val logo_url: String?,
    val programs: List<Program>?,
) {
    val safePrograms: List<Program> get() = programs ?: emptyList()
    val displayName: String get() = name ?: physical_channel ?: "?"
}

/** XMLTV timestamps look like "20260714183000 +0000" (timezone optional). */
object XmltvTime {
    fun parse(value: String?): Long? {
        val v = value?.trim() ?: return null
        if (v.length < 14) return null
        val base = v.substring(0, 14)
        val tz = v.substring(14).trim()
        return try {
            if (tz.isNotEmpty()) {
                SimpleDateFormat("yyyyMMddHHmmss Z", Locale.US).parse("$base $tz")?.time
            } else {
                SimpleDateFormat("yyyyMMddHHmmss", Locale.US).parse(base)?.time
            }
        } catch (e: ParseException) {
            null
        }
    }
}

fun ChannelGuide.programAt(timeMillis: Long): Program? =
    safePrograms.firstOrNull { p ->
        val s = XmltvTime.parse(p.start) ?: return@firstOrNull false
        val e = XmltvTime.parse(p.stop) ?: return@firstOrNull false
        timeMillis in s until e
    }

fun ChannelGuide.programAfter(timeMillis: Long): Program? =
    safePrograms
        .mapNotNull { p -> XmltvTime.parse(p.start)?.let { it to p } }
        .filter { it.first > timeMillis }
        .minByOrNull { it.first }
        ?.second

/** "7-1" (TvContract style) and "7.1" (mapping UI style) are the same channel. */
fun normalizeChannelNumber(value: String?): String = value?.trim()?.replace('-', '.') ?: ""

/**
 * Lineup-aware entry completion: a typed prefix is "complete" when no known
 * number is a longer extension of it — typing more digits could never reach a
 * different existing number, so the entry can commit immediately.
 * With majors {6, 10, 13}: "6" completes instantly, "1" must wait; add a 61
 * to the lineup and "6" starts waiting too.
 */
fun numberPrefixComplete(prefix: String, numbers: Set<String>): Boolean =
    numbers.none { it.length > prefix.length && it.startsWith(prefix) }

// Channel numbers sort as (major, minor) integer pairs, NOT as decimals —
// 6.12 is the twelfth subchannel and comes after 6.6, not before.
fun channelMajor(value: String?): Int =
    normalizeChannelNumber(value).substringBefore('.').toIntOrNull() ?: Int.MAX_VALUE

fun channelMinor(value: String?): Int =
    normalizeChannelNumber(value).substringAfter('.', "").toIntOrNull() ?: 0

fun List<ChannelGuide>.forChannel(channel: PhysicalChannel?): ChannelGuide? {
    channel ?: return null
    val number = normalizeChannelNumber(channel.displayNumber)
    return firstOrNull { normalizeChannelNumber(it.physical_channel) == number }
        ?: firstOrNull { it.name?.equals(channel.displayName, ignoreCase = true) == true }
}

/**
 * The guide should show every channel the tuner scanned, whether or not the server
 * has a mapping for it: physical channels come first (with server program data
 * merged in where a mapping matches), then any server-only channels that didn't
 * match a scanned channel.
 */
fun mergeGuide(physical: List<PhysicalChannel>, guide: List<ChannelGuide>): List<ChannelGuide> {
    if (physical.isEmpty()) return guide
    val used = mutableSetOf<ChannelGuide>()
    val rows = mutableListOf<ChannelGuide>()
    for (channel in physical) {
        val match = guide.forChannel(channel)
        if (match != null) used.add(match)
        rows.add(
            ChannelGuide(
                physical_channel = normalizeChannelNumber(channel.displayNumber),
                xmltv_id = match?.xmltv_id,
                name = match?.name ?: channel.displayName,
                logo_url = match?.logo_url,
                programs = match?.safePrograms ?: emptyList(),
            )
        )
    }
    rows.addAll(guide.filter { it !in used })
    return rows
}

/** One cell in the EPG timeline: either a real program or a gap with no listings. */
sealed interface GuideSlot {
    val startMillis: Long
    val stopMillis: Long

    data class Show(val program: Program, override val startMillis: Long, override val stopMillis: Long) : GuideSlot

    data class Gap(override val startMillis: Long, override val stopMillis: Long) : GuideSlot
}

/**
 * Turns a channel's program list into a contiguous run of slots covering
 * [windowStart, windowEnd) — overlaps clipped, holes filled with Gap slots —
 * so the timeline grid always lines up across channels.
 */
fun buildSlots(programs: List<Program>, windowStart: Long, windowEnd: Long): List<GuideSlot> {
    val parsed =
        programs
            .mapNotNull { p ->
                val s = XmltvTime.parse(p.start) ?: return@mapNotNull null
                val e = XmltvTime.parse(p.stop) ?: return@mapNotNull null
                if (e <= windowStart || s >= windowEnd || e <= s) null else Triple(p, s, e)
            }
            .sortedBy { it.second }

    val slots = mutableListOf<GuideSlot>()
    var cursor = windowStart
    for ((program, s, e) in parsed) {
        val start = maxOf(s, cursor)
        val stop = minOf(e, windowEnd)
        if (stop <= start) continue
        if (start > cursor) slots.add(GuideSlot.Gap(cursor, start))
        slots.add(GuideSlot.Show(program, start, stop))
        cursor = stop
    }
    if (cursor < windowEnd) slots.add(GuideSlot.Gap(cursor, windowEnd))
    return slots
}
