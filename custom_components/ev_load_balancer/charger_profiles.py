"""Laddarprofiler för EV Load Balancer.

Definierar dataklasserna SensorDef, ControlDef och ChargerProfile,
samt de konkreta profilerna goe_gemini och generic.
Ingen HA-runtime-logik — ren Python, testbar utan HA.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# T012: Dataklasser för sensor- och kontrolldefinitioner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensorDef:
    """Definition av en sensor-entitet hos laddaren (read-only)."""

    entity_pattern: str
    """Entitetsmönster med {serial}-placeholder, t.ex. 'sensor.goe_{serial}_nrg_4'."""

    platform: str
    """HA-plattform: 'sensor' eller 'binary_sensor'."""

    unit: str
    """Mätenhet, t.ex. 'A', 'W' eller '' för dimensionslösa värden."""

    description: str = ""
    """Fritext-beskrivning av sensorn."""

    allowed_values: list[str] | None = None
    """Tillåtna strängvärden för tillståndssensorer (t.ex. bilistatus)."""


@dataclass(frozen=True)
class ControlDef:
    """Definition av en styrentitet hos laddaren (writable)."""

    entity_pattern: str
    """Entitetsmönster med {serial}-placeholder, t.ex. 'number.goe_{serial}_amp'."""

    platform: str
    """HA-plattform: 'number' eller 'select'."""

    unit: str
    """Mätenhet, t.ex. 'A' eller '' för dimensionslösa värden."""

    flash: bool = False
    """True = skrivs till flash-minne. MÅSTE sättas en gång vid installation (Princip I)."""

    description: str = ""
    """Fritext-beskrivning av kontrollen."""


# ---------------------------------------------------------------------------
# T013: ChargerProfile-dataklassen med resolve()-metoden
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChargerProfile:
    """Komplett profil för en laddarmodell.

    Kapslar in all laddarspecifik logik — inga hårdkodade entitets-ID:n
    utanför denna modul (Konstitution Princip III).
    """

    id: str
    """Unikt identifierare för profilen, t.ex. 'goe_gemini'."""

    name: str
    """Läsbart namn, t.ex. 'go-e Charger Gemini flex'."""

    manufacturer: str
    """Tillverkarens namn."""

    requires_serial: bool
    """True om profilen kräver ett serienummer för att bilda entity-ID:n."""

    sensors: dict[str, SensorDef] = field(default_factory=dict)
    """Nyckel → SensorDef-mappning för alla sensorer."""

    controls: dict[str, ControlDef] = field(default_factory=dict)
    """Nyckel → ControlDef-mappning för alla styrningsentiteter."""

    capabilities: frozenset[str] = field(default_factory=frozenset)
    """Kapabilitetsflaggor som styr tillgängliga funktioner i logiken."""

    def resolve(self, serial: str) -> dict[str, str]:
        """Returnerar konkreta entity-ID:n för ett givet serienummer.

        Args:
            serial: Laddarens serienummer, t.ex. '409787'.

        Returns:
            Flat dict med nyckel → konkret entity-ID.

        Raises:
            ValueError: Om serial är en tom sträng.
        """
        if not serial:
            raise ValueError("serial får inte vara tom")

        result: dict[str, str] = {}
        for key, sensor in self.sensors.items():
            result[key] = sensor.entity_pattern.format(serial=serial)
        for key, control in self.controls.items():
            result[key] = control.entity_pattern.format(serial=serial)
        return result


# ---------------------------------------------------------------------------
# T014 + T015 + T016: go-e Charger Gemini flex-profilen
# ---------------------------------------------------------------------------

_GOE_GEMINI_PROFILE = ChargerProfile(
    id="goe_gemini",
    name="go-e Charger Gemini flex",
    manufacturer="go-e",
    requires_serial=True,
    sensors={
        # T014: Alla 11 sensorer
        "nrg_4": SensorDef(
            entity_pattern="sensor.goe_{serial}_nrg_4",
            platform="sensor",
            unit="A",
            description="L1 ström",
        ),
        "nrg_5": SensorDef(
            entity_pattern="sensor.goe_{serial}_nrg_5",
            platform="sensor",
            unit="A",
            description="L2 ström",
        ),
        "nrg_6": SensorDef(
            entity_pattern="sensor.goe_{serial}_nrg_6",
            platform="sensor",
            unit="A",
            description="L3 ström",
        ),
        "nrg_7": SensorDef(
            entity_pattern="sensor.goe_{serial}_nrg_7",
            platform="sensor",
            unit="W",
            description="L1 effekt",
        ),
        "nrg_8": SensorDef(
            entity_pattern="sensor.goe_{serial}_nrg_8",
            platform="sensor",
            unit="W",
            description="L2 effekt",
        ),
        "nrg_9": SensorDef(
            entity_pattern="sensor.goe_{serial}_nrg_9",
            platform="sensor",
            unit="W",
            description="L3 effekt",
        ),
        "nrg_11": SensorDef(
            entity_pattern="sensor.goe_{serial}_nrg_11",
            platform="sensor",
            unit="W",
            description="Total effekt",
        ),
        "car_value": SensorDef(
            entity_pattern="sensor.goe_{serial}_car_value",
            platform="sensor",
            unit="",
            description="Bilstatus",
            allowed_values=["Idle", "Charging", "WaitCar", "Complete"],
        ),
        "map": SensorDef(
            entity_pattern="sensor.goe_{serial}_map",
            platform="sensor",
            unit="",
            description="Faskarta — visar aktiv fas-konfiguration",
        ),
        "fsp": SensorDef(
            entity_pattern="binary_sensor.goe_{serial}_fsp",
            platform="binary_sensor",
            unit="",
            description="Tvingad enfas",
        ),
        "acu": SensorDef(
            entity_pattern="sensor.goe_{serial}_acu",
            platform="sensor",
            unit="A",
            description="Tillgänglig ström (bekräftad av laddaren)",
        ),
        "pha": SensorDef(
            entity_pattern="sensor.goe_{serial}_pha",
            platform="sensor",
            unit="",
            description="Aktiverade faser efter kontaktorn (JSON-array med 6 booleans)",
        ),
    },
    controls={
        # T015: Alla 4 styrningsentiteter
        "amp": ControlDef(
            entity_pattern="number.goe_{serial}_amp",
            platform="number",
            unit="A",
            flash=False,
            description="Dynamisk laddström — den primära styrparametern",
        ),
        "frc": ControlDef(
            entity_pattern="select.goe_{serial}_frc",
            platform="select",  # ALDRIG number — säkerhetskritiskt (Princip III)
            unit="",
            flash=False,
            description="Tvinga laddstatus: '0'=neutral, '1'=stoppa, '2'=ladda",
        ),
        "psm": ControlDef(
            entity_pattern="select.goe_{serial}_psm",
            platform="select",  # ALDRIG number — säkerhetskritiskt (Princip III)
            unit="",
            flash=False,
            description="Fasväxling: '1'=enfas, '2'=trefas",
        ),
        "ama": ControlDef(
            entity_pattern="number.goe_{serial}_ama",
            platform="number",
            unit="A",
            flash=True,  # Skrivs till flash — sätts EN GÅNG vid installation (Princip I)
            description="Absolut maxström — sätts EN GÅNG vid installation, skrivs till flash",
        ),
    },
    # T016: Alla 8 kapabiliteter
    # OBS: min_current_sensor deklareras som kapabilitet men tillhörande
    # entity (mca) definieras i PR-02+ — ingen mca-entitet skapas i PR-01.
    capabilities=frozenset(
        {
            "per_phase_current",
            "per_phase_power",
            "phase_detection",
            "dynamic_current",
            "pause_resume",
            "phase_switching",
            "car_status",
            "min_current_sensor",
        }
    ),
)


# ---------------------------------------------------------------------------
# T025: Generic-profil som stub (måste definieras FÖRE PROFILES nedan)
# ---------------------------------------------------------------------------

_GENERIC_PROFILE = ChargerProfile(
    id="generic",
    name="Other / Manual configuration",
    manufacturer="—",
    requires_serial=False,
)


# ---------------------------------------------------------------------------
# T017: PROFILES-konstant (refererar till båda profilerna ovan)
# ---------------------------------------------------------------------------

PROFILES: dict[str, ChargerProfile] = {
    "goe_gemini": _GOE_GEMINI_PROFILE,
    "generic": _GENERIC_PROFILE,
}
