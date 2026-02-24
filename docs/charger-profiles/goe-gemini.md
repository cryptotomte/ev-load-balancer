# go-e Charger Gemini flex — Setup Guide

This guide describes how to configure a go-e Charger Gemini flex for
use with EV Load Balancer.

---

## Firmware Requirements

| Requirement | Minimum version |
|---|---|
| Firmware | **≥ 59.x** |
| API | **v2** (enabled by default from firmware 54.x) |

> Check the firmware version in the go-e app settings or via the web interface
> at `http://<charger-ip>/api/status`. Update if necessary via the go-e app.

---

## Home Assistant Integration

Use **[ha-goecharger-api2](https://github.com/cathiele/ha-goecharger-api2)**
(HACS Custom Repository).

### Installation

1. Open HACS → **Custom repositories**
2. Add: `https://github.com/cathiele/ha-goecharger-api2`
3. Category: **Integration**
4. Install and restart Home Assistant
5. Add the integration via **Settings** → **Devices & Services** → **ha-goecharger-api2**
6. Enter the charger's IP address (local network address — no cloud)

---

## Entity List

Replace `{serial}` with your go-e charger's serial number (e.g. `409787`).

| Entity | HA entity ID | Type | Direction | Usage |
|---|---|---|---|---|
| Charging current | `number.goe_{serial}_amp` | number | R/W | EV Load Balancer sets charging current (6–32A) |
| Forced status (frc) | `select.goe_{serial}_frc` | select | R/W | Pause/resume charging |
| Phase selection (psm) | `select.goe_{serial}_psm` | select | R/W | Phase switching (1-phase/3-phase) |
| Absolute max (ama) | `number.goe_{serial}_ama` | number | W | Set ONCE — last line of defense |
| Phase L1 current | `sensor.goe_{serial}_nrg_4` | sensor | R | Measured current on phase L1 (A) |
| Phase L2 current | `sensor.goe_{serial}_nrg_5` | sensor | R | Measured current on phase L2 (A) |
| Phase L3 current | `sensor.goe_{serial}_nrg_6` | sensor | R | Measured current on phase L3 (A) |
| Phase map | `sensor.goe_{serial}_map` | sensor | R | Read-only — phase mapping (internal) |
| Car status | `sensor.goe_{serial}_car_value` | sensor | R | Car connection status |

### Frc values (select)

| Value | Meaning |
|---|---|
| `0` | Neutral (charger controls itself) |
| `1` | Off (force off) |
| `2` | Charge (force on) |

EV Load Balancer uses:
- `1` → pause (frc = "off")
- `2` → resume/activate (frc = "on")

### Psm values (select)

| Value | Phase mode |
|---|---|
| `1` | 1-phase charging |
| `2` | 3-phase charging |

---

## Recommended ama Value

**Set `ama` to 10A ONCE and leave it.**

`ama` (absolute maximum ampere) is the charger's built-in safety limit.
If Home Assistant crashes, the network loses connection or EV Load Balancer
stops responding, the charger falls back to the `ama` value.

- **Recommendation: 10A** — a safe value that does not stress the fuse
  but still allows charging during HA outages
- Set via: `number.goe_{serial}_ama` → enter `10` → save

> **Important:** Do NOT write `ama` dynamically. EV Load Balancer writes `ama`
> with `flash=True` (written to the charger's flash memory) during initial configuration
> and never changes it afterwards. Frequent flash writes wear down the memory chip.

---

## Verified Phase Mapping

go-e Gemini flex has an **important quirk** in 1-phase charging:

> **go-e's 1-phase mode (psm=1) always charges on L2**, not on L1.

This means that when the integration calculates available capacity in 1-phase mode
it is the L2 sensor (`sensor.goe_{serial}_nrg_5`) that is the limiting phase —
not L1 as one might expect.

EV Load Balancer handles this automatically via the charger profile (`goe_gemini`)
which specifies the correct phase mapping. You do not need to configure this manually.

**Confirmed on firmware 59.x with ha-goecharger-api2.**

---

## Configuration in EV Load Balancer

When configuring EV Load Balancer you select the profile **go-e Charger Gemini flex**
and enter the serial number. The integration then fills in the entity IDs automatically
based on the serial number.

Verify that all entities are visible in HA before starting the configuration flow:

```
Developer Tools → States → search for "goe_<your-serial-number>"
```

---

## Troubleshooting

### Entities missing in HA

- Check that ha-goecharger-api2 is correctly configured and the charger responds
- Test direct connection: `http://<charger-ip>/api/status` should return JSON
- Check that API v2 is enabled in the go-e app settings

### Charger charges on wrong phase

- Check `sensor.goe_{serial}_map` — shows current phase mapping
- In 1-phase mode: charging should occur on L2 (go-e's behavior)
- Check firmware version — older firmware may have a different phase mapping

### ama reverts to factory value

- Check that `ama` was set correctly during the initial configuration — EV Load Balancer
  writes `ama` a single time during installation and does not change it thereafter
- Check that `number.goe_{serial}_ama` is available and writable
