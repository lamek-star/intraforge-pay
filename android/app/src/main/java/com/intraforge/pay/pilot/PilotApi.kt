package com.intraforge.pay.pilot

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.IOException
import java.net.CookieManager
import java.net.CookiePolicy
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLSocketFactory

/**
 * The shop server's customer API (the same endpoints the web wallet uses).
 *
 * Pure Kotlin/JVM, no Android classes, so it is tested against the real server on the JVM (app/src/test).
 * HTTPS only, and only to the shop this phone paired with: [pin] is that shop PC's certificate authority, and [ca]
 * the authority itself when the connect code carried it (see [PinnedTrustManager]). The session cookie lives in
 * memory only: closing the app signs the customer out. Every state change carries the CSRF token the server issued
 * at sign-in.
 */
class PilotApi(serverUrl: String, pin: String, ca: String? = null, private val connectTimeoutMs: Int = 10_000) {

    val baseUrl: String = normalizeServerUrl(serverUrl)
    private val tls: SSLSocketFactory = PinnedTrustManager.socketFactory(pin, ca)
    private val cookies = CookieManager(null, CookiePolicy.ACCEPT_ORIGINAL_SERVER)
    internal var csrfToken: String? = null  // internal: the JVM tests sign in as the shop to create QR codes

    fun login(username: String, password: String): User {
        val body = buildJsonObject { put("username", username.trim()); put("password", password) }.toString()
        val response = json.decodeFromString<LoginResponse>(request("POST", "/api/auth/login", body))
        csrfToken = response.csrfToken
        if (response.user.role != "customer") {
            runCatching { logout() }
            throw PilotApiException(403, "not_a_customer", "This app is for customers. Shop staff use the shop page.")
        }
        return response.user
    }

    fun logout() {
        try {
            request("POST", "/api/auth/logout", "{}")
        } finally {
            csrfToken = null
            cookies.cookieStore.removeAll()
        }
    }

    /** Public: which shop this server is (checked against the paired shop before signing in). */
    fun shopIdentity(): ShopIdentity = json.decodeFromString(request("GET", "/api/shop-identity"))

    fun wallet(): Wallet = json.decodeFromString(request("GET", "/api/wallet"))

    fun resolve(code: String): Checkout {
        val body = buildJsonObject { put("code", cleanPayCode(code)) }.toString()
        return json.decodeFromString(request("POST", "/api/wallet/checkout/resolve", body))
    }

    /** Explicit confirmation of exactly the amount the customer was shown. */
    fun pay(code: String, expectedAmountMinor: Long): PayResult {
        val body = buildJsonObject {
            put("code", cleanPayCode(code)); put("expected_amount_minor", expectedAmountMinor); put("confirm", true)
        }.toString()
        return json.decodeFromString(request("POST", "/api/wallet/checkout/pay", body))
    }

    fun receipt(number: String): Receipt {
        require(RECEIPT_NUMBER.matches(number)) { "Not a receipt number" }
        return json.decodeFromString(request("GET", "/api/receipts/$number"))
    }

    /** One HTTPS request; returns the body or throws [PilotApiException]. Public for tests that act as the shop. */
    fun request(method: String, path: String, body: String? = null): String {
        val uri = URI(baseUrl + path)
        val connection = (URL(uri.toString()).openConnection() as HttpsURLConnection).apply {
            sslSocketFactory = tls
            requestMethod = method
            instanceFollowRedirects = false
            connectTimeout = connectTimeoutMs
            readTimeout = 20_000
            useCaches = false
            setRequestProperty("Accept", "application/json")
            cookies.get(uri, emptyMap())["Cookie"]?.takeIf { it.isNotEmpty() }?.let {
                setRequestProperty("Cookie", it.joinToString("; "))
            }
            if (method != "GET") csrfToken?.let { setRequestProperty("X-CSRF-Token", it) }
        }
        try {
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            cookies.put(uri, connection.headerFields.filterKeys { it != null })
            val text = (if (status >= 400) connection.errorStream else connection.inputStream)
                ?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            if (status >= 400) throw errorFrom(status, text)
            return text
        } catch (e: javax.net.ssl.SSLException) {
            throw PilotApiException(0, "untrusted_server",
                "This server is not the shop this phone paired with. Ask the shop for its connect QR and scan it again.", e)
        } catch (e: IOException) {
            throw PilotApiException(0, "network", "Cannot reach the shop server (${e.javaClass.simpleName}).", e)
        } finally {
            connection.disconnect()
        }
    }

