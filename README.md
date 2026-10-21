# SolarEdge ONE for Home Assistant

Custom integration for SolarEdge systems using the new **SolarEdge ONE for Developers**
Monitoring API V2 (OAuth), with an optional **local Modbus TCP** connection for
real-time data and control of the battery and export limit.

> The old V1 Monitoring API (site API key) is retired on November 1, 2026. The
> built-in `solaredge` integration uses V1. This integration uses a separate domain
> (`solaredge_one`), so both can run side by side.

## What you get

| Source | Data | Control |
| --- | --- | --- |
| Cloud (V2, OAuth) | Current power, today's production / consumption / self-consumption / grid import & export / battery charge & discharge, lifetime production, open alerts. Optional per-device telemetry. | None. V2 is read-only. |
| Local Modbus TCP (optional) | Real-time inverter AC/DC power, voltage, current, frequency, temperature, status, lifetime energy; grid meter power and import/export totals; battery state of charge, health, power, energy, status. | Storage control mode, AC charge policy, default and remote storage commands, backup reserve, remote charge/discharge limits, command timeout, site export limit. Service `solaredge_one.set_storage_command` for timed charge/discharge. |

### Why control goes through Modbus

The V2 API (endpoints under `https://monitoringapi.solaredge.com/v2`) exposes only
`GET` operations: site, overview, power, energy, devices, alerts and telemetry. No
write or control endpoint was found in the developer portal or the documented API.
The only supported way to change battery or export settings is the inverter's local
Modbus TCP interface. If SolarEdge adds control endpoints to V2, they can be added
next to the Modbus controls.

## Setup

1. **Install.** In HACS, open the menu → *Custom repositories*, add
   `https://github.com/yuvaldvir81-git/SolarEdgeHaIntegration` as type
   *Integration*, install **SolarEdge ONE**, and restart Home Assistant. (Or copy
   `custom_components/solaredge_one` into `config/custom_components/`.) Requires
   Home Assistant 2026.10 or later.
2. **Create a developer application.** Sign in at <https://developer.solaredge.com>
   and create an application with **Site Access** and the `SITE_DATA` and
   `DEVICE_DATA` scopes. Set its redirect URL to

   ```
   <your Home Assistant URL>/api/solaredge_one/oauth/callback
   ```

   for example `http://homeassistant.local:8123/api/solaredge_one/oauth/callback`.
   This is the address your **browser** uses to reach Home Assistant; it does not
   need to be reachable from the internet. Note the Client ID and Secret.
3. **Add the integration.** Settings → Devices & services → Add integration →
   *SolarEdge ONE*. Enter the Client ID and Secret (the dialog shows the exact
   redirect URL to register), sign in to SolarEdge and approve your site. The site
   is picked up from the redirect; you're asked for the Site ID only if SolarEdge
   doesn't send it.
4. **Optional: Modbus TCP.** Enable *Modbus TCP* on the inverter in SetApp
   (Site Communication → Modbus TCP, default port 1502). Enter the inverter's IP
   address. Tick *Allow control* only if you want writable entities.

### Why not the usual My Home Assistant redirect?

SolarEdge Connect ignores the `state`, `redirect_uri` and `scope` parameters of
the authorize URL: it always redirects to the URL registered for the application
and only echoes `external_id`. Home Assistant's standard OAuth callback
(`/auth/external/callback`) rejects redirects without `state`, so this
integration has its own callback that carries the sign-in flow in `external_id`.

You can change polling intervals and Modbus settings later under the integration's
**Configure** button.

## API budget

The free tier allows **2,000 calls a month** and 10 a minute. By default the
integration polls every 60 minutes. Each poll costs 2 calls (overview, power), and
site info, lifetime energy, inventory and alerts refresh every 12 hours (4 calls).
That's about 1,680 calls a month. Per-device telemetry adds 3 calls per poll, which
needs a paid tier or an interval of about 2.5 hours or more. Modbus is local and free.

## Control safety

- Control entities are created only when **Allow control** is on, the inverter
  answers on the storage registers, and (for battery controls) a battery is detected.
- Remote charge/discharge commands take effect only when *Storage control mode* is
  *Remote control*. The `set_storage_command` service switches to remote control,
  sets the timeout, the limits and the command, in that order. When the timeout
  expires, the inverter falls back to the *Default storage command*.
- Writing the export limit needs export control already configured on the inverter
  (SetApp → Power Control → Export Control).
- SolarEdge inverters accept **one Modbus TCP client at a time**. Disable any other
  Modbus integration pointed at the same inverter.

Example automation: charge from the grid at 3 kW during cheap night hours.

```yaml
action: solaredge_one.set_storage_command
data:
  config_entry_id: <your entry id>
  mode: charge_from_pv_and_ac
  duration: 10800
  charge_limit: 3000
```

## Status and caveats

- **Not yet tested against live hardware or the live V2 API.** The tests run against
  documented response shapes and a simulated inverter register map.
- The authorize behaviour above was read from the SolarEdge Connect page itself.
  The token endpoint (`monitoringapi.solaredge.com/v2/oauth2/token`, JSON body)
  and V2 response shapes come from SolarEdge's V2 documentation as summarized by
  the community [`ha_int_solaredge`](https://github.com/thebigjc/ha_int_solaredge)
  project; the official reference is behind the developer login.
- The Modbus register map follows SolarEdge's SunSpec implementation and its
  proprietary storage/export control registers (`0xE000`–`0xE011`, batteries at
  `0xE100`/`0xE200`, float32 low word first). The meter power sign (positive =
  export) and the battery status codes should be confirmed on a real system.

## Development

Tests run against Home Assistant 2026.10 using
`pytest-homeassistant-custom-component`. With Docker:

```sh
docker run --rm -v "$PWD":/work -w /work python:3.14-slim \
  sh -c "pip install -q -r requirements_test.txt && pytest -q"
```

Layout:

- `api/`: Monitoring API V2 client and models (no Home Assistant imports)
- `modbus/`: register map and decoding (`registers.py`, pure) plus async client
- `coordinator.py`: cloud polling within the credit budget, and Modbus polling and writes
- `sensor.py`, `select.py`, `number.py`: entities
