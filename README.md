# Telia F2 Router — Home Assistant Integration

<img src="icon.png" width="96" align="right" alt="Telia F2 Router integration icon">

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [Home Assistant](https://www.home-assistant.io/) custom integration for the **Telia F2** router (a Kaon AR6445G, sold by Telia across Norway/Sweden/the Baltics), reverse-engineered from the router's own local web UI — there is no official or documented API for this device.

It talks directly to the router over your LAN (`ws://<router-ip>/ws`) — nothing goes through Telia's or Kaon's servers.

## Why this exists

The Telia F2 has no official Home Assistant integration and no public API documentation. This integration was built by capturing and reverse-engineering the router's own local admin web UI (a Vue.js single-page app) — logging in, encrypting the password, and polling status the same way the browser does.

## Features

- **Sensors**: CPU load, free memory, uptime, WAN IP address, connected-device count, ethernet port status
- **Binary sensors**: WAN connectivity, mesh extender connectivity (with backhaul signal strength as attributes)
- **Device list**: one sensor with a full attribute list of every connected client (wired + wireless), which node/radio each is on, signal strength, and link rate — instead of one entity per client
- **Button**: restart the router

Not implemented (yet): toggling Wi-Fi bands on/off. The router's own UI makes this awkward (you have to disable band steering, then toggle each band individually), and we haven't captured/reverse-engineered that flow. PRs welcome.

## Installation

### HACS (recommended)

1. HACS → the three-dot menu (top right) → **Custom repositories**
2. Add this repository URL, category **Integration**
3. Search for "Telia F2 Router" in HACS and install it
4. Restart Home Assistant

### Manual

1. Copy `custom_components/telia_f2` into your Home Assistant `custom_components` folder
2. Restart Home Assistant

## Configuration

Settings → Devices & Services → Add Integration → search for **Telia F2 Router**.

You'll need:
- **Host**: the router's local IP (default `192.168.1.1`)
- **Username**: default `admin`
- **Password**: your router's admin password

The integration logs in for real during setup to verify the credentials before creating the entry.

## Entities

| Entity | Type | Notes |
|---|---|---|
| `sensor.<name>_cpu_load` | Sensor | Percent |
| `sensor.<name>_free_memory` | Sensor | kB |
| `sensor.<name>_uptime` | Sensor | Router's own uptime string |
| `sensor.<name>_wan_ip_address` | Sensor | |
| `sensor.<name>_connected_devices` | Sensor | State = count; `devices`, `nodes`, `wireless_count`, `wired_count` attributes |
| `sensor.<name>_ethernet_ports` | Sensor | State = `connected/total`; `ports` attribute with per-port status/speed |
| `binary_sensor.<name>_wan_connected` | Binary sensor | |
| `binary_sensor.<name>_extender_connected` | Binary sensor | Mesh extender backhaul link, with signal/band/rate as attributes |
| `button.<name>_restart` | Button | Reboots the router (device class `restart`, HA asks for confirmation) |

## Technical background

The router runs an nginx-fronted local web UI (Kaon/Vue.js firmware) that talks to the backend over a WebSocket at `/ws` using the `jsonrpc` subprotocol. Every call is wrapped as:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "api",
  "params": {
    "path": "/dashboard",
    "action": "get",
    "msg": {},
    "sid": "<session>",
    "csrftoken": "<token>"
  }
}
```

Logging in requires three things the browser does that aren't obvious from the WebSocket traffic alone:

1. **A real CSRF token**, fetched via `GET /csrf-token` — the token is in the `X-CSRF-Token` **response header**, not the response body (the body is just a placeholder string). Skipping this step still returns a "successful" login (`code: 0`) but the session silently has no real permissions — every subsequent call fails with `{"code": 6, "message": "Permission denied"}`.
2. **AES-256-CBC encryption of the password**, with a key and IV that are hardcoded in the router's own JavaScript bundle (found by hooking `CryptoJS.AES.encrypt` in the browser console during a real login — see `api.py` for the exact key/IV and encryption code).
3. Using the `sid`/`csrftoken` returned by the login response for every subsequent call, and re-authenticating before the ~300 second session timeout.

None of this is a secret extracted from Telia's or Kaon's servers — it's all client-side JavaScript that the router ships to every browser that connects to it. This integration doesn't bypass any authentication; it logs in with the credentials you provide, the same way the web UI does.

## Known limitations

- The router doesn't report which physical node (main router vs. mesh extender) a **wired** client is plugged into — only wireless clients carry that information in the API response. Wired clients are listed separately.
- We can only confirm the WAN port with certainty (matched by its distinctive 10 Gb/s link speed); the other three ports are exposed as generic LAN ports since the API only reports current negotiated speed, not each port's rated maximum (1G vs 10G capable).

## Disclaimer

Not affiliated with or endorsed by Telia or Kaon. Provided as-is; use at your own risk. This integration was built by observing the router's own local web UI running on the local network — no data leaves your LAN except to the router itself.

## Contributing

Issues and PRs welcome — especially for the Wi-Fi toggle functionality mentioned above.

## License

MIT — see [LICENSE](LICENSE).
