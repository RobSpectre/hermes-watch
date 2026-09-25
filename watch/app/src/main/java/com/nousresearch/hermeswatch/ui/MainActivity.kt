package com.nousresearch.hermeswatch.ui

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.lifecycle.lifecycleScope
import com.nousresearch.hermeswatch.data.BridgeSettings
import com.nousresearch.hermeswatch.data.SettingsStore
import com.nousresearch.hermeswatch.service.WatchLinkService
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* best effort */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)

        setContent {
            HermesWatchTheme {
                val store = remember { SettingsStore(applicationContext) }
                val settings by store.settings.collectAsState(initial = null)
                val scope = rememberCoroutineScope()
                var editing by remember { mutableStateOf(false) }

                val paired = settings?.isComplete == true
                if (!paired || editing) {
                    PairingScreen(
                        initial = settings ?: BridgeSettings(),
                        onSave = { updated ->
                            scope.launch {
                                store.save(updated)
                                editing = false
                            }
                        },
                    )
                } else {
                    StatsScreen(
                        settings = settings!!,
                        onEditPairing = { editing = true },
                    )
                }
            }
        }

        // The link is owned by the service; ask it to run for as long as the app
        // is installed, not just while an activity is on screen.
        lifecycleScope.launch {
            startLinkService()
        }
    }

    private fun startLinkService() {
        val intent = android.content.Intent(this, WatchLinkService::class.java)
        androidx.core.content.ContextCompat.startForegroundService(this, intent)
    }
}
