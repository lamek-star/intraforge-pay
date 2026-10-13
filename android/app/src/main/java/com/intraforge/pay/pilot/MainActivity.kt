package com.intraforge.pay.pilot

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions

class MainActivity : ComponentActivity() {
    private val viewModel: WalletViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Light status/navigation icons on the navy app background.
        enableEdgeToEdge(SystemBarStyle.dark(android.graphics.Color.TRANSPARENT), SystemBarStyle.dark(android.graphics.Color.TRANSPARENT))
        setContent { PilotTheme { WalletApp(viewModel) } }
    }
}

@Composable
fun WalletApp(viewModel: WalletViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    // One scanner for both kinds of QR: the shop's connect QR (Shops screen) and a payment QR (wallet).
    val scanner = rememberLauncherForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@rememberLauncherForActivityResult
        if (state.screen == Screen.Shops) viewModel.addShop(text) else viewModel.resolve(text)
    }
    val scan = { prompt: String ->
        scanner.launch(
            ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt(prompt)
                .setBeepEnabled(false)
                .setOrientationLocked(false)
        )
    }

    BackHandler(enabled = state.screen is Screen.Confirm || state.screen is Screen.ReceiptView) {
        viewModel.backToWallet()
    }

    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxSize().safeDrawingPadding()) {
            SimulatedBanner()
            Box(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
                when (val screen = state.screen) {
                    Screen.Shops -> ShopsScreen(state, onScan = { scan("Scan the shop's connect QR") },
                        onCode = viewModel::addShop, onChoose = viewModel::chooseShop, onRemove = viewModel::removeShop)
                    Screen.SignIn -> SignInScreen(state, viewModel::signIn, onChangeShop = viewModel::showShops)
                    Screen.Wallet -> WalletScreen(state, onScan = { scan("Scan the QR code shown by the shop") },
                        onCode = viewModel::resolve,
                        onRefresh = viewModel::refresh, onReceipt = viewModel::openReceipt, onSignOut = viewModel::signOut)
                    is Screen.Confirm -> ConfirmScreen(state, screen, onPay = { viewModel.confirmAndPay(screen) },
                        onCancel = viewModel::backToWallet)
                    is Screen.ReceiptView -> ReceiptScreen(state, screen.receipt, onDone = viewModel::backToWallet)
                }
            }
        }
    }
}
