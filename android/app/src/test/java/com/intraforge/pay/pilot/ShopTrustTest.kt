package com.intraforge.pay.pilot

import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test
import java.security.cert.CertificateException
import java.util.Base64

/**
 * IntraForge Pay inside IntraForge (kit 1.4.0+): the connect code (v2) carries the shop's CA, because IntraForge's
 * proxy sends only its server and intermediate certificates. Offline, with a fixed test chain of the same shape
 * (root -> intermediate -> server certificate for 127.0.0.1 / shop.test, valid until 2046).
 */
class ShopTrustTest {

    private val root =
        "MIIBdzCCARygAwIBAgIUexh5a5MkN/4eqReWg2Na9698g+MwCgYIKoZIzj0EAwIwGTEXMBUGA1UEAwwOVGVzdCBTaG9wIFJvb3QwHhcNMjYxMTA4MDY0MTM2WhcNNDYxMTAzMDY0MTM2WjAZMRcwFQYDVQQDDA5UZXN0IFNob3AgUm9vdDBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABDcFMQPUvC1+FAJKkIs1LTsLMMfq0GMXKrC6bWMoyKeHFfvmDvT7e/eeY95i3IImmhqlqJEX7IHePqA3kWW4MEyjQjBAMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgEGMB0GA1UdDgQWBBQqrWsLoj5twAq9BREyQ/fttPwKNjAKBggqhkjOPQQDAgNJADBGAiEAgEShkRIjyEQoDT5vA9czP3e5fEyelzR28LilkD8jExoCIQDq5XndintMIGcRioI3AdGeeSGjk4zbnOKqezlE6XtYkg=="
    private val lookAlike =
        "MIIBeTCCAR6gAwIBAgIUGE6kSluaCC/rBc8zTlTGNdFbwXwwCgYIKoZIzj0EAwIwGjEYMBYGA1UEAwwPTG9vay1hbGlrZSBSb290MB4XDTI2MTEwODA2NDEzNloXDTQ2MTEwMzA2NDEzNlowGjEYMBYGA1UEAwwPTG9vay1hbGlrZSBSb290MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjhL1vEMHVYHStdBJA/TB2jLHEOpoSmqy2QZwOjBeZAAlfqJKxdFbRN/cu4ctnAE6Gy9ufqi4t4vXVj8h6q7EY6NCMEAwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAQYwHQYDVR0OBBYEFMm7wdyRd5b9exWHI/5xX9VzvRAPMAoGCCqGSM49BAMCA0kAMEYCIQCdaW42Wec0z8s7c/fTHifcGaiCeupMIOUcf8hZV7pztAIhAI2b6C41Aoax/gKLWc119SViHB0+qqwJZ4Ome6iwr65U"
    private val intermediate = PinnedTrustManager.decodeCa(
        "MIIBoTCCAUigAwIBAgIUC3TP+OtG22jJfA4NPC8Xhpr/Y3QwCgYIKoZIzj0EAwIwGTEXMBUGA1UEAwwOVGVzdCBTaG9wIFJvb3QwHhcNMjYxMTA4MDY0MTM2WhcNNDYxMDI0MDY0MTM2WjAhMR8wHQYDVQQDDBZUZXN0IFNob3AgSW50ZXJtZWRpYXRlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE8tBv1C+otNs7YrRL0Bd9Vqn01mMccNm/TH/JgK13PJ9x7XjLIQ4fkEBWgb5e3yVNUb8XJgIywl7lpe7L7fFf1aNmMGQwEgYDVR0TAQH/BAgwBgEB/wIBADAOBgNVHQ8BAf8EBAMCAQYwHQYDVR0OBBYEFM/4Uu4uuRt55/x9UDNIceLDCX65MB8GA1UdIwQYMBaAFCqtawuiPm3ACr0FETJD9+20/Ao2MAoGCCqGSM49BAMCA0cAMEQCIDKVXVPJmW5soZ4tRQvLl3sGoyT6zjJV/5KaaFbq2Up5AiBj00PSgV/IT/84LbWB8YAvPrf8awjJ91NWQrYycNveGQ==")
    private val server = PinnedTrustManager.decodeCa(
        "MIIByjCCAXCgAwIBAgIURdfxKu//qG8antyjL/Z5qKiW3hwwCgYIKoZIzj0EAwIwITEfMB0GA1UEAwwWVGVzdCBTaG9wIEludGVybWVkaWF0ZTAeFw0yNjExMDgwNjQxMzZaFw00NjEwMTQwNjQxMzZaMBQxEjAQBgNVBAMMCXNob3AudGVzdDBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABNjF9hUZdcB7OSLxzG+/+0EArHg77wqXCd3QfY8GElbPB5497aS9biPko7R09S/2jPcJX68Hh+Nja3Gjp8OtE6SjgZIwgY8wDAYDVR0TAQH/BAIwADAOBgNVHQ8BAf8EBAMCB4AwEwYDVR0lBAwwCgYIKwYBBQUHAwEwGgYDVR0RBBMwEYcEfwAAAYIJc2hvcC50ZXN0MB0GA1UdDgQWBBSgRq3MpN8xH46fkyfY836lNqOwLzAfBgNVHSMEGDAWgBTP+FLuLrkbeef8fVAzSHHiwwl+uTAKBggqhkjOPQQDAgNIADBFAiEA0c074bDX9Q0AJF1K3eF7BMZbIlxtKw05HDKEa/qVPRYCIDjzF/eBgV4SltbNIxaVXakhjwLy+E5NMxePC9BZHOBD")
    private val rootPin = PinnedTrustManager.spkiPin(PinnedTrustManager.decodeCa(root))
    private val merchant = "2e87b27c-0000-4000-8000-000000000001"
    private val urls = """["https://192.168.1.20:8443/apps/pay-1","https://SHOP-PC:8443/apps/pay-1"]"""

