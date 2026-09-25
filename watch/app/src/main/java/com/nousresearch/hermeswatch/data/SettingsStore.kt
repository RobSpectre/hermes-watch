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
    val isComplete: Boolean get() = host.isNotBlank() && token.isNotBlank() && port in 1..65535

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
