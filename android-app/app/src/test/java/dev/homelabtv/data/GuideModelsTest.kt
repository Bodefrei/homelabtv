package dev.homelabtv.data

import java.util.TimeZone
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class GuideModelsTest {

    @Before
    fun forceUtc() {
        TimeZone.setDefault(TimeZone.getTimeZone("UTC"))
    }

    @Test
    fun `parses xmltv time with timezone`() {
        // 2026-07-14T12:00:00Z
        assertEquals(1784030400000L, XmltvTime.parse("20260714120000 +0000"))
    }

    @Test
    fun `parses xmltv time without timezone as local time`() {
        assertEquals(1784030400000L, XmltvTime.parse("20260714120000"))
    }

    @Test
    fun `rejects garbage timestamps`() {
        assertNull(XmltvTime.parse(null))
        assertNull(XmltvTime.parse(""))
        assertNull(XmltvTime.parse("not a time"))
    }

    @Test
    fun `normalizes dash and dot channel numbers to match`() {
        assertEquals(normalizeChannelNumber("7-1"), normalizeChannelNumber("7.1"))
    }

    @Test
    fun `prefix completion is lineup aware`() {
        val majors = setOf("6", "10", "13", "20", "61")
        assertFalse(numberPrefixComplete("6", majors)) // 61 exists, must wait
        assertFalse(numberPrefixComplete("1", majors)) // 10 and 13 exist
        assertTrue(numberPrefixComplete("13", majors))
        assertTrue(numberPrefixComplete("4", majors)) // nothing starts with 4
        assertTrue(numberPrefixComplete("61", majors))

        val minors = setOf("1", "2", "10", "12")
        assertFalse(numberPrefixComplete("1", minors)) // could become 10 or 12
        assertTrue(numberPrefixComplete("2", minors))
        assertTrue(numberPrefixComplete("12", minors))
        assertTrue(numberPrefixComplete("3", minors)) // nonexistent, but unambiguous
    }

    @Test
    fun `channel numbers sort by subchannel count not decimal value`() {
        val numbers = listOf("6.12", "6.6", "20.1", "6.10", "6.7", "13.4", "6.2")
        val sorted = numbers.sortedWith(compareBy({ channelMajor(it) }, { channelMinor(it) }))
        assertEquals(listOf("6.2", "6.6", "6.7", "6.10", "6.12", "13.4", "20.1"), sorted)
    }

    @Test
    fun `inventory hash matches backend encoding and ignores input ordering`() {
        val channel =
            DeviceChannelInventoryEntry(
                channel_id = 101,
                input_id = "tuner/input",
                display_number = "7.1",
                display_name = "Éxample",
                original_display_name = null,
                service_type = "AUDIO_VIDEO",
                channel_type = "TYPE_ATSC_T",
                network_id = 4660,
                transport_stream_id = 22136,
                service_id = 101,
                internal_provider_id = "service-101",
            )
        val anotherChannel = channel.copy(channel_id = 102, display_number = "7.2")

        assertEquals(
            "6b2da8c414ffe967357146676914941bc6f279c2fdee06679ab04716cbd54419",
            calculateDeviceChannelInventoryHash(listOf(channel)),
        )
        assertEquals(
            calculateDeviceChannelInventoryHash(listOf(channel, anotherChannel)),
            calculateDeviceChannelInventoryHash(listOf(anotherChannel, channel)),
        )
    }

    @Test
    fun `buildSlots fills gaps and clips to the window`() {
        val hour = 60 * 60 * 1000L
        val windowStart = 1784030400000L // 12:00Z
        val windowEnd = windowStart + 4 * hour

        val programs =
            listOf(
                // Started before the window, ends 13:00Z -> clipped at window start
                Program("Early Show", "20260714110000 +0000", "20260714130000 +0000", null, null, null),
                // 14:00-15:00Z, leaving a 13:00-14:00Z hole
                Program("Late Show", "20260714140000 +0000", "20260714150000 +0000", null, null, null),
            )

        val slots = buildSlots(programs, windowStart, windowEnd)

        assertEquals(4, slots.size)
        val first = slots[0] as GuideSlot.Show
        assertEquals("Early Show", first.program.title)
        assertEquals(windowStart, first.startMillis)
        assertEquals(windowStart + hour, first.stopMillis)

        val gap = slots[1] as GuideSlot.Gap
        assertEquals(windowStart + hour, gap.startMillis)
        assertEquals(windowStart + 2 * hour, gap.stopMillis)

        val second = slots[2] as GuideSlot.Show
        assertEquals("Late Show", second.program.title)

        val tailGap = slots[3] as GuideSlot.Gap
        assertEquals(windowEnd, tailGap.stopMillis)
    }

    @Test
    fun `programAt and programAfter find current and next shows`() {
        val guide =
            ChannelGuide(
                physical_channel = "7.1",
                xmltv_id = "abc",
                name = "ABC",
                logo_url = null,
                programs =
                    listOf(
                        Program("Now Showing", "20260714120000 +0000", "20260714130000 +0000", null, null, null),
                        Program("Up Next", "20260714130000 +0000", "20260714140000 +0000", null, null, null),
                    ),
            )
        val now = 1784030400000L + 10 * 60 * 1000L // 12:10Z
        assertEquals("Now Showing", guide.programAt(now)?.title)
        assertEquals("Up Next", guide.programAfter(now)?.title)
    }
}
