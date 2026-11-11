package com.intraforge.pay.pilot

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.BeforeClass
import org.junit.Test
import java.io.File
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.Base64

/** Offline checks, plus live checks of the app's own client code against the running HTTPS shop server. */
class PilotApiTest {

    // --- offline ------------------------------------------------------------------------------------------------------

    @Test fun serverAddressMustBeHttps() {
        assertEquals("https://192.168.1.20:8643", PilotApi.normalizeServerUrl(" 192.168.1.20:8643/ "))
        assertEquals("https://10.0.2.2:8643", PilotApi.normalizeServerUrl("https://10.0.2.2:8643"))
        // IntraForge Pay: IntraForge's address plus the application's path.
        assertEquals("https://192.168.1.20:8443/apps/pay-1", PilotApi.normalizeServerUrl("192.168.1.20:8443/apps/pay-1/"))
        for (bad in listOf("http://192.168.1.20:8643", "ftp://x", "https://", "https://x:8643/api", "https://x?a=1",
                "https://x:8443/apps/pay-1/api", "https://x:8443/apps/Pay-1", "https://x:8443/apps/../admin",
                "https://x:8443/apps/pay-1#a", "https://u:p@x:8443/apps/pay-1")) {
            try { PilotApi.normalizeServerUrl(bad); fail("accepted $bad") } catch (_: IllegalArgumentException) { }
        }
    }

    @Test fun onlyPayCodesAreResolved() {
        assertTrue(PilotApi.looksLikePayCode("IFPAY1.0123.abc"))
        assertFalse(PilotApi.looksLikePayCode("https://evil.example/pay"))
        assertFalse(PilotApi.looksLikePayCode(""))
        assertEquals("IFPAY1.ab.cd", PilotApi.cleanPayCode(" IFPAY1. ab\n.cd\r\n"))
        assertTrue(PilotApi.looksLikePayCode("\n IFPAY1.x"))
        assertFalse(PilotApi.looksLikePayCode("IFPAYSHOP1.abc"))
    }

    @Test fun parsesServerJsonIgnoringUnknownFields() {
        val checkout = PilotApi.json.decodeFromString<Checkout>(
            """{"id":"x","merchant":{"id":"m","name":"Pilot Shop","verified":true},"amount":"25.00",
               "amount_minor":2500,"status":"pending","created_at":"t","expires_at":"2026-10-13T13:01:36+00:00",
               "receipt_number":null,"notice":"SIMULATED VALUE — NO CASH VALUE","future_field":1}""")
        assertEquals(2500L, checkout.amountMinor)
        assertTrue(checkout.merchant.verified)
    }