    private fun connectCode(json: String) =
        ShopProfile.PREFIX + Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray())

    @Test fun pairsWithAnIntraForgeConnectCode() {
        val shop = ShopProfile.fromConnectCode(connectCode(
            """{"v":2,"name":"Test Shop","merchant_id":"$merchant","urls":$urls,"pin":"$rootPin","ca":"$root"}"""))
        assertEquals(listOf("https://192.168.1.20:8443/apps/pay-1", "https://SHOP-PC:8443/apps/pay-1"), shop.urls)
        assertEquals(root, shop.ca)
        assertEquals("https://192.168.1.20:8443/apps/pay-1", PilotApi(shop.urls[0], shop.pin, shop.ca).baseUrl)
    }

    @Test fun refusesACertificateThatIsNotTheCodesPinOrNotACa() {
        val serverDer = Base64.getEncoder().encodeToString(server.encoded)
        val bad = listOf(
            """{"v":2,"name":"X","merchant_id":"$merchant","urls":$urls,"pin":"$rootPin","ca":"$lookAlike"}""",
            """{"v":2,"name":"X","merchant_id":"$merchant","urls":$urls,"pin":"${PinnedTrustManager.spkiPin(server)}","ca":"$serverDer"}""",
            """{"v":2,"name":"X","merchant_id":"$merchant","urls":$urls,"pin":"$rootPin","ca":"not base64 at all"}""",
        )
        for (json in bad) {
            try { ShopProfile.fromConnectCode(connectCode(json)); fail("accepted $json") } catch (_: IllegalArgumentException) { }
        }
    }

    @Test fun trustsTheShopsChainEvenThoughTheRootIsNotSent() {
        // What IntraForge's proxy sends: the server certificate and the intermediate, no root.
        PinnedTrustManager(rootPin, PinnedTrustManager.decodeCa(root))
            .checkServerTrusted(arrayOf(server, intermediate), "ECDHE_ECDSA")
    }

    @Test fun refusesTheSameChainForAnotherShop() {
        val other = PinnedTrustManager.decodeCa(lookAlike)
        val otherPin = PinnedTrustManager.spkiPin(other)
        for (manager in listOf(
            PinnedTrustManager(otherPin, other),                             // paired with a different shop
            PinnedTrustManager(otherPin, PinnedTrustManager.decodeCa(root)), // a CA that is not the pinned one
            PinnedTrustManager(rootPin),                                     // old app: no CA, and the root is not sent
        )) {
            try { manager.checkServerTrusted(arrayOf(server, intermediate), "ECDHE_ECDSA"); fail("trusted") }
            catch (_: CertificateException) { }
        }
    }
}
