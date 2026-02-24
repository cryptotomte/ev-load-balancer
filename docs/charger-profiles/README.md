# Charger Profiles — Community Contribution Guide

EV Load Balancer supports different EV chargers via a modular profile system.
Each profile defines which entities the charger exposes and how they are mapped
to the integration's control commands.

---

## What is a charger profile?

A charger profile is a configuration in `charger_profiles.py` that describes:

1. **Entities** — which HA entities the charger exposes (amp, frc, psm, etc.)
2. **Control commands** — how to pause, resume and switch phases
3. **Phase handling** — phase mapping and behavior for 1-phase vs. 3-phase charging
4. **Validation** — which entities are mandatory vs. optional

---

## Available profiles

| Profile ID | Charger | Status | Guide |
|---|---|---|---|
| `goe_gemini` | go-e Charger Gemini flex | ✅ Supported in MVP | [goe-gemini.md](goe-gemini.md) |

---

## What a new charger profile needs

### Mandatory entities

| Entity type | Function | HA type |
|---|---|---|
| `amp` | Current setting (6–32A) | `number` — readable and writable |
| `frc` | Pause/resume charging | `select` — writable with specific values |

### Optional entities (extends functionality)

| Entity type | Function | HA type |
|---|---|---|
| `psm` | Phase selection (1-phase/3-phase) | `select` — required for automatic phase switching |
| `ama` | Absolute maximum current (safety limit) | `number` — set ONCE |
| `nrg_4` | Measured current L1 | `sensor` — used for load calibration |
| `nrg_5` | Measured current L2 | `sensor` — used for load calibration |
| `nrg_6` | Measured current L3 | `sensor` — used for load calibration |
| `map` | Phase map (read-only) | `sensor` — internal phase mapping |
| `car_value` | Car connection status | `sensor` — optional status info |

### Control values for frc

The profile must specify which select values correspond to "pause" and "charge":

```python
"frc": {
    "entity_key": "frc",
    "pause_value": "<pause-value>",    # e.g. "1" or "Stop"
    "resume_value": "<charge-value>",  # e.g. "2" or "Charge"
}
```

### Control values for psm (phase selection)

If the charger supports phase switching, specify which select values activate 1-phase
and 3-phase charging respectively:

```python
"psm": {
    "entity_key": "psm",
    "one_phase_value": "<1-phase-value>",   # e.g. "1" or "Single"
    "three_phase_value": "<3-phase-value>", # e.g. "2" or "Three"
}
```

### Phase handling

Specify which physical phase the charger uses in 1-phase mode:

```python
"phase_mapping": {
    "single_phase_uses": "L2",  # or "L1" depending on the charger's behavior
}
```

> go-e Gemini flex always charges on L2 in 1-phase mode — other chargers
> may have different behavior. Verify with actual hardware.

---

## Template for a new charger profile

Add the profile to `charger_profiles.py` using the `ChargerProfile` dataclass.
See the imports at the top of the file: `from dataclasses import dataclass, field`.

