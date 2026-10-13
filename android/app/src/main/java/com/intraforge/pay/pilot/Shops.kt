package com.intraforge.pay.pilot

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.net.URI
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.Base64
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/**
 * A shop this phone has paired with, by scanning the shop's "connect" QR in person.
 *
 * Every shop PC has its own certificate authority. The connect code carries the shop's name, its HTTPS addresses and
 * the pin (SHA-256 of the public key) of that CA; from then on the app trusts only a server whose certificate chain
 * leads to that exact CA. One app therefore works with any number of shop PCs, each identified by its shop name.
 */
@Serializable
data class ShopProfile(
    val name: String,
    @SerialName("merchant_id") val merchantId: String,
    val urls: List<String>,
    val pin: String,
    @SerialName("last_url") val lastUrl: String? = null,
) {
    /** The last address that worked first, then the others in the shop's own order. */
    fun addressesToTry(): List<String> = listOfNotNull(lastUrl) + urls.filter { it != lastUrl }

    companion object {
        const val PREFIX = "IFPAYSHOP1."
        private val PIN = Regex("^sha256/[A-Za-z0-9+/]{43}=$")

        fun looksLikeConnectCode(text: String) = PilotApi.cleanPayCode(text).startsWith(PREFIX)

        /** Parses and validates a connect code; anything unexpected is refused rather than guessed. */
        fun fromConnectCode(text: String): ShopProfile {
            val code = PilotApi.cleanPayCode(text)
            require(code.startsWith(PREFIX)) { "That is not a shop connect code." }
            val raw = runCatching { Base64.getUrlDecoder().decode(code.removePrefix(PREFIX)) }.getOrNull()
                ?: throw IllegalArgumentException("The shop connect code is damaged. Scan it again.")
            val payload = runCatching { PilotApi.json.decodeFromString<ConnectPayload>(raw.decodeToString()) }.getOrNull()
                ?: throw IllegalArgumentException("The shop connect code is damaged. Scan it again.")
            require(payload.v == 1) { "This shop uses a newer connect code. Update the app." }
            val name = payload.name.trim()
            require(name.length in 1..80) { "The shop connect code has no valid shop name." }
            require(PIN.matches(payload.pin)) { "The shop connect code has no valid security pin." }
            val urls = payload.urls.mapNotNull { runCatching { PilotApi.normalizeServerUrl(it) }.getOrNull() }.distinct()
            require(urls.isNotEmpty() && urls.size <= 12) { "The shop connect code has no valid address." }
            runCatching { java.util.UUID.fromString(payload.merchantId) }
                .getOrElse { throw IllegalArgumentException("The shop connect code is damaged. Scan it again.") }
            return ShopProfile(name, payload.merchantId, urls, payload.pin)
        }
    }
}

@Serializable
private data class ConnectPayload(
    val v: Int,
    val name: String,
    @SerialName("merchant_id") val merchantId: String,
    val urls: List<String>,
    val pin: String,
)

/** Trusts a server only if the chain it presents contains the pinned shop CA and validates against it. */
class PinnedTrustManager(private val pin: String) : X509TrustManager {
    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
        val anchor = chain.firstOrNull { it.basicConstraints >= 0 && spkiPin(it) == pin }
            ?: throw CertificateException("This server is not the shop this phone paired with.")
        val store = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null); setCertificateEntry("shop", anchor) }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(store) }
        // Full validation (signature, validity dates, key usage) with the pinned CA as the only trust anchor.
        tmf.trustManagers.filterIsInstance<X509TrustManager>().first().checkServerTrusted(chain, authType)
    }

    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) =
        throw CertificateException("Client certificates are not used.")

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    companion object {
        fun spkiPin(cert: X509Certificate): String =
            "sha256/" + Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded))

        fun socketFactory(pin: String): SSLSocketFactory =
            SSLContext.getInstance("TLS").apply { init(null, arrayOf(PinnedTrustManager(pin)), null) }.socketFactory
    }
}

/** Finds which of the shop's addresses answers right now, and checks it really is that shop. */
object ShopConnector {
    fun connect(shop: ShopProfile): Pair<PilotApi, ShopProfile> {
        var lastNetworkError: PilotApiException? = null
        for (url in shop.addressesToTry()) {
            val api = PilotApi(url, shop.pin, connectTimeoutMs = 4_000)
            try {
                val identity = api.shopIdentity()
                if (identity.merchantId != shop.merchantId) {
                    throw PilotApiException(0, "different_shop", "This address now belongs to a different shop. " +
                        "Scan ${shop.name}'s connect QR again.")
                }
                return api to shop.copy(name = identity.name, lastUrl = url)
            } catch (e: PilotApiException) {
                if (e.code != "network") throw e  // an untrusted server is never skipped over silently
                lastNetworkError = e
            }
        }
        throw PilotApiException(0, "network", "Cannot reach ${shop.name}. Connect to the shop's Wi-Fi, or scan the " +
            "shop's connect QR again if the shop PC changed address.", lastNetworkError)
    }

    fun hostOf(url: String): String = runCatching { URI(url).host }.getOrDefault(url)
}
