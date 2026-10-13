package com.intraforge.pay.pilot

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

const val SIMULATED_NOTICE = "SIMULATED VALUE — NO CASH VALUE"
private val timeFormat = DateTimeFormatter.ofPattern("d MMM yyyy, HH:mm").withZone(ZoneId.systemDefault())

private fun formatTime(iso: String): String =
    runCatching { timeFormat.format(Instant.parse(iso.replace(Regex("\\+00:00$"), "Z"))) }.getOrDefault(iso)

@Composable
fun SimulatedBanner() {
    Text(
        SIMULATED_NOTICE,
        modifier = Modifier.fillMaxWidth().background(PilotColors.simTint).padding(vertical = 6.dp),
        color = PilotColors.sim, fontWeight = FontWeight.Bold, fontSize = 12.sp, textAlign = TextAlign.Center,
    )
}

@Composable
private fun Messages(state: UiState) {
    state.error?.let {
        Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(vertical = 8.dp))
    }
    state.info?.let {
        Text(it, color = PilotColors.ok, modifier = Modifier.padding(vertical = 8.dp))
    }
}

@Composable
private fun BigButton(text: String, enabled: Boolean = true, busy: Boolean = false, onClick: () -> Unit) {
    Button(onClick = onClick, enabled = enabled && !busy, modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)) {
        if (busy) CircularProgressIndicator(Modifier.height(20.dp), strokeWidth = 2.dp)
        else Text(text, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun PilotCard(content: @Composable () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) { Column(Modifier.padding(16.dp)) { content() } }
}

// --- shops -----------------------------------------------------------------------------------------------------------

@Composable
fun ShopsScreen(state: UiState, onScan: () -> Unit, onCode: (String) -> Unit, onChoose: (ShopProfile) -> Unit,
                onRemove: (ShopProfile) -> Unit) {
    var enterCode by remember { mutableStateOf(false) }
    var confirmRemove by remember { mutableStateOf<ShopProfile?>(null) }
    LazyColumn(Modifier.fillMaxWidth()) {
        item {
            Text("IF Credits Wallet", style = MaterialTheme.typography.headlineSmall,
                modifier = Modifier.padding(top = 16.dp).semantics { heading() })
            Text("Choose your shop, or add one by scanning the shop's connect QR (ask at the counter).",
                color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 4.dp, bottom = 8.dp))
            Messages(state)
            if (state.shops.isEmpty()) {
                Text("No shops yet.", color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(vertical = 8.dp))
            }
        }
        items(state.shops, key = { it.merchantId }) { shop ->
            PilotCard {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(shop.name, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                        Text("Paired · ${shop.lastUrl?.let(ShopConnector::hostOf) ?: "not connected yet"}",
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    TextButton(onClick = { confirmRemove = shop }) { Text("Remove") }
                    Button(onClick = { onChoose(shop) }) { Text("Open") }
                }
            }
        }
        item {
            Spacer(Modifier.height(12.dp))
            BigButton("Scan shop connect QR", busy = state.busy, onClick = onScan)
            TextButton(onClick = { enterCode = true }, modifier = Modifier.fillMaxWidth()) { Text("Paste a connect code instead") }
            Text("All balances are simulated IF Credits with no cash value. No real money is used.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(vertical = 16.dp))
        }
    }
    if (enterCode) {
        CodeDialog(title = "Shop connect code", hint = "IFPAYSHOP1.…", onDismiss = { enterCode = false }) {
            enterCode = false; onCode(it)
        }
    }
    confirmRemove?.let { shop ->
        AlertDialog(
            onDismissRequest = { confirmRemove = null },
            title = { Text("Remove ${shop.name}?") },
            text = { Text("Your credits stay with the shop. To use it again, scan its connect QR again.") },
            confirmButton = { TextButton(onClick = { confirmRemove = null; onRemove(shop) }) { Text("Remove") } },
            dismissButton = { TextButton(onClick = { confirmRemove = null }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun CodeDialog(title: String, hint: String, onDismiss: () -> Unit, onContinue: (String) -> Unit) {
    var code by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            OutlinedTextField(code, { code = it }, label = { Text(hint) }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii, autoCorrectEnabled = false,
                    capitalization = KeyboardCapitalization.None, imeAction = ImeAction.Done),
                modifier = Modifier.fillMaxWidth())
        },
        confirmButton = { TextButton(enabled = code.isNotBlank(), onClick = { onContinue(code) }) { Text("Continue") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

// --- sign in ---------------------------------------------------------------------------------------------------------

@Composable
fun SignInScreen(state: UiState, onSignIn: (String, String) -> Unit, onChangeShop: () -> Unit) {
    var username by rememberSaveable { mutableStateOf(state.username) }
    var password by remember { mutableStateOf("") }  // never saved
    Column(Modifier.verticalScroll(rememberScrollState()).padding(vertical = 16.dp)) {
        Text("Sign in to", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(state.shop?.name ?: "–", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold,
                modifier = Modifier.weight(1f).semantics { heading() })
            TextButton(onClick = onChangeShop) { Text("Change shop") }
        }
        Text("Use the test account the shop gave you.", color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 4.dp, bottom = 12.dp))
        OutlinedTextField(username, { username = it }, label = { Text("Username") }, singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii, autoCorrectEnabled = false,
                capitalization = KeyboardCapitalization.None, imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth())
        OutlinedTextField(password, { password = it }, label = { Text("Password") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
            modifier = Modifier.fillMaxWidth().padding(top = 4.dp))
        Messages(state)
        Spacer(Modifier.height(12.dp))
        BigButton("Sign in", enabled = username.isNotBlank() && password.isNotEmpty(), busy = state.busy) {
            onSignIn(username, password)
            password = ""
        }
        Text("All balances are simulated IF Credits with no cash value. No real money is used.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 16.dp))
    }
}

// --- wallet ----------------------------------------------------------------------------------------------------------

@Composable
fun WalletScreen(
    state: UiState, onScan: () -> Unit, onCode: (String) -> Unit, onRefresh: () -> Unit,
    onReceipt: (String) -> Unit, onSignOut: () -> Unit,
) {
    val wallet = state.wallet
    var enterCode by remember { mutableStateOf(false) }
    LazyColumn(Modifier.fillMaxWidth()) {
        item {
            Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(wallet?.customer ?: "", style = MaterialTheme.typography.titleMedium)
                    Text(state.shop?.name ?: "", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                TextButton(onClick = onRefresh, enabled = !state.busy) { Text("Refresh") }
                TextButton(onClick = onSignOut) { Text("Sign out") }
            }
            PilotCard {
                Text("Available", color = MaterialTheme.colorScheme.onSurfaceVariant)
                Row(verticalAlignment = Alignment.Bottom) {
                    Text(wallet?.balance ?: "–", fontSize = 40.sp, fontWeight = FontWeight.Bold)
                    Text("  IF Credits", modifier = Modifier.padding(bottom = 8.dp))
                }
                Text("Simulated value, no cash value", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(16.dp))
                BigButton("Scan QR", busy = state.busy, onClick = onScan)
                TextButton(onClick = { enterCode = true }, modifier = Modifier.fillMaxWidth()) {
                    Text("Type or paste a payment code instead")
                }
            }
            Messages(state)
            Text("Transaction history", style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.padding(top = 8.dp, bottom = 4.dp).semantics { heading() })
            if (wallet != null && wallet.history.isEmpty()) {
                Text("No transactions yet.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        items(wallet?.history ?: emptyList()) { h ->
            Row(Modifier.fillMaxWidth().padding(vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(h.description)
                    Text(formatTime(h.at), style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Text(h.amount, fontWeight = FontWeight.Bold,
                    color = if (h.amount.startsWith("-")) MaterialTheme.colorScheme.error else PilotColors.ok)
                h.receiptNumber?.let { number ->
                    TextButton(onClick = { onReceipt(number) }) { Text("Receipt") }
                }
            }
            HorizontalDivider()
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
    if (enterCode) {
        CodeDialog(title = "Payment code", hint = "IFPAY1.…", onDismiss = { enterCode = false }) {
            enterCode = false; onCode(it)
        }
    }
}

// --- confirm ---------------------------------------------------------------------------------------------------------

@Composable
fun ConfirmScreen(state: UiState, screen: Screen.Confirm, onPay: () -> Unit, onCancel: () -> Unit) {
    val c = screen.checkout
    val expiresAt = remember(c.expiresAt) { runCatching { Instant.parse(c.expiresAt.replace(Regex("\\+00:00$"), "Z")) }.getOrNull() }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(c.id) { while (true) { delay(1000); now = System.currentTimeMillis() } }
    val secondsLeft = expiresAt?.let { ((it.toEpochMilli() - now) / 1000).coerceAtLeast(0) }
    val expired = c.status == "expired" || secondsLeft == 0L
    val payable = c.status == "pending" && c.merchant.verified && !expired

    Column(Modifier.verticalScroll(rememberScrollState()).padding(vertical = 16.dp)) {
        Text("Confirm purchase", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
        PilotCard {
            Text("Paying", color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(c.merchant.name, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text(
                if (c.merchant.verified) "✓ Verified by IntraForge" else "✗ Not verified — do not pay",
                color = if (c.merchant.verified) PilotColors.ok else MaterialTheme.colorScheme.error,
                fontWeight = FontWeight.SemiBold,
            )
            Text("${c.amount} IF Credits", fontSize = 40.sp, fontWeight = FontWeight.ExtraBold,
                textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(vertical = 16.dp))
            SimulatedBanner()
            Text(
                when {
                    c.status == "paid" -> "This QR code has already been paid."
                    c.status == "cancelled" -> "The shop cancelled this payment request."
                    expired -> "This QR code has expired. Ask the shop for a new one."
                    secondsLeft != null -> "Expires in ${secondsLeft / 60}:${"%02d".format(secondsLeft % 60)}"
                    else -> ""
                },
                color = if (payable) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
        Messages(state)
        Spacer(Modifier.height(8.dp))
        BigButton("Confirm and pay ${c.amount}", enabled = payable, busy = state.busy, onClick = onPay)
        OutlinedButton(onClick = onCancel, modifier = Modifier.fillMaxWidth().padding(top = 8.dp).heightIn(min = 48.dp)) {
            Text("Don't pay")
        }
    }
}

// --- receipt ---------------------------------------------------------------------------------------------------------

@Composable
fun ReceiptScreen(state: UiState, receipt: Receipt, onDone: () -> Unit) {
    val b = receipt.body
    Column(Modifier.verticalScroll(rememberScrollState()).padding(vertical = 16.dp)) {
        Messages(state)
        Text("Receipt ${receipt.receiptNumber}", style = MaterialTheme.typography.headlineSmall,
            modifier = Modifier.semantics { heading() })
        PilotCard {
            Text(b.notice, color = PilotColors.sim, fontWeight = FontWeight.Bold, textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth().border(1.dp, PilotColors.simBorder, RoundedCornerShape(6.dp)).padding(6.dp))
            Spacer(Modifier.height(8.dp))
            Field("Shop", b.merchant.name)
            Field("Customer", b.customer.name)
            Field("Amount", "${b.amount} ${b.unit}")
            Field("Issued", formatTime(b.issuedAt))
            Field("Journal ref", b.journalReference, mono = true)
            Spacer(Modifier.height(8.dp))
            b.journalEntries.forEach { e ->
                Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                    Text(if (e.side == "debit") "Dr" else "Cr", modifier = Modifier.padding(end = 8.dp))
                    Text(e.account, fontFamily = FontFamily.Monospace, fontSize = 12.sp, modifier = Modifier.weight(1f))
                    Text(e.amount)
                }
            }
            Spacer(Modifier.height(8.dp))
            Text("Receipt fingerprint (SHA-256) — the shop sees the same:", style = MaterialTheme.typography.bodySmall)
            Text(receipt.sha256, fontFamily = FontFamily.Monospace, fontSize = 11.sp)
        }
        BigButton("Done", onClick = onDone)
    }
}

@Composable
private fun Field(label: String, value: String, mono: Boolean = false) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(0.35f))
        Text(value, fontFamily = if (mono) FontFamily.Monospace else null, fontSize = if (mono) 12.sp else 14.sp,
            modifier = Modifier.weight(0.65f))
    }
}

object PilotColors {
    val sim = Color(0xFFFCD34D)
    val simTint = Color(0xFF2A2210)
    val simBorder = Color(0xFFB45309)
    val ok = Color(0xFF4ADE80)
}
