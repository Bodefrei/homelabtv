package dev.homelabtv.data

import java.net.URI
import java.net.URISyntaxException

private const val SERVER_PORT = 30060

fun serverBaseUrl(value: String): String {
    val address = value.trim()
    require(address.isNotEmpty() && "://" !in address && '/' !in address && '?' !in address && '#' !in address) {
        "Bitte nur die IP-Adresse oder den Hostnamen ohne Protokoll oder Port eingeben."
    }
    val parsed =
        try {
            URI("http://$address")
        } catch (e: URISyntaxException) {
            throw IllegalArgumentException("Ungültige IP-Adresse oder ungültiger Hostname.", e)
        }
    require(!parsed.host.isNullOrBlank() && parsed.port == -1 && parsed.userInfo == null) {
        "Bitte nur die IP-Adresse oder den Hostnamen ohne Protokoll oder Port eingeben."
    }
    val host = parsed.host.removePrefix("[").removeSuffix("]")
    return URI("http", null, host, SERVER_PORT, "/", null, null).toString()
}