    private fun errorFrom(status: Int, text: String): PilotApiException {
        val parsed = runCatching { json.decodeFromString<ApiError>(text) }.getOrNull()
        if (status == 401) csrfToken = null  // the server-side session is gone
        return PilotApiException(status, parsed?.code ?: "http_$status",
            parsed?.message ?: "The shop server answered HTTP $status.")
    }

    companion object {
        const val PAY_CODE_PREFIX = "IFPAY1."
        private val RECEIPT_NUMBER = Regex("^IFP-\\d{8}-\\d{6}$")
        val json = Json { ignoreUnknownKeys = true }

        /** Inside IntraForge, Pay lives under an application path on IntraForge's address
         * (https://192.168.1.10:8443/apps/pay-1); a stand-alone Pay server has none. Only that path shape is accepted. */
        private val APP_PATH = Regex("^(/apps/[a-z0-9][a-z0-9-]{0,62})?$")

        fun normalizeServerUrl(input: String): String {
            val trimmed = input.trim()
            val withScheme = (if ("://" in trimmed) trimmed else "https://$trimmed").trimEnd('/')
            val uri = runCatching { URI(withScheme) }.getOrNull()
            require(uri != null && uri.scheme.equals("https", ignoreCase = true) && !uri.host.isNullOrBlank()) {
                "The shop server address must be https://<address>:<port>"
            }
            require(uri.rawQuery == null && uri.rawFragment == null && uri.rawUserInfo == null &&
                APP_PATH.matches(uri.rawPath.orEmpty())) {
                "Enter the shop's address, e.g. https://192.168.1.20:8443/apps/pay-1"
            }
            return withScheme
        }

        /** Payment codes never contain whitespace; keyboards and copy/paste sometimes add it. */
        fun cleanPayCode(text: String) = text.filterNot { it.isWhitespace() }

        fun looksLikePayCode(text: String) = cleanPayCode(text).startsWith(PAY_CODE_PREFIX)
    }
}

class PilotApiException(val status: Int, val code: String, message: String, cause: Throwable? = null) :
    Exception(message, cause)

@Serializable
data class ApiError(val code: String? = null, val message: String? = null)

@Serializable
data class ShopIdentity(val name: String, @SerialName("merchant_id") val merchantId: String)

@Serializable
data class User(
    val id: String,
    val username: String,
    @SerialName("display_name") val displayName: String,
    val role: String,
    val active: Boolean,
)

@Serializable
data class LoginResponse(val user: User, @SerialName("csrf_token") val csrfToken: String, val home: String)

@Serializable
data class Wallet(
    val notice: String,
    val customer: String,
    val balance: String,
    @SerialName("balance_minor") val balanceMinor: Long,
    val history: List<HistoryItem>,
)

@Serializable
data class HistoryItem(
    val kind: String,
    val description: String,
    val amount: String,
    val at: String,
    @SerialName("receipt_number") val receiptNumber: String? = null,
)

@Serializable
data class Merchant(val id: String, val name: String, val verified: Boolean)

@Serializable
data class Checkout(
    val id: String,
    val merchant: Merchant,
    val amount: String,
    @SerialName("amount_minor") val amountMinor: Long,
    val status: String,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("receipt_number") val receiptNumber: String? = null,
    val notice: String = "",
)

@Serializable
data class PayResult(val status: String, val receipt: Receipt, val balance: String)

@Serializable
data class Receipt(
    @SerialName("receipt_number") val receiptNumber: String,
    val body: ReceiptBody,
    val sha256: String,
    val uploads: List<ReceiptUpload> = emptyList(),
)

@Serializable
data class ReceiptBody(
    val notice: String,
    @SerialName("issued_at") val issuedAt: String,
    val merchant: Party,
    val customer: Party,
    val amount: String,
    val unit: String,
    @SerialName("journal_reference") val journalReference: String,
    @SerialName("journal_transaction_id") val journalTransactionId: String,
    @SerialName("journal_entries") val journalEntries: List<JournalEntry>,
)

@Serializable
data class Party(val id: String, val name: String)

@Serializable
data class JournalEntry(val account: String, val side: String, val amount: String)

@Serializable
data class ReceiptUpload(
    val kind: String,
    val filename: String,
    @SerialName("intraforge_file_id") val intraforgeFileId: String,
)
