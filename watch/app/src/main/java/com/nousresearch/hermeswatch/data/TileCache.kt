package com.nousresearch.hermeswatch.data

import android.content.Context
import android.content.SharedPreferences

/**
 * The last snapshot, cached for the tile.
 *
 * Separate from [SettingsStore] on purpose. DataStore is the right home for the
 * user's pairing details (async, transactional, Flow-shaped), but the tile API
 * asks for a layout on a system-scheduled thread and cannot suspend, so the tile
 * needs a synchronous read. SharedPreferences with `commit()` is exactly that:
 * a synchronous, single-value cache. Writes happen once per stats frame from the
 * service, which is why the blocking commit is acceptable here and would not be
 * in a UI path.
 */
object TileCache {

    private const val FILE = "hermes-watch-tile"
    private const val KEY_SNAPSHOT = "snapshot_json"
    private const val KEY_UPDATED_AT = "updated_at"

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun write(context: Context, json: String, updatedAt: Long = System.currentTimeMillis()) {
        prefs(context).edit()
            .putString(KEY_SNAPSHOT, json)
            .putLong(KEY_UPDATED_AT, updatedAt)
            .commit()
    }

    fun readJson(context: Context): String? = prefs(context).getString(KEY_SNAPSHOT, null)

    fun updatedAt(context: Context): Long = prefs(context).getLong(KEY_UPDATED_AT, 0L)

    fun clear(context: Context) {
        prefs(context).edit().clear().commit()
    }
}
