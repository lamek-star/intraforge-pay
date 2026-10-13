package com.intraforge.pay.pilot

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.builtins.ListSerializer

sealed interface Screen {
    data object Shops : Screen
    data object SignIn : Screen
    data object Wallet : Screen
    data class Confirm(val code: String, val checkout: Checkout) : Screen
    data class ReceiptView(val receipt: Receipt, val justPaid: Boolean) : Screen
}

data class UiState(
    val screen: Screen = Screen.Shops,
    val shops: List<ShopProfile> = emptyList(),
    val shop: ShopProfile? = null,
    val username: String = "",
    val wallet: Wallet? = null,
    val busy: Boolean = false,
    val error: String? = null,
    val info: String? = null,
)

/**
 * Stored on the phone: the paired shops (name, addresses, security pin -- all public, shown on the shop screen), which
 * one was used last, and the last username. Never the password or the session.
 */
class WalletViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = app.getSharedPreferences("ifpay", Context.MODE_PRIVATE)
    private val shopListSerializer = ListSerializer(ShopProfile.serializer())
    private val _state = MutableStateFlow(initialState())
    val state: StateFlow<UiState> = _state
    private var api: PilotApi? = null

    private fun initialState(): UiState {
        val shops = runCatching {
            PilotApi.json.decodeFromString(shopListSerializer, prefs.getString("shops", "[]")!!)
        }.getOrDefault(emptyList())
        val current = shops.firstOrNull { it.merchantId == prefs.getString("current_shop", null) }
        return UiState(
            screen = if (current != null) Screen.SignIn else Screen.Shops,
            shops = shops, shop = current, username = prefs.getString("username", null) ?: "",
        )
    }

    private fun saveShops(shops: List<ShopProfile>, current: ShopProfile?) {
        prefs.edit()
            .putString("shops", PilotApi.json.encodeToString(shopListSerializer, shops))
            .putString("current_shop", current?.merchantId)
            .apply()
        _state.update { it.copy(shops = shops, shop = current) }
    }

    private fun remember(shop: ShopProfile) {
        val shops = _state.value.shops.filter { it.merchantId != shop.merchantId } + shop
        saveShops(shops.sortedBy { it.name.lowercase() }, shop)
    }

    private fun action(block: suspend () -> Unit) {
        if (_state.value.busy) return  // one action at a time: no double-tap double submissions
        _state.update { it.copy(busy = true, error = null, info = null) }
        viewModelScope.launch {
            try {
                block()
            } catch (e: PilotApiException) {
                if (e.status == 401 && e.code != "bad_credentials" && e.code != "too_many_attempts") {
                    api = null
                    _state.update { it.copy(screen = Screen.SignIn, wallet = null) }
                }
                _state.update { it.copy(error = e.message) }
            } catch (e: IllegalArgumentException) {
                _state.update { it.copy(error = e.message) }
            } finally {
                _state.update { it.copy(busy = false) }
            }
        }
    }

    private suspend fun <T> io(block: () -> T): T = withContext(Dispatchers.IO) { block() }

    // --- shops -------------------------------------------------------------------------------------------------------

    /** A scanned or pasted connect code: check that shop answers and really is that shop, then remember it. */
    fun addShop(code: String) = action {
        val profile = ShopProfile.fromConnectCode(code)
        val (_, connected) = io { ShopConnector.connect(profile) }
        remember(connected)
        _state.update { it.copy(screen = Screen.SignIn, info = "${connected.name} added.") }
    }

    fun chooseShop(shop: ShopProfile) {
        saveShops(_state.value.shops, shop)
        _state.update { it.copy(screen = Screen.SignIn, error = null, info = null) }
    }

    fun removeShop(shop: ShopProfile) {
        val shops = _state.value.shops.filter { it.merchantId != shop.merchantId }
        saveShops(shops, _state.value.shop?.takeIf { it.merchantId != shop.merchantId })
    }

    fun showShops() {
        signOut()
        _state.update { it.copy(screen = Screen.Shops) }
    }

    // --- wallet ------------------------------------------------------------------------------------------------------

    fun signIn(username: String, password: String) = action {
        val shop = _state.value.shop ?: throw IllegalArgumentException("Choose a shop first.")
        val (client, connected) = io { ShopConnector.connect(shop) }
        io { client.login(username, password) }
        remember(connected)
        prefs.edit().putString("username", username.trim()).apply()
        api = client
        val wallet = io { client.wallet() }
        _state.update { it.copy(screen = Screen.Wallet, username = username.trim(), wallet = wallet) }
    }

    fun refresh() = action {
        val wallet = io { requireApi().wallet() }
        _state.update { it.copy(wallet = wallet) }
    }

    /** A scanned or typed code: ask the server who the shop is and how much, then show the confirmation screen. */
    fun resolve(code: String) = action {
        if (ShopProfile.looksLikeConnectCode(code)) {
            _state.update { it.copy(error = "That is a shop connect QR. To add a shop, sign out and open \"Shops\".") }
            return@action
        }
        if (!PilotApi.looksLikePayCode(code)) {
            _state.update { it.copy(error = "That is not an IntraForge Pay QR code.") }
            return@action
        }
        val checkout = io { requireApi().resolve(code) }
        _state.update { it.copy(screen = Screen.Confirm(PilotApi.cleanPayCode(code), checkout)) }
    }

    fun confirmAndPay(confirm: Screen.Confirm) = action {
        val result = io { requireApi().pay(confirm.code, confirm.checkout.amountMinor) }
        val wallet = io { requireApi().wallet() }
        _state.update {
            it.copy(
                screen = Screen.ReceiptView(result.receipt, justPaid = true),
                wallet = wallet,
                info = if (result.status == "paid") "Paid. New balance: ${result.balance} IF Credits."
                else "This code was already paid by you; here is the receipt. Nothing was charged again.",
            )
        }
    }

    fun openReceipt(number: String) = action {
        val receipt = io { requireApi().receipt(number) }
        _state.update { it.copy(screen = Screen.ReceiptView(receipt, justPaid = false)) }
    }

    fun backToWallet() {
        _state.update { it.copy(screen = Screen.Wallet, error = null) }
        refresh()
    }

    fun signOut() {
        val client = api
        api = null
        _state.update { it.copy(screen = Screen.SignIn, wallet = null, error = null, info = null) }
        if (client != null) viewModelScope.launch { runCatching { io { client.logout() } } }
    }

    private fun requireApi(): PilotApi =
        api ?: throw PilotApiException(401, "not_signed_in", "Please sign in again.")
}
