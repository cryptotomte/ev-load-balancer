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

    Innehåller beräknad target_current, laddarbudget per fas,
    säkringsmarginal per fas samt metadata om beräkningen.
    """

    target_current: int
    """Beräknad laddström i ampere — avrundad nedåt, klämd till [min, max]."""

    charger_budget_l1: float
    """Laddarens budget på L1 (A): max_a - (phase_load - device_load) - margin.
    Kan vara negativ vid kapacitetsbrist. Används internt för target_current-beräkning."""

    charger_budget_l2: float
    """Laddarens budget på L2 (A): max_a - (phase_load - device_load) - margin.
    Kan vara negativ vid kapacitetsbrist. Används internt för target_current-beräkning."""

    charger_budget_l3: float
    """Laddarens budget på L3 (A): max_a - (phase_load - device_load) - margin.
    Kan vara negativ vid kapacitetsbrist. Används internt för target_current-beräkning."""

    available_min: float
    """Minsta tillgängliga ström bland aktiva faser (A). Baseras på charger_budget."""

    active_phases: list[int]
    """Lista med aktiva fasnummer, t.ex. [1, 2, 3] eller [2]."""

    phase_loads: list[float]
    """Hushållets fassensorvärden i konfigurationsordning (A)."""

    device_loads: list[float]
    """Laddarens egna uttag per fas i konfigurationsordning (A)."""

    charging_mode: str
    """Laddningsläge: '1-phase' eller '3-phase'."""

    fuse_headroom_l1: float = 0.0
    """Faktisk säkringsmarginal på L1 (A): max_a - phase_load - margin.
    Visas i Available L1-sensorn — visar hur mycket som faktiskt är kvar på säkringen."""

    fuse_headroom_l2: float = 0.0
    """Faktisk säkringsmarginal på L2 (A): max_a - phase_load - margin.
    Visas i Available L2-sensorn — visar hur mycket som faktiskt är kvar på säkringen."""

    fuse_headroom_l3: float = 0.0
    """Faktisk säkringsmarginal på L3 (A): max_a - phase_load - margin.
    Visas i Available L3-sensorn — visar hur mycket som faktiskt är kvar på säkringen."""

    fuse_headroom_min: float = 0.0
    """Minsta säkringsmarginal bland aktiva faser (A).
    Visas i Available MIN-sensorn — visar faktisk marginal, inte laddarens interna budget."""

    available_per_phase: dict[str, float] = field(default_factory=dict)
    """Laddarens budget per fas som dict: {"l1": float, "l2": float, "l3": float}.
    Innehåller charger_budget-värden (samma formel som charger_budget_lX).
    Används av PhaseSwitcher för att avgöra om fasväxling är möjlig.
    """

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

    Formel charger_budget per fas i (intern, driver target_current):
        charger_budget_i = max_ampere_i - (phase_value_i - device_value_i) - safety_margin

    Formel fuse_headroom per fas i (för sensordisplay):
        fuse_headroom_i = max_ampere_i - phase_value_i - safety_margin

    target_current = clamp(floor(min(charger_budget_i for i in active_phases)),
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

    # Beräkna charger_budget och fuse_headroom per fas (alla 3 — även inaktiva)
    charger_budget = [0.0, 0.0, 0.0]  # L1, L2, L3 (index 0-2)
    fuse_headroom = [0.0, 0.0, 0.0]  # L1, L2, L3 (index 0-2)
    for i in range(n):
        max_a = float(phases[i].get("max_ampere", 25))
        phase_load = phase_values[i] if i < len(phase_values) else 0.0
        device_load = device_values[i] if i < len(device_values) else 0.0
        # Intern budget: subtraherar laddarens eget uttag (oförändrad formel)
        charger_budget[i] = max_a - (phase_load - device_load) - safety_margin
        # Säkringsmarginal: visar vad som faktiskt är kvar på säkringen
        fuse_headroom[i] = max_a - phase_load - safety_margin

    # Samla charger_budget för aktiva faser (driver available_min och target_current)
    active_charger_budget: list[float] = []
    active_fuse_headroom: list[float] = []
    for phase_num in active_phase_numbers:
        idx = phase_num - 1  # fasnummer 1 → index 0
        if 0 <= idx < n:
            active_charger_budget.append(charger_budget[idx])
            active_fuse_headroom.append(fuse_headroom[idx])

    # Om inga aktiva faser — använd alla konfigurerade faser som fallback
    if not active_charger_budget:
        active_charger_budget = [charger_budget[i] for i in range(n)]
        active_fuse_headroom = [fuse_headroom[i] for i in range(n)]

    # Minsta tillgängliga ström bland aktiva faser (charger_budget — driver target_current)
    available_min = min(active_charger_budget)

    # Minsta säkringsmarginal bland aktiva faser (för Available MIN-sensor)
    fuse_headroom_min = min(active_fuse_headroom)

    # Beräkna target_current: floor, sedan kläm till [min_current, max_current]
    target = int(math.floor(available_min))
    target_current = max(min_current, min(max_current, target))

    # Avgör laddningsläge
    charging_mode = "3-phase" if len(active_phase_numbers) > 1 else "1-phase"

    return CalculationResult(
        target_current=target_current,
        charger_budget_l1=charger_budget[0],
        charger_budget_l2=charger_budget[1],
        charger_budget_l3=charger_budget[2],
        available_min=available_min,
        active_phases=list(active_phase_numbers),
        phase_loads=list(phase_values[:n]),
        device_loads=list(device_values[:n]),
        charging_mode=charging_mode,
        fuse_headroom_l1=fuse_headroom[0],
        fuse_headroom_l2=fuse_headroom[1],
        fuse_headroom_l3=fuse_headroom[2],
        fuse_headroom_min=fuse_headroom_min,
        # Alla 3 faser inkluderas alltid som metadata (även vid 1-fas-installation).
        # PhaseSwitcher använder alla faser för att bedöma fasväxlingsmöjligheter.
        # Konsumenter förlitar sig inte på antalet entries.
        available_per_phase={
            "l1": charger_budget[0],
            "l2": charger_budget[1],
            "l3": charger_budget[2],
        },
    )