```python
_MY_CHARGER_PROFILE = ChargerProfile(
    id="my_charger",
    name="<Charger model>",
    manufacturer="<Manufacturer>",
    requires_serial=True,
    sensors={
        # OPTIONAL: per-phase current sensors (A)
        "nrg_4": SensorDef(
            entity_pattern="sensor.<prefix>_{serial}_nrg_4",
            platform="sensor",
            unit="A",
            description="L1 current",
        ),
        "nrg_5": SensorDef(
            entity_pattern="sensor.<prefix>_{serial}_nrg_5",
            platform="sensor",
            unit="A",
            description="L2 current",
        ),
        "nrg_6": SensorDef(
            entity_pattern="sensor.<prefix>_{serial}_nrg_6",
            platform="sensor",
            unit="A",
            description="L3 current",
        ),
        # OPTIONAL: car status
        "car_value": SensorDef(
            entity_pattern="sensor.<prefix>_{serial}_car_value",
            platform="sensor",
            unit="",
            description="Car status",
        ),
    },
    controls={
        # MANDATORY: current setting
        "amp": ControlDef(
            entity_pattern="number.<prefix>_{serial}_amp",
            platform="number",
            unit="A",
            flash=False,
            description="Dynamic charging current",
        ),
        # MANDATORY: pause/resume
        "frc": ControlDef(
            entity_pattern="select.<prefix>_{serial}_frc",
            platform="select",  # ALWAYS select — never number
            unit="",
            flash=False,
            description="Force charge status: '<pause-value>'=stop, '<charge-value>'=charge",
        ),
        # OPTIONAL: phase switching
        "psm": ControlDef(
            entity_pattern="select.<prefix>_{serial}_psm",
            platform="select",  # ALWAYS select — never number
            unit="",
            flash=False,
            description="Phase switching: '<1-phase-value>'=single phase, '<3-phase-value>'=three phase",
        ),
        # OPTIONAL: safety maximum — written ONCE with flash=True
        "ama": ControlDef(
            entity_pattern="number.<prefix>_{serial}_ama",
            platform="number",
            unit="A",
            flash=True,  # Written to flash — set ONCE at installation
            description="Absolute maximum current — set ONCE, not dynamic",
        ),
    },
    capabilities=frozenset(
        {
            "dynamic_current",   # MANDATORY: current setting supported
            "pause_resume",      # MANDATORY: pause/resume supported
            # "per_phase_current",  # Add if nrg_4/5/6 are defined
            # "phase_switching",    # Add if psm is defined
            # "car_status",         # Add if car_value is defined
        }
    ),
)

# Register the profile in the PROFILES dict (at the bottom of the file)
PROFILES["my_charger"] = _MY_CHARGER_PROFILE
```

---

## Checklist for a new profile

Before opening a Pull Request — verify:

**Entities:**
- [ ] `amp` — tested that current setting actually changes the charging current
- [ ] `frc` — tested that pause/resume works with the specified select values
- [ ] `psm` (if applicable) — tested phase switching on actual hardware
- [ ] `ama` (if applicable) — confirmed that the value is persistent (flash)

**Phase handling:**
- [ ] Verified which physical phase is used in 1-phase mode
- [ ] Documented the phase mapping in the setup guide for the profile

**Documentation:**
- [ ] Created `docs/charger-profiles/<profile-id>.md` with setup guide
- [ ] Included firmware requirements and HA integration link
- [ ] Listed all entities with actual HA entity IDs

**Tests:**
- [ ] No regressions in existing tests (`uv run pytest tests/ -v`)
- [ ] Config flow works with the new profile

---

## How to contribute via Pull Request

1. **Fork** the repo: `https://github.com/cryptotomte/ev-load-balancer`

2. **Create a branch** with the charger's model name:
   ```bash
   git checkout -b feat/add-<charger-model>-profile
   ```

3. **Add the profile** to `custom_components/ev_load_balancer/charger_profiles.py`
   — follow the template above

4. **Create the setup guide** in `docs/charger-profiles/<profile-id>.md`
   — use [goe-gemini.md](goe-gemini.md) as a reference

5. **Update the profile table** in `docs/charger-profiles/README.md`

6. **Run tests** and verify that everything passes:
   ```bash
   uv run pytest tests/ -v
   ruff format custom_components/ tests/
   ruff check --fix custom_components/ tests/
   ```

7. **Open a Pull Request** against `main` with:
   - Charger model and firmware version in the title
   - Which entities are supported (mandatory + optional)
   - Confirmation that it has been tested on actual hardware
   - Photo/screenshot of the charger if possible

---

## Reference: charger_profiles.py

Profiles are defined in:

```
custom_components/ev_load_balancer/charger_profiles.py
```

API of interest for profile development:

| Symbol | Type | Description |
|---|---|---|
| `PROFILES` | `dict[str, ChargerProfile]` | All registered profiles — look up with `PROFILES["goe_gemini"]` |
| `ChargerProfile.resolve(serial)` | Method | Returns `dict[str, str]` with concrete entity IDs for a serial number |
| `ChargerProfile.sensors` | Attribute | `dict[str, SensorDef]` — all sensor definitions |
| `ChargerProfile.controls` | Attribute | `dict[str, ControlDef]` — all control definitions |
| `ChargerProfile.capabilities` | Attribute | `frozenset[str]` — capability flags |

---

## Questions and support

- **GitHub Issues:** `https://github.com/cryptotomte/ev-load-balancer/issues`
- **Discussions:** `https://github.com/cryptotomte/ev-load-balancer/discussions`

Tag your issue with `charger-profile` for faster handling.
