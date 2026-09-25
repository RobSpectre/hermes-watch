package com.nousresearch.hermeswatch.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.TextField
import androidx.wear.compose.material.PositionIndicator
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText
import com.nousresearch.hermeswatch.data.BridgeSettings

/**
 * Pairing: host, port, token. Three fields is already a lot for a watch, so the
 * layout is one column, big targets, and the port pre-filled with the bridge's
 * documented default.
 */
@Composable
fun PairingScreen(
    initial: BridgeSettings,
    onSave: (BridgeSettings) -> Unit,
) {
    var host by rememberSaveable { mutableStateOf(initial.host) }
    var port by rememberSaveable { mutableStateOf(initial.port.toString()) }
    var token by rememberSaveable { mutableStateOf(initial.token) }
    val listState = rememberScalingLazyListState()
    val valid = host.isNotBlank() && token.isNotBlank() && (port.toIntOrNull() ?: 0) in 1..65535

    Scaffold(
        timeText = { TimeText() },
        positionIndicator = { PositionIndicator(scalingLazyListState = listState) },
    ) {
        ScalingLazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize(),
            verticalArrangement = Arrangement.spacedBy(6.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            item {
                Text(
                    text = "Pair with bridge",
                    style = MaterialTheme.typography.title3,
                    textAlign = TextAlign.Center,
                )
            }
            item {
                TextField(
                    value = host,
                    onValueChange = { host = it },
                    label = { Text("Bridge host") },
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            item {
                TextField(
                    value = port,
                    onValueChange = { port = it.filter { ch -> ch.isDigit() } },
                    label = { Text("Port") },
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Number,
                        imeAction = ImeAction.Next,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            item {
                TextField(
                    value = token,
                    onValueChange = { token = it.trim() },
                    label = { Text("Pairing token") },
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            item {
                Text(
                    text = "On the Hermes machine: hermes-watch-bridge token --show",
                    style = MaterialTheme.typography.caption1,
                    textAlign = TextAlign.Center,
                )
            }
            item {
                Button(
                    onClick = {
                        onSave(BridgeSettings(host = host, port = port.toIntOrNull() ?: 8787, token = token))
                    },
                    enabled = valid,
                ) {
                    Text("Connect")
                }
            }
        }
    }
}
