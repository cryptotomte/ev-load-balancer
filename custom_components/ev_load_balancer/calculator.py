"""Beräkningsmotor för EV Load Balancer.

Fasmedveten beräkning av tillgänglig ström och optimal laddström.
Ren Python utan HA-beroenden — fullt testbar med vanlig pytest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CalculationResult:
    """Resultatet av en beräkningscykel.

    Innehåller beräknad target_current, tillgänglig ström per fas
    samt metadata om beräkningen.
    """

    target_current: int
    """Beräknad laddström i ampere — avrundad nedåt, klämd till [min, max]."""

    available_l1: float
    """Tillgänglig ström på fas L1 (A). Kan vara negativ vid kapacitetsbrist."""

    available_l2: float
    """Tillgänglig ström på fas L2 (A). Kan vara negativ vid kapacitetsbrist."""

    available_l3: float
    """Tillgänglig ström på fas L3 (A). Kan vara negativ vid kapacitetsbrist."""

    available_min: float
    """Minsta tillgängliga ström bland aktiva faser (A)."""

    active_phases: list[int]
    """Lista med aktiva fasnummer, t.ex. [1, 2, 3] eller [2]."""

    phase_loads: list[float]
    """Hushållets fassensorvärden i konfigurationsordning (A)."""

    device_loads: list[float]
    """Laddarens egna uttag per fas i konfigurationsordning (A)."""

    charging_mode: str
    """Laddningsläge: '1-phase' eller '3-phase'."""

    calculation_time: datetime = field(default_factory=datetime.now)
    """Tidpunkt för beräkning."""


def calculate(
    phases: list[dict],
    phase_values: list[float],
    device_values: list[float],
    active_phase_numbers: list[int],
    safety_margin: float,
    min_current: int,
    max_current: int,
) -> CalculationResult:
    """Beräknar tillgänglig ström per fas och optimal target_current.

    Formel per fas i:
        available_i = max_ampere_i - (phase_value_i - device_value_i) - safety_margin

    target_current = clamp(floor(min(available_i for i in active_phases)),
                           min_current, max_current)

    Args:
        phases: Lista med fasdefinitioner, varje element är en dict med
                nycklarna "sensor", "max_ampere" och "label".
        phase_values: Fassensorvärden i samma ordning som phases (A).
                      Inkluderar laddarens uttag.
        device_values: Laddarens egna uttag per fas i samma ordning (A).
                       Använd 0.0 om sensorn saknas (generic-profil).
        active_phase_numbers: Lista med aktiva fasnummer (1-baserat),
                              t.ex. [1, 2, 3] eller [2].
        safety_margin: Säkerhetsmarginal i ampere som subtraheras.
        min_current: Lägsta tillåtna laddström i ampere.
        max_current: Högsta tillåtna laddström i ampere.

    Returns:
        CalculationResult med beräknat target_current och metadata.
    """
    n = len(phases)

    # Beräkna tillgänglig ström per fas (alla 3 — även inaktiva)
    available = [0.0, 0.0, 0.0]  # L1, L2, L3 (index 0-2)
    for i in range(n):
        max_a = float(phases[i].get("max_ampere", 25))
        phase_load = phase_values[i] if i < len(phase_values) else 0.0
        device_load = device_values[i] if i < len(device_values) else 0.0
        available[i] = max_a - (phase_load - device_load) - safety_margin

    # Samla tillgänglig ström för aktiva faser
    active_available: list[float] = []
    for phase_num in active_phase_numbers:
        idx = phase_num - 1  # fasnummer 1 → index 0
        if 0 <= idx < n:
            active_available.append(available[idx])

    # Om inga aktiva faser — använd alla konfigurerade faser som fallback
    if not active_available:
        active_available = [available[i] for i in range(n)]

    # Minsta tillgängliga ström bland aktiva faser
    available_min = min(active_available)

    # Beräkna target_current: floor, sedan kläm till [min_current, max_current]
    target = int(math.floor(available_min))
    target_current = max(min_current, min(max_current, target))

    # Avgör laddningsläge
    charging_mode = "3-phase" if len(active_phase_numbers) > 1 else "1-phase"

    return CalculationResult(
        target_current=target_current,
        available_l1=available[0],
        available_l2=available[1],
        available_l3=available[2],
        available_min=available_min,
        active_phases=list(active_phase_numbers),
        phase_loads=list(phase_values[:n]),
        device_loads=list(device_values[:n]),
        charging_mode=charging_mode,
    )
