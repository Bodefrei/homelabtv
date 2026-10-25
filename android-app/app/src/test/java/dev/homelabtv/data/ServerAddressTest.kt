package dev.homelabtv.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ServerAddressTest {
    @Test
    fun hostnameAndIpGetHttpSchemeAndDefaultPort() {
        assertEquals("http://homelabtv.local:30060/", serverBaseUrl(" homelabtv.local "))
        assertEquals("http://192.168.1.20:30060/", serverBaseUrl("192.168.1.20"))
    }

    @Test
    fun bracketedIpv6AddressIsSupported() {
        assertEquals("http://[fd00::10]:30060/", serverBaseUrl("[fd00::10]"))
    }

    @Test
    fun rejectsSchemePortAndPaths() {
        assertThrows(IllegalArgumentException::class.java) { serverBaseUrl("http://server.local") }
        assertThrows(IllegalArgumentException::class.java) { serverBaseUrl("server.local:8080") }
        assertThrows(IllegalArgumentException::class.java) { serverBaseUrl("server.local/path") }
    }
}
