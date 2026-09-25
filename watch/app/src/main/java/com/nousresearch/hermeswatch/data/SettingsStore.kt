package com.nousresearch.hermeswatch.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

/** Pairing details, as entered on the watch. */
data class BridgeSettings(
    val host: String = "",
    val port: Int = DEFAULT_PORT,
    val token: String = "",
) {
    /**
     * Whether there is enough here to open a socket.
     *
     * The token is deliberately *not* required: the bridge no longer keeps a
     * shared secret. Access is granted by Hermes' own pairing store, so a watch
     * with the right host and port connects, says hello, and is told it is
     * unpaired until someone runs `hermes pairing approve pixel_watch <code>` on
     * the host. Requiring a token here would block that first hello.
     */
    val isComplete: Boolean get() = host.isNotBlank() && port in 1..65535

    companion object {
        const val DEFAULT_PORT = 8787
    }
}

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "hermes-watch")

class SettingsStore(private val context: Context) {

    private object Keys {
        val HOST = stringPreferencesKey("bridge_host")
        val PORT = intPreferencesKey("bridge_port")
        val TOKEN = stringPreferencesKey("bridge_token")
    }

    val settings: Flow<BridgeSettings> = context.dataStore.data.map { prefs ->
        BridgeSettings(
            host = prefs[Keys.HOST] ?: "",
            port = prefs[Keys.PORT] ?: BridgeSettings.DEFAULT_PORT,
            token = prefs[Keys.TOKEN] ?: "",
        )
    }

    suspend fun save(settings: BridgeSettings) {
        context.dataStore.edit { prefs ->
            prefs[Keys.HOST] = settings.host.trim()
            prefs[Keys.PORT] = settings.port
            prefs[Keys.TOKEN] = settings.token.trim()
        }
    }
}
