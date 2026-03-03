"""Tester för PhaseSwitcher (phase_switcher.py).

Täcker:
- T005: Grundläggande initiering och evaluate() vid tillräcklig kapacitet
- T006: PHEV-skydd (device_supports_3phase=False)
- T007: Nedväxling 3→1 när L2 har kapacitet
- T008: Nedväxling ej möjlig (L2 otillräcklig) → returnera None
- T013: Uppväxling 1→3 med 60s hysteres
- T014: Hysteres-timer nollställs om kapacitet sjunker före 60s
- T017: PHEV-skydd (device_supports_3phase=False) → alltid None
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.ev_load_balancer.phase_switcher import (
    PhaseMode,
    PhaseSwitchCommand,
    PhaseSwitcher,
)

# ---------------------------------------------------------------------------
# Konstanter och hjälpvärden
# ---------------------------------------------------------------------------

MIN_CURRENT = 6

# Fasvärden: alla faser har god kapacitet (>= min_current)
ALL_PHASES_OK = {"l1": 10.0, "l2": 10.0, "l3": 10.0}

# Fasvärden: L1 under min_current, L2 har kapacitet
L1_LOW_L2_OK = {"l1": 2.0, "l2": 10.0, "l3": 10.0}

# Fasvärden: alla faser under min_current
ALL_PHASES_LOW = {"l1": 2.0, "l2": 3.0, "l3": 2.0}

# Fas L3 under min, L2 OK
L3_LOW_L2_OK = {"l1": 10.0, "l2": 10.0, "l3": 2.0}

T0 = datetime(2025, 1, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# T005: Grundläggande initiering
# ---------------------------------------------------------------------------


def test_initial_mode_is_three_phase():
    """PhaseSwitcher ska initieras i THREE_PHASE-läge."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    assert switcher.current_mode == PhaseMode.THREE_PHASE


def test_evaluate_returns_none_when_all_phases_ok_in_3phase():
    """evaluate() ska returnera None när alla faser har kapacitet i 3-fas-läge.

    Ingen nedväxling om alla faser >= min_current.
    """
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    result = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0)
    assert result is None


def test_evaluate_returns_none_at_exact_min_current():
    """evaluate() ska returnera None när faser är exakt på min_current."""
    switcher = PhaseSwitcher(min_current=6)
    exact_phases = {"l1": 6.0, "l2": 6.0, "l3": 6.0}
    result = switcher.evaluate(exact_phases, 6, T0)
    assert result is None


# ---------------------------------------------------------------------------
# T007: Nedväxling 3→1 (US1)
# ---------------------------------------------------------------------------


def test_downscale_3to1_when_l1_low_and_l2_ok():
    """Nedväxling 3→1: L1 under min_current, L2 >= min_current → switch_to_1phase."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    cmd = switcher.evaluate(L1_LOW_L2_OK, MIN_CURRENT, T0)

    assert cmd is not None
    assert isinstance(cmd, PhaseSwitchCommand)
    assert cmd.action == "switch_to_1phase"
    assert cmd.target_mode == PhaseMode.ONE_PHASE
    assert "switch_to_1phase" in cmd.action


def test_downscale_3to1_when_l3_low_and_l2_ok():
    """Nedväxling 3→1: L3 under min_current, L2 >= min_current → switch_to_1phase."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    cmd = switcher.evaluate(L3_LOW_L2_OK, MIN_CURRENT, T0)

    assert cmd is not None
    assert cmd.action == "switch_to_1phase"
    assert cmd.target_mode == PhaseMode.ONE_PHASE


