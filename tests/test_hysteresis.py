"""Tester för hysteresis.py.

Täcker:
- Omedelbar nedreglering utan timer
- Paus efter 15s under min_current (och timer-nollställning)
- Resume efter 30s över resume_threshold (och timer-nollställning)
- Uppreglering med 5s cooldown
- Deduplicering (target == last_sent → NONE)
- reset() nollställer alla timers
- resume_threshold = min_current + offset
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.ev_load_balancer.hysteresis import (
    HysteresisAction,
    HysteresisController,
)

# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

MIN_CURRENT = 6
RESUME_OFFSET = 2
PAUSE_DELAY = 15.0
RESUME_DELAY = 30.0
COOLDOWN = 5.0

T0 = datetime(2025, 1, 1, 12, 0, 0)


def make_controller() -> HysteresisController:
    """Skapa en HysteresisController med standardkonfiguration."""
    return HysteresisController(
        min_current=MIN_CURRENT,
        resume_threshold_offset=RESUME_OFFSET,
        pause_delay=PAUSE_DELAY,
        resume_delay=RESUME_DELAY,
        cooldown=COOLDOWN,
    )


# ---------------------------------------------------------------------------
# Test 1: Omedelbar nedreglering utan timer
# ---------------------------------------------------------------------------


def test_immediate_downregulation():
    """Nedreglering ska returnera SET_AMP omedelbart utan att vänta på timer."""
    ctrl = make_controller()
    # Bil laddas på 14A, kapacitet sjunker — calculator säger 10A
    cmd = ctrl.evaluate(
        available_min=10.0,
        target_current=10,
        last_sent_amp=14,
        is_paused=False,
        now=T0,
    )
    assert cmd.action == HysteresisAction.SET_AMP
    assert cmd.amp == 10


def test_downregulation_does_not_require_cooldown():
    """Nedreglering ska ske utan cooldown även direkt efter en amp-ändring."""
    ctrl = make_controller()
    # Registrera en amp-ändring precis nu
    ctrl.record_amp_change(T0)

    # Nedreglering 0.1s senare — ska ske trots att cooldown (5s) ej passerat
    cmd = ctrl.evaluate(
        available_min=8.0,
        target_current=10,
        last_sent_amp=14,
        is_paused=False,
        now=T0 + timedelta(seconds=0.1),
    )
    assert cmd.action == HysteresisAction.SET_AMP
    assert cmd.amp == 10


def test_downregulation_resets_pause_timer():
    """Nedreglering ska nollställa paus-timer.

    Obs: Nedreglering (target < last_sent) har prioritet 2, FÖRE kapacitetsbrist (prioritet 3).
    Därför startar paus-timern inte om target < last_sent. För att testa att nedreglering
    nollställer timern, starta timern manuellt och kör sedan en nedreglering.
    """
    ctrl = make_controller()

    # Starta paus-timer manuellt (simulerar att en tidigare beräkning startade den)
    ctrl._below_min_since = T0
    assert ctrl._below_min_since is not None

    # Nedreglering ska nollställa timern (target=10 < last_sent=14)
    ctrl.evaluate(
        available_min=4.0,
        target_current=10,
        last_sent_amp=14,
        is_paused=False,
        now=T0 + timedelta(seconds=5),
    )
    assert ctrl._below_min_since is None


# ---------------------------------------------------------------------------
# Test 2: Paus efter 15s under min_current + timer-nollställning
# ---------------------------------------------------------------------------


def test_no_pause_before_delay():
    """Ingen PAUSE ska skickas om available_min < min i under 15s."""
    ctrl = make_controller()

    # T=0: under min → starta timer
    cmd = ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0,
    )
    assert cmd.action == HysteresisAction.NONE

    # T=14.9s: fortfarande under min, men timer ej expired
    cmd = ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0 + timedelta(seconds=14.9),
    )
    assert cmd.action == HysteresisAction.NONE


def test_pause_after_delay():
    """PAUSE ska skickas efter 15s under min_current."""
    ctrl = make_controller()

    # T=0: starta timer
    ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0,
    )

    # T=15s: timer expired → PAUSE
    cmd = ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0 + timedelta(seconds=15),
    )
    assert cmd.action == HysteresisAction.PAUSE
    assert cmd.amp is None


def test_pause_timer_resets_when_capacity_returns():
    """Paus-timer ska nollställas om available_min stiger över min_current."""
    ctrl = make_controller()

    # T=0: under min → starta timer
    ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0,
    )
    assert ctrl._below_min_since is not None

    # T=10s: kapacitet återkommer → nollställ timer
    ctrl.evaluate(
        available_min=8.0,
        target_current=8,
        last_sent_amp=6,
        is_paused=False,
        now=T0 + timedelta(seconds=10),
    )
    assert ctrl._below_min_since is None

    # T=11s: åter under min → starta ny timer (inte fortsätta gammal)
    ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0 + timedelta(seconds=11),
    )

    # T=24s (11+13s): under min i 13s sedan ny timer — ingen paus ännu
    cmd = ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0 + timedelta(seconds=24),
    )
    assert cmd.action == HysteresisAction.NONE


# ---------------------------------------------------------------------------
# Test 3: Resume efter 30s över resume_threshold + timer-nollställning
# ---------------------------------------------------------------------------


def test_no_resume_before_delay():
    """Ingen RESUME ska skickas om available_min >= threshold i under 30s."""
    ctrl = make_controller()
    resume_threshold = MIN_CURRENT + RESUME_OFFSET  # 8A

    # T=0: PAUSED, kapacitet tillräcklig → starta timer
    cmd = ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=8,
        last_sent_amp=6,
        is_paused=True,
        now=T0,
    )
    assert cmd.action == HysteresisAction.NONE

    # T=29.9s: fortfarande under resume_delay
    cmd = ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=8,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=29.9),
    )
    assert cmd.action == HysteresisAction.NONE


def test_resume_after_delay():
    """RESUME ska skickas efter 30s över resume_threshold."""
    ctrl = make_controller()
    resume_threshold = MIN_CURRENT + RESUME_OFFSET  # 8A

    # T=0: PAUSED, starta timer
    ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0,
    )

    # T=30s: timer expired → RESUME
    cmd = ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=30),
    )
    assert cmd.action == HysteresisAction.RESUME
    assert cmd.amp == 10


def test_resume_timer_resets_when_capacity_drops():
    """Resume-timer ska nollställas om available_min sjunker under threshold."""
    ctrl = make_controller()
    resume_threshold = MIN_CURRENT + RESUME_OFFSET  # 8A

    # T=0: PAUSED, starta resume-timer
    ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0,
    )
    assert ctrl._above_resume_since is not None

    # T=15s: kapacitet sjunker → nollställ timer
    cmd = ctrl.evaluate(
        available_min=5.0,  # under threshold
        target_current=6,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=15),
    )
    assert cmd.action == HysteresisAction.NONE
    assert ctrl._above_resume_since is None

    # T=16s: kapacitet återkommer → starta ny timer
    ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=16),
    )

    # T=44s (16+28s): timer löpt 28s sedan restart → ingen resume ännu
    cmd = ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=44),
    )
    assert cmd.action == HysteresisAction.NONE


# ---------------------------------------------------------------------------
# Test 4: Uppreglering med 5s cooldown
# ---------------------------------------------------------------------------


def test_upregulation_without_previous_change():
    """Uppreglering utan tidigare amp-ändring ska skicka SET_AMP omedelbart."""
    ctrl = make_controller()
    # last_amp_change_time == None → ingen cooldown
    cmd = ctrl.evaluate(
        available_min=12.0,
        target_current=12,
        last_sent_amp=8,
        is_paused=False,
        now=T0,
    )
    assert cmd.action == HysteresisAction.SET_AMP
    assert cmd.amp == 12


def test_upregulation_blocked_during_cooldown():
    """Uppreglering ska blockeras under cooldown-perioden (< 5s)."""
    ctrl = make_controller()
    # Registrera amp-ändring precis nu
    ctrl.record_amp_change(T0)

    # 4.9s senare — fortfarande inom cooldown
    cmd = ctrl.evaluate(
        available_min=12.0,
        target_current=12,
        last_sent_amp=8,
        is_paused=False,
        now=T0 + timedelta(seconds=4.9),
    )
    assert cmd.action == HysteresisAction.NONE


def test_upregulation_allowed_after_cooldown():
    """Uppreglering ska tillåtas exakt vid eller efter 5s cooldown."""
    ctrl = make_controller()
    ctrl.record_amp_change(T0)

    # Exakt 5s senare — cooldown slut
    cmd = ctrl.evaluate(
        available_min=12.0,
        target_current=12,
        last_sent_amp=8,
        is_paused=False,
        now=T0 + timedelta(seconds=5),
    )
    assert cmd.action == HysteresisAction.SET_AMP
    assert cmd.amp == 12


# ---------------------------------------------------------------------------
# Test 5: Deduplicering (target == last_sent → NONE)
# ---------------------------------------------------------------------------


def test_no_command_when_target_equals_last_sent():
    """Ingen åtgärd ska skickas om target_current == last_sent_amp."""
    ctrl = make_controller()
    cmd = ctrl.evaluate(
        available_min=12.0,
        target_current=10,
        last_sent_amp=10,
        is_paused=False,
        now=T0,
    )
    assert cmd.action == HysteresisAction.NONE
    assert cmd.amp is None


def test_no_command_when_target_equals_last_sent_with_cooldown():
    """Oförändrat ström ska inte skickas även utan cooldown."""
    ctrl = make_controller()
    # Ingen tidigare amp-ändring (cooldown inte relevant)
    cmd = ctrl.evaluate(
        available_min=12.0,
        target_current=8,
        last_sent_amp=8,
        is_paused=False,
        now=T0,
    )
    assert cmd.action == HysteresisAction.NONE


# ---------------------------------------------------------------------------
# Test 6: reset() nollställer alla timers
# ---------------------------------------------------------------------------


def test_reset_clears_all_timers():
    """reset() ska nollställa alla interna timers."""
    ctrl = make_controller()

    # Starta paus-timer
    ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0,
    )
    # Registrera amp-ändring
    ctrl.record_amp_change(T0)

    # Starta resume-timer (simulera PAUSED-läge)
    ctrl.evaluate(
        available_min=float(MIN_CURRENT + RESUME_OFFSET),
        target_current=8,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=1),
    )

    assert (
        ctrl._below_min_since is not None
        or ctrl._above_resume_since is not None
        or ctrl._last_amp_change_time is not None
    )

    # reset() ska nollställa allt
    ctrl.reset()

    assert ctrl._below_min_since is None
    assert ctrl._above_resume_since is None
    assert ctrl._last_amp_change_time is None


def test_after_reset_upregulation_is_immediate():
    """Efter reset() ska uppreglering ske utan cooldown."""
    ctrl = make_controller()

    # Registrera amp-ändring (sätter cooldown-timer)
    ctrl.record_amp_change(T0)

    # reset() nollställer cooldown
    ctrl.reset()

    # Uppreglering ska nu ske omedelbart (inga 5s)
    cmd = ctrl.evaluate(
        available_min=12.0,
        target_current=12,
        last_sent_amp=8,
        is_paused=False,
        now=T0 + timedelta(seconds=0.1),
    )
    assert cmd.action == HysteresisAction.SET_AMP
    assert cmd.amp == 12


# ---------------------------------------------------------------------------
# Test 7: resume_threshold = min_current + offset
# ---------------------------------------------------------------------------


def test_resume_threshold_is_min_plus_offset():
    """resume_threshold ska vara min_current + resume_threshold_offset."""
    ctrl = make_controller()
    expected_threshold = MIN_CURRENT + RESUME_OFFSET  # 6 + 2 = 8
    assert ctrl._resume_threshold == expected_threshold


def test_resume_requires_above_threshold_not_min():
    """Resume-timer ska INTE starta om available_min bara är >= min_current men < threshold."""
    ctrl = make_controller()
    threshold = MIN_CURRENT + RESUME_OFFSET  # 8A

    # available_min = 7A (>= min=6A men < threshold=8A) → ingen resume-timer
    ctrl.evaluate(
        available_min=7.0,
        target_current=7,
        last_sent_amp=6,
        is_paused=True,
        now=T0,
    )
    assert ctrl._above_resume_since is None

    # available_min = 8A (== threshold) → starta resume-timer
    ctrl.evaluate(
        available_min=float(threshold),
        target_current=8,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=1),
    )
    assert ctrl._above_resume_since is not None


# ---------------------------------------------------------------------------
# Test 8: PAUSED-läge ignorerar nedreglering/uppreglering
# ---------------------------------------------------------------------------


def test_paused_mode_ignores_downregulation():
    """I PAUSED-läge ska ingen SET_AMP skickas för nedreglering."""
    ctrl = make_controller()

    # PAUSED med otillräcklig kapacitet → NONE (väntar på resume-threshold)
    cmd = ctrl.evaluate(
        available_min=3.0,  # under min och threshold
        target_current=6,
        last_sent_amp=14,  # target(6) < last_sent(14) men vi är PAUSED
        is_paused=True,
        now=T0,
    )
    # PAUSED-logik ska ignorera nedreglering
    assert cmd.action == HysteresisAction.NONE


def test_paused_mode_ignores_upregulation():
    """I PAUSED-läge ska ingen SET_AMP skickas för uppreglering."""
    ctrl = make_controller()

    cmd = ctrl.evaluate(
        available_min=3.0,
        target_current=10,
        last_sent_amp=6,  # target(10) > last_sent(6) men vi är PAUSED
        is_paused=True,
        now=T0,
    )
    assert cmd.action == HysteresisAction.NONE


# ---------------------------------------------------------------------------
# Test 9: Snabb pause→resume-cykel — resume-timer ska börja om
# ---------------------------------------------------------------------------


def test_rapid_pause_resume_cycle_requires_full_resume_delay():
    """Resume-timer ska starta om från noll efter PAUSE.

    Scenario:
    - T=0: under min → starta paus-timer
    - T=15s: paus-timer expired → PAUSE
    - T=15s (PAUSED): available_min stiger omedelbart över threshold → starta resume-timer
    - T=30s (PAUSED, 15s efter resume-timer start): resume-timer ej expired ännu (behöver 30s)
    - T=45s (PAUSED, 30s efter resume-timer start): RESUME
    """
    ctrl = make_controller()
    resume_threshold = MIN_CURRENT + RESUME_OFFSET  # 8A

    # T=0: under min → starta paus-timer
    ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0,
    )

    # T=15s: paus-timer expired → PAUSE
    cmd = ctrl.evaluate(
        available_min=4.0,
        target_current=6,
        last_sent_amp=6,
        is_paused=False,
        now=T0 + timedelta(seconds=15),
    )
    assert cmd.action == HysteresisAction.PAUSE

    # Nu är vi PAUSED (is_paused=True)
    # T=15s: available_min stiger omedelbart → starta resume-timer
    cmd = ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=15),
    )
    assert cmd.action == HysteresisAction.NONE  # Timer precis startad

    # T=30s: resume-timer har löpt 15s (behöver 30s) → fortfarande NONE
    cmd = ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=30),
    )
    assert cmd.action == HysteresisAction.NONE  # 15s av 30s

    # T=45s: resume-timer har löpt 30s → RESUME
    cmd = ctrl.evaluate(
        available_min=float(resume_threshold),
        target_current=10,
        last_sent_amp=6,
        is_paused=True,
        now=T0 + timedelta(seconds=45),
    )
    assert cmd.action == HysteresisAction.RESUME
    assert cmd.amp == 10


# ---------------------------------------------------------------------------
# Test 10: record_amp_change() sätts av koordinatorn, inte evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_does_not_set_last_amp_change_time():
    """evaluate() ska INTE uppdatera _last_amp_change_time — det är koordinatorns ansvar."""
    ctrl = make_controller()

    # SET_AMP returneras — men _last_amp_change_time ska fortfarande vara None
    cmd = ctrl.evaluate(
        available_min=12.0,
        target_current=12,
        last_sent_amp=8,
        is_paused=False,
        now=T0,
    )
    assert cmd.action == HysteresisAction.SET_AMP
    assert ctrl._last_amp_change_time is None  # koordinatorn ska sätta detta


def test_record_amp_change_sets_time():
    """record_amp_change() ska sätta _last_amp_change_time."""
    ctrl = make_controller()
    assert ctrl._last_amp_change_time is None

    ctrl.record_amp_change(T0)
    assert ctrl._last_amp_change_time == T0