    private fun connectCode(json: String) =
        ShopProfile.PREFIX + Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray())

    private val goodPin = "sha256/" + "A".repeat(43) + "="
    private val merchant = "2e87b27c-0000-4000-8000-000000000001"

    @Test fun parsesAValidConnectCode() {
        val shop = ShopProfile.fromConnectCode(connectCode(
            """{"v":1,"name":"Pilot Shop","merchant_id":"$merchant","urls":["https://192.168.1.20:8643","https://10.0.2.2:8643"],"pin":"$goodPin"}"""))
        assertEquals("Pilot Shop", shop.name)
        assertEquals(listOf("https://192.168.1.20:8643", "https://10.0.2.2:8643"), shop.urls)
        assertEquals(shop.urls, shop.addressesToTry())
        val used = shop.copy(lastUrl = "https://10.0.2.2:8643")
        assertEquals(listOf("https://10.0.2.2:8643", "https://192.168.1.20:8643"), used.addressesToTry())
    }

    @Test fun refusesBadConnectCodes() {
        val base = """"merchant_id":"$merchant","urls":["https://192.168.1.20:8643"]"""
        val bad = listOf(
            "IFPAY1.not-a-shop-code",
            ShopProfile.PREFIX + "%%%",
            connectCode("""{"v":3,"name":"X",$base,"pin":"$goodPin"}"""),              // future format
            connectCode("""{"v":2,"name":"X",$base,"pin":"$goodPin"}"""),              // v2 without its certificate
            connectCode("""{"v":1,"name":"",$base,"pin":"$goodPin"}"""),               // no name
            connectCode("""{"v":1,"name":"X",$base,"pin":"md5/abc"}"""),               // bad pin
            connectCode("""{"v":1,"name":"X","merchant_id":"$merchant","urls":["http://192.168.1.20:8643"],"pin":"$goodPin"}"""),
            connectCode("""{"v":1,"name":"X","merchant_id":"nope","urls":["https://a:1"],"pin":"$goodPin"}"""),
        )
        for (code in bad) {
            try { ShopProfile.fromConnectCode(code); fail("accepted $code") } catch (_: IllegalArgumentException) { }
        }
    }

    // --- live (skipped unless the HTTPS shop server is running) -------------------------------------------------------

    companion object {
        private val server = System.getProperty("pilot.server", "https://127.0.0.1:8643")
        private val root = File(System.getProperty("pilot.root", "../.."))
        private lateinit var pin: String
        private lateinit var accounts: JsonObject
        private var live = false

        @BeforeClass @JvmStatic fun connect() {
            val ca = File(root, "secrets/tls/ca.crt")
            val acc = File(root, "secrets/test-accounts.json")
            if (!ca.exists() || !acc.exists()) { println("live tests skipped: no $ca or $acc"); return }
            val cert = ca.inputStream().use { CertificateFactory.getInstance("X.509").generateCertificate(it) }
            pin = PinnedTrustManager.spkiPin(cert as X509Certificate)
            accounts = Json.parseToJsonElement(acc.readText()).jsonObject
            live = runCatching { PilotApi(server, pin).request("GET", "/api/shop-identity") }
                .onFailure { println("live tests skipped: $server not reachable: $it / ${it.cause}") }.isSuccess
        }

        private fun password(user: String) = accounts[user]!!.jsonObject["password"]!!.jsonPrimitive.content
    }

    private fun customer(user: String = "customer5") = PilotApi(server, pin).also { it.login(user, password(user)) }

    /** The test acting as the shop: signs in as the cashier (to create QR codes and read the connect code). */
    private fun shopSession(): PilotApi {
        val shop = PilotApi(server, pin)
        val login = Json.parseToJsonElement(shop.request("POST", "/api/auth/login",
            """{"username":"cashier","password":"${password("cashier")}"}""")).jsonObject
        shop.csrfToken = login["csrf_token"]!!.jsonPrimitive.content
        return shop
    }

    private fun shopQr(amount: String): Pair<PilotApi, JsonObject> {
        val shop = shopSession()
        val qr = Json.parseToJsonElement(shop.request("POST", "/api/shop/payment-requests", """{"amount":"$amount"}""")).jsonObject
        return shop to qr
    }

    private fun expectError(code: String, block: () -> Unit) {
        try { block(); fail("expected $code") } catch (e: PilotApiException) { assertEquals(code, e.code) }
    }

    @Test fun livePairingWithTheShopsRealConnectCode() {
        assumeTrue("HTTPS shop server not running", live)
        val code = Json.parseToJsonElement(shopSession().request("GET", "/api/shop/pairing")).jsonObject["code"]!!
            .jsonPrimitive.content
        val shop = ShopProfile.fromConnectCode(code)
        assertEquals("Pilot Shop", shop.name)
        assertEquals(pin, shop.pin)
        // A dead address first: the connector must move on to one that answers.
        val reachable = shop.copy(urls = listOf("https://127.0.0.1:1") + shop.urls + server)
        val (api, connected) = ShopConnector.connect(reachable)
        assertEquals("Pilot Shop", connected.name)
        assertTrue(connected.lastUrl != "https://127.0.0.1:1")
        api.login("customer5", password("customer5"))
        assertTrue(api.wallet().balanceMinor >= 0)
    }

    @Test fun liveConnectorRefusesADifferentShopAndAWrongPin() {
        assumeTrue("HTTPS shop server not running", live)
        val other = ShopProfile("Other Shop", "00000000-0000-4000-8000-000000000000", listOf(server), pin)
        expectError("different_shop") { ShopConnector.connect(other) }
        val wrongPin = ShopProfile("Pilot Shop", "x", listOf(server), "sha256/" + "B".repeat(43) + "=")
        expectError("untrusted_server") { ShopConnector.connect(wrongPin) }
    }

    @Test fun livePayFlowGivesBothSidesTheSameReceiptAndNeverChargesTwice() {
        assumeTrue("HTTPS shop server not running", live)
        val api = customer()
        val before = api.wallet().balanceMinor
        val (shop, qr) = shopQr("0.01")
        val code = qr["code"]!!.jsonPrimitive.content

        val seen = api.resolve(code)
        assertEquals("Pilot Shop", seen.merchant.name)
        assertTrue(seen.merchant.verified)
        assertEquals(1L, seen.amountMinor)
        assertEquals("pending", seen.status)

        val paid = api.pay(code, seen.amountMinor)
        assertEquals("paid", paid.status)
        assertEquals(before - 1, api.wallet().balanceMinor)
        assertEquals(SIMULATED_NOTICE, paid.receipt.body.notice)

        val shopCopy = PilotApi.json.decodeFromString<Receipt>(shop.request("GET", "/api/receipts/${paid.receipt.receiptNumber}"))
        assertEquals(paid.receipt.sha256, shopCopy.sha256)
        assertEquals(paid.receipt.sha256, api.receipt(paid.receipt.receiptNumber).sha256)

        assertEquals("already_paid", api.pay(code, seen.amountMinor).status)
        assertEquals(before - 1, api.wallet().balanceMinor)
    }

    @Test fun liveInsufficientBalanceAndAmountMismatchAreRefused() {
        assumeTrue("HTTPS shop server not running", live)
        val api = customer()
        val before = api.wallet().balanceMinor
        val (_, big) = shopQr("1000.00")
        val code = big["code"]!!.jsonPrimitive.content
        assertTrue(before < big["amount_minor"]!!.jsonPrimitive.long)
        expectError("insufficient_balance") { api.pay(code, 100_000) }
        val (_, small) = shopQr("0.02")
        expectError("amount_mismatch") { api.pay(small["code"]!!.jsonPrimitive.content, 1) }
        assertEquals(before, api.wallet().balanceMinor)
    }

    @Test fun liveTamperedCodeIsRefused() {
        assumeTrue("HTTPS shop server not running", live)
        val api = customer()
        val (_, qr) = shopQr("0.03")
        val code = qr["code"]!!.jsonPrimitive.content
        val tampered = code.dropLast(1) + (if (code.last() == 'A') 'B' else 'A')
        expectError("unknown_code") { api.resolve(tampered) }
    }

    @Test fun liveShopStaffAndWrongPasswordsCannotUseTheWallet() {
        assumeTrue("HTTPS shop server not running", live)
        expectError("not_a_customer") { PilotApi(server, pin).login("cashier", password("cashier")) }
        expectError("not_a_customer") { PilotApi(server, pin).login("admin", password("admin")) }
        expectError("bad_credentials") { PilotApi(server, pin).login("customer4", "definitely-wrong") }
    }

    @Test fun liveSignOutEndsTheSession() {
        assumeTrue("HTTPS shop server not running", live)
        val api = customer("customer4")
        api.wallet()
        api.logout()
        expectError("not_signed_in") { api.wallet() }
    }
}
