# EV Load Balancer

Dynamic load balancing for EV chargers in Home Assistant. Measures current per
phase at the main fuse and adjusts the charger's output power in real time to
prevent the fuse from tripping. Supports phase switching (1↔3 phase), failsafe
modes and charger profiles. MVP: go-e Gemini flex.

[![HACS Badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/cryptotomte/ev-load-balancer/actions/workflows/validate.yaml/badge.svg)](https://github.com/cryptotomte/ev-load-balancer/actions/workflows/validate.yaml)

---

## Features

- **Phase-specific load balancing** — measures and balances current per phase (L1, L2, L3)
- **Event-driven calculation** with immediate downward adjustment on load increase
- **Automatic phase switching** — dynamically switches between 3-phase and 1-phase charging
- **Hysteresis against hunting** — debouncer (5s) prevents rapid on/off oscillation
- **Failsafe on sensor loss** — reduce (6A) or pause depending on configuration
- **Configurable safety margin** — adds a buffer below the fuse limit
- **Charger profiles** — modular support for different charger models (MVP: go-e Gemini flex)
- **HA events** for automations and notifications on all state changes
- **Capacity warning sensor** — binary sensor + event when capacity margin is low
- **Utilization sensor** — shows fuse utilization as a percentage

---

## How It Works

EV Load Balancer operates in a continuous cycle: measure → calculate → control.

```
Current per phase at the fuse
  sensor.current_be1_30051 (L1: 18.3 A)
  sensor.current_be2_30051 (L2: 12.1 A)
  sensor.current_be3_30051 (L3: 15.7 A)
            │
            ▼
    ┌─────────────────────────────────┐
    │         Calculation             │
    │                                 │
    │  Available per phase:           │
    │  L1: 25 - 2 (margin) - 18.3    │
    │     = 4.7 A                     │
    │  L2: 25 - 2 - 12.1 = 10.9 A    │
    │  L3: 25 - 2 - 15.7 = 7.3 A     │
    │                                 │
    │  available_min = min(4.7,       │
    │    10.9, 7.3) = 4.7 A           │
    │                                 │
    │  target_current = floor(4.7)    │
    │    = 4 → clamped to [6, 16] A  │
    └─────────────────────────────────┘
            │
            ▼
    ┌───────────────────┐    ┌─────────────────────┐
    │   Control         │    │   Pause logic       │
    │                   │    │                     │
    │  Immediate down-  │    │  available_min      │
    │  ward adjustment  │    │  < min 15s→ PAUSE   │
    │  on load          │    │                     │
    │  increase         │    │  > min+2A 30s→RESUME│
    │                   │    │                     │
    │  5s cooldown      │    │  Phase switching:   │
    │  upward adjust.   │    │  all phases >= 6A   │
    └───────────────────┘    │  for 60s → 3-phase  │
            │                └─────────────────────┘
            ▼
  number.goe_{serial}_amp  (sets charging current)
  select.goe_{serial}_frc  (pause/resume)
  select.goe_{serial}_psm  (phase selection)
```

> **Simplification:** The diagram above shows a simplification — the actual calculation
> subtracts the charger's own draw (`device_load`) from the phase load before calculating
> available capacity: `max_a - (phase_load - device_load) - safety_margin`.

### Calculation logic (step by step)

1. **Measurement**: Reads current per phase from configured phase sensors
2. **Available capacity**: `(max_ampere - safety_margin - measured_current)` per phase
3. **Limiting phase**: `available_min = min(available_L1, available_L2, available_L3)`
4. **Target current**: `target_current = floor(available_min)`, clamped to [min_current, max_current]
5. **Control**: Writes new current to the charger entity — immediately on downward adjustment, 5s debounce on upward adjustment
6. **Pause**: If `available_min < min_current` for 15 seconds → pause charging
7. **Resume**: If `available_min > (min_current + 2A)` for 30 seconds → resume charging

---

## Prerequisites

Verify the following before installation:

**Home Assistant:**
- [ ] Home Assistant 2024.1.0 or later
- [ ] HACS installed ([instructions](https://hacs.xyz/docs/setup/download))

**Hardware:**
- [ ] EV charger installed and connected to the home network
- [ ] Per-phase current measurement at the main fuse installed

**Home Assistant integrations:**
- [ ] Charger integration installed in HA (e.g. [ha-goecharger-api2](https://github.com/cathiele/ha-goecharger-api2) for go-e)
- [ ] Phase sensors exposed in HA (e.g. via Nibe Modbus, P1 port, Shelly EM)

**Network requirements:**
- [ ] Home Assistant and the EV charger on the same network (or routable)
- [ ] Charger API accessible (local network communication, no cloud required)

**Current measurement — options:**

| Option | Integration | Sensors |
|---|---|---|
| Nibe Modbus (heat pump) | nibe-hass | `sensor.current_be1_30051`, `be2`, `be3` |
| P1 port (Tibber, Easee home) | tibber-local | Per-phase current |
| Shelly EM | Shelly HA | Per-phase current |
| Eigil Stromberg | Modbus | Per-phase current |

> **Important:** The sensors must show current in Ampere per phase *at the main fuse* —
> not at the distribution board or at the charger. It is the total load per phase
> that determines whether the fuse is at risk of tripping.

---

## Charger Setup

### go-e Charger Gemini flex

See the full setup guide:
[docs/charger-profiles/goe-gemini.md](docs/charger-profiles/goe-gemini.md)

The guide covers:
- Firmware requirements (≥ 59.x, API v2)
- Installation of `ha-goecharger-api2`
- All entities that EV Load Balancer needs
- Recommended `ama` value (safe maximum)
- Verified phase mapping

### Other chargers

EV Load Balancer supports any charger that exposes the right entities in Home Assistant.
See [docs/charger-profiles/README.md](docs/charger-profiles/README.md) for requirements and
how to contribute support for your charger model.

**General requirements for the charger integration:**

- [ ] `number.*_amp` — current setting (6–32A), readable and writable
- [ ] `select.*_frc` — force charge status (pause/resume), writable
- [ ] `select.*_psm` (optional) — phase selection (1/3 phase), writable
- [ ] `sensor.*_nrg_4/5/6` (optional) — current per phase, readable

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant (**HACS** → **Integrations**)
2. Click the menu (⋮) at the top right → **Custom repositories**
3. Paste the URL: `https://github.com/cryptotomte/ev-load-balancer`
4. Select category: **Integration**
5. Click **Add** → find **EV Load Balancer** in the list → click **Download**
6. Restart Home Assistant
7. Go to **Settings** → **Devices & Services** → **+ Add integration**
8. Search for **EV Load Balancer** and follow the configuration guide below

### Manual installation

1. Copy the folder `custom_components/ev_load_balancer/` to your HA
   `custom_components/` folder
2. Restart Home Assistant
3. Add the integration via **Settings** → **Devices & Services**

---

## Configuration

### Config Flow (initial setup)

The configuration flow consists of six steps:

| Step | Content |
|---|---|
| **1. Charger profile** | Select charger model (e.g. go-e Gemini flex) |
| **2. Serial number** | Enter the charger's serial number |
| **3. Entities** | Confirm or adjust entity IDs |
| **4. Phases** | Configure phase sensors and fuse limits per phase |
| **5. Parameters** | Safety margin, min/max current, failsafe behavior |
| **6. Confirm** | Review settings and create the integration |

### Options Flow (post-configuration)

Settings can be adjusted afterwards without having to remove and recreate the integration:

- **Phase sensors** — change sensors or fuse limits
- **Calculation parameters** — safety margin, min/max current
- **Failsafe behavior** — action on sensor loss (reduce/pause) and safe default current
- **Capacity warning threshold** — limit in Ampere for warning sensor (default 3A)

Open Options Flow via: **Settings** → **Devices & Services** → **EV Load Balancer** → ⚙️

---

## Who Does What?

Responsibility split between EV Load Balancer, the charger integration and
EV Charging Manager (EVCM).

| Function | EV Load Balancer | Charger integration | EV Charging Manager |
|---|---|---|---|
| Current measurement at fuse | Reads sensors | — | — |
| Calculate available capacity | ✅ | — | — |
| Control charging current (amp) | ✅ Writes | Exposes entity | — |
| Pause/resume (frc) | ✅ Writes | Exposes entity | — |
| Phase switching (psm) | ✅ Writes | Exposes entity | — |
| Failsafe on sensor loss | ✅ | — | — |
| Identify who is charging | — | — | ✅ (RFID) |
| Session management | — | — | ✅ |
| Cost calculation | — | — | ✅ |
| Spot price reading | — | — | ✅ (reads, does not control) |
| API communication with charger | — | ✅ | — |

**EV Load Balancer** focuses solely on load balancing — keeping the grid load
below the fuse limit. The integration does *not* communicate directly with the
charger hardware but goes via the entities exposed by the charger integration.

**The charger integration** (e.g. ha-goecharger-api2) is responsible for all
communication with the charger and exposes the entities that EV Load Balancer controls.

**EV Charging Manager** (separate integration) handles sessions, RFID cards,
cost calculation and spot price optimization — features that are outside
EV Load Balancer's scope.

> The three components are **independent** and can be installed individually.
> EV Load Balancer + charger integration + EVCM can run simultaneously without conflict.

---

## Optional: InfluxDB + Grafana

EV Load Balancer can export load balancing decisions to InfluxDB for
historical analysis and visualization in Grafana.

### Flow A — Continuous sensor data (HA built-in)

Home Assistant automatically logs sensors to its internal database. Configure
the InfluxDB integration in HA to also send these sensors:

```yaml
# configuration.yaml
influxdb:
  host: <your-influxdb-host>
  include:
    entities:
      - sensor.ev_load_balancer_status
      - sensor.ev_load_balancer_available_min
      - sensor.ev_load_balancer_target_current
      - sensor.ev_load_balancer_utilization
```

### Flow B — Event export (automation template)

Import the automation template `automations/ev_load_balancer_influxdb_export.yaml`
in Home Assistant to export all load balancing events to
measurement `ev_load_balancer_events`.

**Events exported:**

| Event | Measurement | Tags | Fields |
|---|---|---|---|
| `ev_load_balancer_current_adjusted` | ev_load_balancer_events | event_type, charger_profile, state | old_current, new_current, available, reason |
| `ev_load_balancer_device_paused` | ev_load_balancer_events | event_type, charger_profile, state | reason, available_min |
| `ev_load_balancer_device_resumed` | ev_load_balancer_events | event_type, charger_profile, state | reason, available_min |
| `ev_load_balancer_phase_switched` | ev_load_balancer_events | event_type, charger_profile | from_mode, to_mode, reason |

### Grafana queries

| Visualization | InfluxDB query (concept) |
|---|---|
| Current adjustments per day | `COUNT(new_current) WHERE event_type='current_adjusted' GROUP BY time(1d)` |
| Average allocated current | `MEAN(new_current) WHERE event_type='current_adjusted' GROUP BY time(1h)` |
| Number of pauses per week | `COUNT(*) WHERE event_type='device_paused' GROUP BY time(7d)` |
| Pause reasons (distribution) | `COUNT(*) WHERE event_type='device_paused' GROUP BY reason` |
| Phase switches per day | `COUNT(*) WHERE event_type='phase_switched' GROUP BY time(1d)` |
| Capacity utilization | `MEAN(utilization_pct) GROUP BY time(1h)` |
| Lowest margin per day | `MIN(available_min) GROUP BY time(1d)` |

---

## Troubleshooting

### Charger does not respond to adjustments

**Symptom:** Charging current does not change even though EV Load Balancer is
active and shows a calculated value.

**Check:**
1. Verify that the charger is in the correct mode — `frc` must allow external control
   (not forced off or on)
2. Check that entity IDs are correctly configured (**Settings** → **EV Load Balancer** → ⚙️)
3. Check HA logs (`home-assistant.log`) for error messages from EV Load Balancer
4. Verify that the charger integration is active and that entities do not show `unavailable`

### Sensors show `unavailable`

**Symptom:** `sensor.ev_load_balancer_status` or phase sensors show `unavailable`.

**Check:**
1. Verify that the phase sensors work: open **Developer Tools** → **States**
   and search for your phase sensors
2. Check the network connection to the current meter (Nibe, Shelly, P1 port)
3. If phase sensors are unavailable the **failsafe** activates — check the selected action
   (reduce/pause) in Options Flow
4. Restart Home Assistant and wait ~30 seconds for sensors to initialize

### Fuse tripped anyway

**Symptom:** Despite EV Load Balancer being active the fuse trips.

**Check:**
1. Check the size of the safety margin — increase from default (2A) to 3–5A
   via Options Flow
2. Verify that the phase sensors measure at the *correct point* — at the main fuse,
   not at the distribution board or the charger
3. Check that `ama` (absolute maximum value) on the charger is set to a safe
   value (recommendation: 10A) — see [goe-gemini.md](docs/charger-profiles/goe-gemini.md)
4. Verify that no other large loads (stove, oven) switched on instantaneously —
   EV Load Balancer reacts to sensor readings, not predictively

### HA restart — integration shows INITIALIZING

**Symptom:** After HA restart the integration is in `INITIALIZING` state
for a while.

**Explanation:** This is normal behavior. After a restart the integration waits for
Home Assistant to publish initial sensor values before it can calculate the
correct current. This typically takes 10–30 seconds.

**If the state persists:**
1. Check that the phase sensors return numeric values (not `unavailable`)
2. Verify that the charger integration is correctly configured and the entities are
   available
3. Check the logs for any errors during startup

---

## Charger Profiles (Community)

EV Load Balancer is designed with a modular profile architecture. The MVP version
includes support for go-e Gemini flex. Support for more chargers is added via
community contributions.

**Available profiles:**

| Profile | Charger | Status | Guide |
|---|---|---|---|
| `goe_gemini` | go-e Charger Gemini flex | ✅ MVP | [goe-gemini.md](docs/charger-profiles/goe-gemini.md) |

**Want to contribute support for your charger?**

Read [docs/charger-profiles/README.md](docs/charger-profiles/README.md) for a
template and instructions on how to open a Pull Request.

---

## Relation to EV Charging Manager

[EV Charging Manager](https://evchargermanager.de/) (EVCM) is a separate
integration for Home Assistant that handles charging sessions, RFID cards,
cost calculation and scheduling based on spot prices.

**EV Load Balancer and EVCM are independent integrations** that address
different problems:

- **EV Load Balancer** → *How much can I charge right now without the fuse tripping?*
- **EV Charging Manager** → *Who is charging? What does it cost? When should I schedule charging?*

They can and **should be installed together** if you want complete
charging management. EV Load Balancer sets the capacity ceiling — EVCM optimizes
the schedule within that ceiling.

> **Note:** EV Charging Manager can read spot prices and plan charging,
> but does not control load balancing. EV Load Balancer does not control charging
> schedules or price sensitivity — it is purely real-time balancing.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

Copyright (c) 2026 cryptotomte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