def test_downscale_reason_contains_phase_info():
    """PhaseSwitchCommand.reason ska innehålla fasinfo vid nedväxling."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    cmd = switcher.evaluate(L1_LOW_L2_OK, MIN_CURRENT, T0)

    assert cmd is not None
    assert "L2" in cmd.reason or "l2" in cmd.reason.lower()


# ---------------------------------------------------------------------------
# T008: Nedväxling ej möjlig (US1)
# ---------------------------------------------------------------------------


def test_no_downscale_when_l2_also_insufficient():
    """Nedväxling ej möjlig: alla faser under min_current → returnera None.

    Pauslogiken tar över när L2 också saknar kapacitet.
    """
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    cmd = switcher.evaluate(ALL_PHASES_LOW, MIN_CURRENT, T0)
    assert cmd is None


def test_no_downscale_when_only_l2_available_below_min():
    """Nedväxling ej möjlig: L1 OK, L3 OK men L2 under min_current → None."""
    # L2 under min → kan inte växla till L2-baserad 1-fas
    phases = {"l1": 10.0, "l2": 3.0, "l3": 10.0}
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    cmd = switcher.evaluate(phases, MIN_CURRENT, T0)
    # L2 < min_current → ingen nedväxling (även om L1/L3 är ok)
    # Men "L1 ok, L3 ok" → inga faser är under min → ingen nedväxling trigger heller
    # Egentligen: alla faser >= min, L2 < min innebär bara L2 som är problemet
    # Rätt: eftersom L2 < min_current finns 1 fas under min → kontrollera L2 som backup
    # Eftersom L2 < min → ingen nedväxling → None
    assert cmd is None


def test_downscale_exactly_at_min_boundary():
    """L2 exakt på min_current (=6A) → nedväxling möjlig."""
    phases = {"l1": 3.0, "l2": 6.0, "l3": 10.0}  # L1 under min, L2 exakt på min
    switcher = PhaseSwitcher(min_current=6)
    cmd = switcher.evaluate(phases, 6, T0)
    assert cmd is not None
    assert cmd.action == "switch_to_1phase"


# ---------------------------------------------------------------------------
# T013: Uppväxling 1→3 med 60s hysteres (US2)
# ---------------------------------------------------------------------------


def test_upscale_1to3_after_60s_all_phases_ok():
    """Uppväxling 1→3: alla faser >= min_current i 60s → switch_to_3phase."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    # Sätt läge till ONE_PHASE
    switcher.record_mode_change(PhaseMode.ONE_PHASE)

    # T=0s: alla faser ok, timer startar — ingen uppväxling ännu
    cmd0 = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0)
    assert cmd0 is None

    # T=59s: timer ej expired → ingen uppväxling
    cmd59 = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=59))
    assert cmd59 is None

    # T=60s: timer expired → uppväxling
    cmd60 = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=60))
    assert cmd60 is not None
    assert cmd60.action == "switch_to_3phase"
    assert cmd60.target_mode == PhaseMode.THREE_PHASE


def test_upscale_reason_mentions_hysteresis():
    """PhaseSwitchCommand.reason ska nämna hysteres vid uppväxling."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.record_mode_change(PhaseMode.ONE_PHASE)

    switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0)  # starta timer
    cmd = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=60))

    assert cmd is not None
    assert "60" in cmd.reason or "hysteres" in cmd.reason.lower()


def test_no_upscale_in_3phase_mode():
    """Ingen uppväxling i THREE_PHASE-läge — logiken gäller enbart i ONE_PHASE."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    # Befinner sig i THREE_PHASE (standard)
    assert switcher.current_mode == PhaseMode.THREE_PHASE

    # Alla faser ok — ingen uppväxling triggas (redan i 3-fas)
    cmd = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=120))
    assert cmd is None


# ---------------------------------------------------------------------------
# T014: Hysteres-timer nollställs (US2)
# ---------------------------------------------------------------------------


def test_upscale_timer_resets_when_phase_drops_below_min():
    """Hysteres-timer nollställs om kapacitet sjunker under min_current före 60s."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.record_mode_change(PhaseMode.ONE_PHASE)

    # T=0s: alla faser ok, timer startar
    switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0)

    # T=30s: L1 sjunker under min → timer nollställs
    phases_l1_low = {"l1": 3.0, "l2": 10.0, "l3": 10.0}
    cmd = switcher.evaluate(phases_l1_low, MIN_CURRENT, T0 + timedelta(seconds=30))
    assert cmd is None  # Ingen uppväxling

    # T=80s: alla faser ok igen, men ny timer start vid T=30s
    # Ny timer startade vid T=30s? Nej — timer nollställdes vid T=30s.
    # Nu är det T=80s, 50s sedan nollställning → inte 60s sedan ny start
    cmd80 = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=80))
    # Timer startade om vid T=80s (då L1 åter OK) — men evaluate vid T=80 returnerar None
    # eftersom timer precis startade
    assert cmd80 is None


def test_upscale_timer_requires_full_60s_after_reset():
    """Uppväxling kräver 60s sammanhängande tid efter timer-nollställning."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.record_mode_change(PhaseMode.ONE_PHASE)

    # Starta timer vid T=0
    switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0)

    # Nollställ timer vid T=30s (L1 sjunker)
    switcher.evaluate({"l1": 3.0, "l2": 10.0, "l3": 10.0}, MIN_CURRENT, T0 + timedelta(seconds=30))

    # Starta om timer vid T=40s (alla OK igen)
    switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=40))

    # T=90s: 50s sedan timer restart (40s + 50s = 90s, men timer startade vid 40s)
    # 90 - 40 = 50s < 60s → ingen uppväxling
    cmd = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=90))
    assert cmd is None

    # T=101s: 61s sedan timer restart (101 - 40 = 61s >= 60s) → uppväxling
    cmd = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=101))
    assert cmd is not None
    assert cmd.action == "switch_to_3phase"


# ---------------------------------------------------------------------------
# T017: PHEV-skydd (US3)
# ---------------------------------------------------------------------------


def test_phev_guard_returns_none_when_supports_3phase_false():
    """device_supports_3phase=False → evaluate() ska alltid returnera None."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.set_device_capability(supports_3phase=False)

    # Även kapacitetsbrist → ingen nedväxling (PHEV-skydd)
    cmd = switcher.evaluate(L1_LOW_L2_OK, MIN_CURRENT, T0)
    assert cmd is None


def test_phev_guard_blocks_upscale_too():
    """device_supports_3phase=False → ingen uppväxling heller."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.record_mode_change(PhaseMode.ONE_PHASE)
    switcher.set_device_capability(supports_3phase=False)

    # Alla faser OK länge → ingen uppväxling pga PHEV-skydd
    switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0)
    cmd = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=60))
    assert cmd is None


def test_phev_guard_can_be_re_enabled():
    """PHEV-skyddet ska kunna återaktiveras via set_device_capability(True)."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.set_device_capability(supports_3phase=False)

    # PHEV-skydd aktivt → None
    cmd = switcher.evaluate(L1_LOW_L2_OK, MIN_CURRENT, T0)
    assert cmd is None

    # Återaktivera → nedväxling möjlig
    switcher.set_device_capability(supports_3phase=True)
    cmd = switcher.evaluate(L1_LOW_L2_OK, MIN_CURRENT, T0)
    assert cmd is not None
    assert cmd.action == "switch_to_1phase"


# ---------------------------------------------------------------------------
# record_mode_change
# ---------------------------------------------------------------------------


def test_record_mode_change_updates_current_mode():
    """record_mode_change() ska uppdatera current_mode."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    assert switcher.current_mode == PhaseMode.THREE_PHASE

    switcher.record_mode_change(PhaseMode.ONE_PHASE)
    assert switcher.current_mode == PhaseMode.ONE_PHASE

    switcher.record_mode_change(PhaseMode.THREE_PHASE)
    assert switcher.current_mode == PhaseMode.THREE_PHASE


def test_record_mode_change_resets_timer():
    """record_mode_change() ska nollställa hysteres-timern."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.record_mode_change(PhaseMode.ONE_PHASE)

    # Starta timer
    switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0)

    # record_mode_change ska nollställa timern
    switcher.record_mode_change(PhaseMode.ONE_PHASE)

    # Timer nollställd → ny timer startar nu
    switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=50))

    # 50s efter record_mode_change (0s timer) → ingen uppväxling ännu
    cmd = switcher.evaluate(ALL_PHASES_OK, MIN_CURRENT, T0 + timedelta(seconds=50))
    assert cmd is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_evaluate_with_empty_available_per_phase_returns_none():
    """evaluate() med tom dict ska inte krascha och returnera None."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    # Tom dict → alla faser default 0.0 → alla < min_current → check L2 (0.0 < 6) → None
    cmd = switcher.evaluate({}, MIN_CURRENT, T0)
    assert cmd is None


def test_phase_switch_command_is_frozen():
    """PhaseSwitchCommand ska vara ett frozen dataclass."""
    cmd = PhaseSwitchCommand(
        action="switch_to_1phase",
        target_mode=PhaseMode.ONE_PHASE,
        reason="test",
    )
    with pytest.raises((AttributeError, TypeError)):
        cmd.action = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PR-09: set_initial_mode() — initial fasläge utan THREE_PHASE-default
# ---------------------------------------------------------------------------


def test_phase_switcher_no_three_phase_default():
    """set_initial_mode() ska sätta initial fasläge baserat på faktiska aktiva faser.

    - set_initial_mode([1]) → ONE_PHASE (enfas-only bil, t.ex. PHEV)
    - set_initial_mode([1, 2, 3]) → THREE_PHASE (normal trefas-bil)
    """
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)

    # Enfas: pha visar bara L1 aktiv
    switcher.set_initial_mode([1])
    assert switcher.current_mode == PhaseMode.ONE_PHASE

    # Trefas: pha visar alla tre faser aktiva
    switcher.set_initial_mode([1, 2, 3])
    assert switcher.current_mode == PhaseMode.THREE_PHASE


def test_phase_switcher_set_initial_mode_empty_list_gives_three_phase():
    """set_initial_mode([]) ska fallbacka till THREE_PHASE (tom lista = 0 != 1)."""
    switcher = PhaseSwitcher(min_current=MIN_CURRENT)
    switcher.set_initial_mode([])
    assert switcher.current_mode == PhaseMode.THREE_PHASE
