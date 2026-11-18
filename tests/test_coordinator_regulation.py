"""Coordinator tests through the real listener path (spec 010, review round 13).

The coordinator is set up with its listeners and timers; phase sensors,
charger sensors and the charger's controls are plain Home Assistant states and
mocked services. The clock is frozen and moved explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.ev_load_balancer.const import DOMAIN
from custom_components.ev_load_balancer.sensor import EVLoadBalancerCoordinator

PHASE_SENSORS = ["sensor.rig_grid_l1", "sensor.rig_grid_l2", "sensor.rig_grid_l3"]
CHARGER_ENTITIES = {
    "amp": "number.rig_charger_amp",
    "frc": "select.rig_charger_frc",
    "psm": "select.rig_charger_psm",
    "car_value": "sensor.rig_charger_car_value",
    "nrg_4": "sensor.rig_charger_current_l1",
    "nrg_5": "sensor.rig_charger_current_l2",
    "nrg_6": "sensor.rig_charger_current_l3",
    "map": "sensor.rig_charger_map",
    "pha": "sensor.rig_charger_pha",
}
CHARGER_CURRENT_SENSORS = [CHARGER_ENTITIES[key] for key in ("nrg_4", "nrg_5", "nrg_6")]


def _entry(options: dict | None = None) -> MockConfigEntry:
    """25 A per phase, 2 A margin, 6-16 A: the measured installation (spec 2.2)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Rig",
        data={
            "profile_id": "goe_gemini",
            "charger_entities": CHARGER_ENTITIES,
            "phases": [
                {"sensor": sensor, "max_ampere": 25, "label": f"L{i + 1}"}
                for i, sensor in enumerate(PHASE_SENSORS)
            ],
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
            "phase_count": "auto",
        },
        options=options or {},
    )


class Rig:
    """A coordinator with its listeners, fed through Home Assistant states."""

    def __init__(self, hass: HomeAssistant, freezer, entry: MockConfigEntry | None = None):
        self.hass = hass
        self.freezer = freezer
        self.entry = entry or _entry()
        self.coordinator: EVLoadBalancerCoordinator | None = None
        self.calls: list[ServiceCall] = []
        self.call_times: list[datetime] = []

    @property
    def commands(self) -> list[tuple[str, str]]:
        """Every command sent, in order: ("amp", "12"), ("psm", "2"), ("frc", "1")."""
        result = []
        for call in self.calls:
            if call.domain == "number":
                result.append(("amp", str(int(call.data["value"]))))
            else:
                key = "psm" if call.data["entity_id"] == CHARGER_ENTITIES["psm"] else "frc"
                result.append((key, call.data["option"]))
        return result

    @property
    def amps(self) -> list[int]:
        """Every amp value sent, in order."""
        return [int(value) for key, value in self.commands if key == "amp"]

    @property
    def psm(self) -> list[str]:
        """Every psm option sent, in order."""
        return [value for key, value in self.commands if key == "psm"]

    @property
    def frc(self) -> list[str]:
        """Every frc option sent, in order."""
        return [value for key, value in self.commands if key == "frc"]

    def _mock_services(self) -> None:
        async def record(call: ServiceCall) -> None:
            self.calls.append(call)
            self.call_times.append(dt_util.utcnow())

        self.hass.services.async_register("number", "set_value", record)
        self.hass.services.async_register("select", "select_option", record)

    def report(self, values: list[float]) -> None:
        """The meter writes one value per phase (a new sample even if unchanged)."""
        for sensor, value in zip(PHASE_SENSORS, values):
            self.hass.states.async_set(sensor, f"{value:.2f}")

    def charger(self, values: list[float]) -> None:
        """The charger's current sensors write one value per phase."""
        for sensor, value in zip(CHARGER_CURRENT_SENSORS, values):
            self.hass.states.async_set(sensor, f"{value:.2f}")

    def car(self, value: str) -> None:
        self.hass.states.async_set(CHARGER_ENTITIES["car_value"], value)

    async def start(
        self,
        *,
        phases: list[float],
        charger: list[float],
        amp: str | None,
        car: str = "Charging",
        psm: str | None = None,
        pha: str | None = None,
        balancing: bool = True,
    ) -> EVLoadBalancerCoordinator:
        """Set the inputs, set up the coordinator and, if balancing, give it its first sample.

        ``amp`` is the state of the charger's amp entity (None: entity missing).
        """
        self._mock_services()
        self.hass.states.async_set(CHARGER_ENTITIES["map"], "[1, 2, 3]")
        if pha is not None:
            self.hass.states.async_set(CHARGER_ENTITIES["pha"], pha)
        if amp is not None:
            self.hass.states.async_set(CHARGER_ENTITIES["amp"], amp)
        if psm is not None:
            self.hass.states.async_set(CHARGER_ENTITIES["psm"], psm)
        self.car(car)
        self.charger(charger)
        self.report(phases)
        self.coordinator = EVLoadBalancerCoordinator(self.hass, self.entry)
        await self.coordinator.async_setup()
        await self.advance(1)
        if balancing:
            # The second successful calculation leaves INITIALIZING (state machine).
            self.report(phases)
            await self.advance(1)
        return self.coordinator

    async def advance(self, seconds: float, step: float = 0.1, settle: bool = True) -> None:
        """Move the frozen clock in steps and fire the timers that are due.

        Listeners for states written just before run first, as they would in
        Home Assistant before any time passes.
        """
        for _ in range(round(seconds / step)):
            if settle:
                await self.hass.async_block_till_done()
            else:
                await asyncio.sleep(0)
            self.freezer.tick(step)
            async_fire_time_changed_exact(self.hass, dt_util.utcnow())
            if settle:
                await self.hass.async_block_till_done()
            else:
                await asyncio.sleep(0)

    async def stop(self) -> None:
        await self.coordinator.async_shutdown()


# ---------------------------------------------------------------------------
# D-11 / R-2: no sample is dropped while a calculation waits for the charger
# ---------------------------------------------------------------------------


async def test_r2_report_during_slow_command_is_not_dropped(hass, freezer):
    """R-2 / D-11: a report that arrives while send_amp is in flight gets its own calculation.

    The second report proves more load than the first; its reduction is sent
    right after the first command returns.
    """
    rig = Rig(hass, freezer)
    coordinator = await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    release = asyncio.Event()
    sent: list[int] = []

    async def slow_send_amp(amp: int) -> bool:
        sent.append(amp)
        if len(sent) == 1:
            await release.wait()
        return True

    coordinator._dispatcher.send_amp = slow_send_amp
    try:
        # h = 23.5 - 15.5 = 8 A → budget 15 A
        rig.report([23.5] * 3)
        await rig.advance(0.5, settle=False)
        assert sent == [15]

        # h = 26.5 - 15.5 = 11 A → budget 12 A, while the first command is in flight
        rig.report([26.5] * 3)
        await rig.advance(0.5, settle=False)
        assert sent == [15]

        release.set()
        await rig.advance(0.1)
        assert sent == [15, 12]
    finally:
        release.set()
        await rig.stop()


async def test_shutdown_lets_calculation_in_flight_finish_without_rerun(hass, freezer):
    """13.3: shutdown waits for the calculation in flight and starts no new one,

    even when a report asked for one while it was running.
    """
    rig = Rig(hass, freezer)
    coordinator = await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    release = asyncio.Event()
    sent: list[int] = []

    async def slow_send_amp(amp: int) -> bool:
        sent.append(amp)
        await release.wait()
        return True

    coordinator._dispatcher.send_amp = slow_send_amp
    rig.report([23.5] * 3)
    await rig.advance(0.5, settle=False)
    rig.report([26.5] * 3)
    await rig.advance(0.5, settle=False)
    assert sent == [15]

    stopping = hass.async_create_task(rig.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await stopping
    await rig.advance(1)
    assert sent == [15]
    assert coordinator.last_sent_amp == 15


# ---------------------------------------------------------------------------
# D-12 / R-3: the 5 s clock
# ---------------------------------------------------------------------------


async def test_clock_pauses_15s_after_shortage_without_sensor_events(hass, freezer):
    """R-3 / D-12: the pause comes ca 15 s after the shortage, with no further reports.

    h = 34.5 - 15.5 = 19 A gives a budget of 4 A < 6 A: reduction to 6 A at once,
    then the existing pause logic, driven only by the clock.
    """
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        rig.report([34.5] * 3)
        shortage = dt_util.utcnow()
        await rig.advance(0.5)
        assert rig.amps == [6]

        pause_after = None
        for _ in range(300):
            await rig.advance(0.1)
            if rig.frc:
                pause_after = (dt_util.utcnow() - shortage).total_seconds()
                break
        assert rig.frc == ["1"]
        assert 15 <= pause_after <= 15 + 5 + 0.5, pause_after
    finally:
        await rig.stop()


async def test_clock_is_not_a_sample(hass, freezer):
    """D-12: clock recalculations alone never make an estimate unambiguous.

    The only report came before the history covered the window, so an increase
    must wait for the next report, however many clock ticks pass.
    """
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="10")
    try:
        await rig.advance(60)
        assert rig.amps == []

        rig.report([20.5] * 3)
        await rig.advance(0.5)
        assert rig.amps == [16]
    finally:
        await rig.stop()


async def test_clock_and_coalescing_end_on_unload(hass, freezer):
    """D-12 / 13.3: after shutdown no clock, no pending calculation, no command."""
    rig = Rig(hass, freezer)
    coordinator = await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    rig.report([34.5] * 3)
    await hass.async_block_till_done()
    assert coordinator._calc_timer is not None
    await rig.stop()

    assert coordinator._calc_timer is None
    assert coordinator._remove_listeners == []
    await rig.advance(30)
    assert rig.amps == []
    assert rig.frc == []


# ---------------------------------------------------------------------------
# D-10 / R-1, R-7: start state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amp", ["16", None, "unavailable"], ids=["amp_16", "missing", "unavailable"]
)
async def test_r1_fresh_start_reduces_at_first_sample(hass, freezer, amp):
    """R-1 / D-10 c: charger at 16 A, coordinator freshly started, target 10 A.

    The reduction is sent at the first sample. An unreadable amp entity is
    taken as max_current (16 A), which gives the same reduction.
    """
    rig = Rig(hass, freezer)
    # h = 28.5 - 15.5 = 13 A → budget 25 - 13 - 2 = 10 A
    await rig.start(phases=[28.5] * 3, charger=[15.5] * 3, amp=amp)
    try:
        assert rig.amps == [10]
    finally:
        await rig.stop()


async def test_reconnect_after_disconnect_parks_at_min_then_starts_at_once(hass, freezer):
    """D-10 a, b: unplugging sends amp = min; plugging in again sends the calculated value

    on the car_value event itself, without the 0.2 s coalescing or the 5 s cooldown,
    and without an frc pause. The car comes back within the window, so the
    hysteresis path alone would still refuse the increase (K-4).
    """
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        assert rig.amps == []
        rig.car("Idle")
        await hass.async_block_till_done()
        assert rig.amps == [6]

        rig.charger([0.0] * 3)
        rig.report([5.0] * 3)
        await rig.advance(5)
        rig.car("Charging")
        await hass.async_block_till_done()

        # h = 5 A → budget 18 A → 16 A
        assert rig.amps == [6, 16]
        assert rig.frc == []
    finally:
        await rig.stop()


async def test_r7_failsafe_reduce_then_recovery_raises_only_through_k4(hass, freezer):
    """R-7 / D-10 c: after a failsafe reduction the belief is the sent value.

    After recovery a lower target than the old 16 A is an increase from 6 A and
    must pass K-4; here the sample is not unambiguous, so nothing is sent.
    """
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        hass.states.async_set(PHASE_SENSORS[1], "unavailable")
        await rig.advance(0.5)
        assert rig.amps == [6]
        assert rig.coordinator.last_sent_amp == 6

        # The charger follows; its amp entity shows the sent value.
        hass.states.async_set(CHARGER_ENTITIES["amp"], "6")
        rig.charger([5.5] * 3)
        # h = 15.5 - 5.5 = 10 A → budget 13 A
        for _ in range(2):
            rig.report([15.5] * 3)
            await rig.advance(5)

        assert rig.coordinator.state.value == "balancing"
        assert rig.amps == [6]
    finally:
        await rig.stop()


# ---------------------------------------------------------------------------
# D-13 / R-4: setup or reload while a car is charging
# ---------------------------------------------------------------------------


async def _report_every_5s(rig: Rig, values: list[float], seconds: float) -> None:
    for _ in range(round(seconds / 5)):
        rig.report(values)
        await rig.advance(5)


async def test_reload_while_charging_keeps_psm_and_arms_dwell(hass, freezer):
    """R-4 / D-13: no psm at setup while charging; the mode comes from the psm entity

    (forced 1-phase here) and no phase switch follows within 300 s of the setup,
    although an up-switch is wanted all the time.
    """
    rig = Rig(hass, freezer)
    # 1-phase on L1 at 15.5 A; household 5 A per phase → room for 3-phase 16 A
    coordinator = await rig.start(
        phases=[20.5, 5.0, 5.0], charger=[15.5, 0.0, 0.0], amp="16", psm="1"
    )
    try:
        assert rig.psm == []
        assert coordinator.phase_switcher_mode == "one_phase"

        await _report_every_5s(rig, [20.5, 5.0, 5.0], 295)
        assert rig.psm == []

        await _report_every_5s(rig, [20.5, 5.0, 5.0], 75)
        assert rig.psm == ["2"]
    finally:
        await rig.stop()


async def test_unknown_car_status_at_setup_sends_no_psm(hass, freezer):
    """D-13 / A-10: car status that cannot be read counts as charging; psm is not touched."""
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16", car="unknown", balancing=False)
    try:
        assert rig.psm == []
    finally:
        await rig.stop()


async def test_setup_without_car_sends_psm_auto(hass, freezer):
    """D-13: with no car connected psm = auto is sent at setup, as before."""
    rig = Rig(hass, freezer)
    await rig.start(phases=[5.0] * 3, charger=[0.0] * 3, amp="6", car="Idle", balancing=False)
    try:
        assert rig.psm == ["0"]
    finally:
        await rig.stop()


# ---------------------------------------------------------------------------
# D-14 / R-5, R-6: up-switch and resume
# ---------------------------------------------------------------------------

PHA_L1_ONLY = "[true, false, false, true, true, true]"


async def test_r5_upswitch_reduces_to_l2_l3_budget_before_psm(hass, freezer):
    """R-5 / D-14: 1-phase at 16 A on L1, L2 budget 12 A: the up-switch first reduces

    to 12 A, computed over all configured phases, and only then sends psm.
    """
    rig = Rig(hass, freezer)
    # Household 5 / 11 / 5 A → budgets 18 / 12 / 18 A; charger on L1 only (pha)
    coordinator = await rig.start(
        phases=[20.5, 11.0, 5.0], charger=[15.5, 0.0, 0.0], amp="16", psm="1", pha=PHA_L1_ONLY
    )
    # The 1-phase mode is older than the dwell time (K-5).
    coordinator._phase_switcher.record_mode_change(
        coordinator.phase_switcher_mode, dt_util.utcnow() - timedelta(seconds=300)
    )
    try:
        await _report_every_5s(rig, [20.5, 11.0, 5.0], 70)
        assert rig.commands[:2] == [("amp", "12"), ("psm", "2")], rig.commands
    finally:
        await rig.stop()


async def test_r6_resume_is_capped_by_hold_time_ceiling(hass, freezer):
    """R-6 / D-14: resume after a pause goes no higher than the hold-time ceiling.

    Shortage (h = 19 A) → reduction to 6 A and pause. Then h = 12 A for 15 s and
    h = 7 A after that. The target is 16 A, but with h = 12 A inside the 90 s hold
    time the ceiling is 25 - 12 - 2 = 11 A, so the resume is to 11 A.
    """
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        rig.report([34.5] * 3)
        await rig.advance(0.5)
        assert rig.amps == [6]
        rig.charger([5.5] * 3)
        for _ in range(6):
            rig.report([24.5] * 3)
            await rig.advance(5)
            if rig.frc:
                break
        assert rig.frc == ["1"]

        rig.charger([0.0] * 3)
        await _report_every_5s(rig, [12.0] * 3, 15)
        for _ in range(60):
            rig.report([7.0] * 3)
            await rig.advance(5)
            if len(rig.frc) > 1:
                break
        assert rig.frc == ["1", "0"]
        assert rig.amps == [6, 11]
    finally:
        await rig.stop()


# ---------------------------------------------------------------------------
# D-16 / R-7, R-8: every command counts; substitutes and missing phases prove nothing
# ---------------------------------------------------------------------------


async def test_r8_unavailable_charger_sensor_gives_no_increase(hass, freezer):
    """R-8 / D-16: with the charger sensors unavailable the substitute (last sent amp)

    is constant, but it is no measurement: no increase however long it lasts.
    """
    rig = Rig(hass, freezer)
    for sensor in CHARGER_CURRENT_SENSORS:
        hass.states.async_set(sensor, "unavailable")
    # belief 10 A from the amp entity; h = 15 - 10 = 5 A → target 16 A
    await rig.start(phases=[15.0] * 3, charger=[], amp="10")
    for sensor in CHARGER_CURRENT_SENSORS:
        hass.states.async_set(sensor, "unavailable")
    try:
        await _report_every_5s(rig, [15.0] * 3, 60)
        assert rig.amps == []
    finally:
        await rig.stop()


async def test_r7_failsafe_command_counts_as_change_in_window(hass, freezer):
    """R-7 / D-16: the failsafe reduction is a change in the window, like any command.

    The car keeps drawing 4 A whatever is set, so the charger sensor never moves.
    After recovery the target 13 A is an increase from 6 A, and it waits until the
    failsafe command has left the 15 s window.
    """
    rig = Rig(hass, freezer)
    # h = 14 - 4 = 10 A → budget 13 A = the charger's setting
    await rig.start(phases=[14.0] * 3, charger=[4.0] * 3, amp="13")
    try:
        await _report_every_5s(rig, [14.0] * 3, 20)
        assert rig.amps == []
        hass.states.async_set(PHASE_SENSORS[1], "unavailable")
        await rig.advance(0.5)
        failsafe_at = dt_util.utcnow()
        assert rig.amps == [6]
        hass.states.async_set(CHARGER_ENTITIES["amp"], "6")

        rig.report([14.0] * 3)
        await rig.advance(5)
        await _report_every_5s(rig, [14.0] * 3, 5)
        assert rig.amps == [6]

        await _report_every_5s(rig, [14.0] * 3, 10)
        assert rig.amps == [6, 13]
        assert (dt_util.utcnow() - failsafe_at).total_seconds() >= 15
    finally:
        await rig.stop()


async def test_pause_and_phase_switch_count_as_changes_in_window(hass, freezer):
    """D-16: pause (frc) and psm are recorded in the window like amp commands."""
    rig = Rig(hass, freezer)
    coordinator = await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        rig.report([34.5] * 3)
        await rig.advance(0.5)
        rig.charger([5.5] * 3)
        for _ in range(6):
            rig.report([24.5] * 3)
            await rig.advance(5)
            if rig.frc:
                break
        assert rig.frc == ["1"]
        assert [0.0, 0.0, 0.0] in [values for _, values in coordinator._estimator._commands]
    finally:
        await rig.stop()

    rig = Rig(hass, freezer)
    coordinator = await rig.start(
        phases=[20.5, 11.0, 5.0], charger=[15.5, 0.0, 0.0], amp="16", psm="1", pha=PHA_L1_ONLY
    )
    coordinator._phase_switcher.record_mode_change(
        coordinator.phase_switcher_mode, dt_util.utcnow() - timedelta(seconds=300)
    )
    try:
        await _report_every_5s(rig, [20.5, 11.0, 5.0], 70)
        assert rig.psm == ["2"]
        commands = [values for _, values in coordinator._estimator._commands]
        assert [0.0, 0.0, 0.0] in commands
        assert [12.0, 12.0, 12.0] in commands
    finally:
        await rig.stop()


def test_no_active_configured_phase_is_not_unambiguous(hass):
    """D-16: active phases that map to no configured phase give no ceiling (no vacuous all())."""
    coordinator = EVLoadBalancerCoordinator(hass, _entry())
    assert coordinator._up_ceiling(dt_util.utcnow(), [5.0] * 3, [0.0] * 3, []) is None


# ---------------------------------------------------------------------------
# R-11: stored advanced settings are checked when read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "window", "hold"),
    [
        ({"estimate_window_seconds": "abc"}, 15, 90),
        ({"estimate_window_seconds": None, "up_hold_seconds": True}, 15, 90),
        ({"estimate_window_seconds": float("nan"), "up_hold_seconds": [90]}, 15, 90),
        ({"estimate_window_seconds": 100, "up_hold_seconds": 1000}, 30, 600),
        ({"estimate_window_seconds": 1, "up_hold_seconds": -5}, 3, 0),
        ({"estimate_window_seconds": 20.5, "up_hold_seconds": 45}, 20.5, 45),
    ],
    ids=["text", "none_and_bool", "nan_and_list", "above", "below", "valid"],
)
async def test_r11_stored_settings_fall_back_or_are_clamped(
    hass, freezer, caplog, options, window, hold
):
    """R-11 / 13.3: a hand-edited value never stops the integration.

    A wrong type gives the default, a value outside the range is clamped; each
    with one warning, and setup completes.
    """
    rig = Rig(hass, freezer, _entry(options))
    coordinator = await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        assert coordinator._estimator._window == timedelta(seconds=window)
        assert coordinator._estimator._hold == timedelta(seconds=hold)
        warnings = [
            r for r in caplog.records if r.levelname == "WARNING" and "_seconds" in r.getMessage()
        ]
        expected = sum(
            1
            for key, value in options.items()
            if value != {"estimate_window_seconds": window, "up_hold_seconds": hold}[key]
        )
        assert len(warnings) == expected, [r.getMessage() for r in warnings]
    finally:
        await rig.stop()


# ---------------------------------------------------------------------------
# K-7 log line and warnings on transition (13.3)
# ---------------------------------------------------------------------------

K7_FIELDS = re.compile(
    r"^Skattning (L[123]): nytt=(ja|nej) P=[\d.-]+A laddare_min=[\d.-]+A laddare_max=[\d.-]+A "
    r"låg=[\d.-]+A hög=[\d.-]+A h=[\d.-]+A entydig=(True|False) budget=[\d.-]+A$"
)


def _k7_lines(caplog) -> list[tuple[str, str]]:
    lines = []
    for record in caplog.records:
        match = K7_FIELDS.match(record.getMessage())
        if match:
            lines.append((match.group(1), match.group(2)))
    return lines


async def test_k7_log_line_shows_which_phases_got_a_new_sample(hass, freezer, caplog):
    """K-7 / 13.3: one DEBUG line per phase with P, charger min/max, low, high, h, budget,

    and whether the phase got a new sample in this calculation.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.ev_load_balancer.sensor")
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        caplog.clear()
        hass.states.async_set(PHASE_SENSORS[1], "21.50")
        await rig.advance(0.5)
        assert _k7_lines(caplog) == [("L1", "nej"), ("L2", "ja"), ("L3", "nej")]

        caplog.clear()
        await rig.advance(5)
        assert _k7_lines(caplog) == [("L1", "nej"), ("L2", "nej"), ("L3", "nej")]
    finally:
        await rig.stop()


async def test_unavailable_sensor_warns_on_transition_only(hass, freezer, caplog):
    """13.3: an unavailable charger sensor or an unknown phase sensor gives one WARNING

    when it goes away, not one per report or clock tick; a new one after it came back.
    """
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")

    def warnings(entity_id: str) -> int:
        return sum(
            1
            for r in caplog.records
            if r.levelname == "WARNING" and f"'{entity_id}'" in r.getMessage()
        )

    try:
        hass.states.async_set(CHARGER_CURRENT_SENSORS[0], "unavailable")
        await _report_every_5s(rig, [20.5] * 3, 30)
        assert warnings(CHARGER_CURRENT_SENSORS[0]) == 1

        rig.charger([15.5] * 3)
        await _report_every_5s(rig, [20.5] * 3, 10)
        hass.states.async_set(CHARGER_CURRENT_SENSORS[0], "unavailable")
        await _report_every_5s(rig, [20.5] * 3, 10)
        assert warnings(CHARGER_CURRENT_SENSORS[0]) == 2

        hass.states.async_set(PHASE_SENSORS[2], "unknown")
        await rig.advance(30)
        assert warnings(PHASE_SENSORS[2]) == 1
    finally:
        await rig.stop()


async def test_unload_before_home_assistant_started_sends_nothing(hass, freezer):
    """13.3: shutdown also drops the start callback; a coordinator unloaded before

    Home Assistant finished starting sends no psm when it does.
    """
    hass.set_state(CoreState.not_running)
    rig = Rig(hass, freezer)
    await rig.start(phases=[5.0] * 3, charger=[0.0] * 3, amp="6", car="Idle", balancing=False)
    await rig.stop()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert rig.commands == []


# ---------------------------------------------------------------------------
# T-10, T-12, T-13 through the listeners, with the clock moving
# ---------------------------------------------------------------------------


async def test_t10_charger_changes_without_phase_report_send_nothing(hass, freezer):
    """T-10 / F-2: the charger drops from 15.5 A to 5.5 A while the meter writes nothing

    new (P-3 false). For 40 s the clock recalculates; the old phase value is never
    compared with the newer charger values, so nothing is sent. The old formula
    would give h = 20.5 - 5.5 = 15 A and reduce to 8 A.
    """
    rig = Rig(hass, freezer)
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    try:
        for value in (12.0, 9.0, 6.0, 5.5):
            rig.charger([value] * 3)
            await rig.advance(1)
        await rig.advance(40)
        assert rig.commands == []
    finally:
        await rig.stop()


async def test_t12_increase_waits_until_history_covers_window(hass, freezer):
    """T-12: right after start an increase waits until the history covers the 15 s window.

    No command is sent before (belief 10 A read from the charger), so only the
    history can block; the reports are unambiguous from 15 s after setup.
    """
    rig = Rig(hass, freezer, _entry({"up_hold_seconds": 0}))
    setup_at = dt_util.utcnow()
    await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="10")
    try:
        for _ in range(30):
            rig.report([20.5] * 3)
            await rig.advance(1)
        assert rig.amps == [16]
        raised_at = (rig.call_times[0] - setup_at).total_seconds()
        # history starts with the first calculation, 0.2 s after setup; reports every 1 s
        assert 15.2 <= raised_at <= 15.2 + 1 + 0.2, raised_at
    finally:
        await rig.stop()


async def test_t13_sensor_loss_through_listener_failsafe_and_recovery(hass, freezer):
    """T-13: one phase sensor unavailable → failsafe reduce to 6 A through the listener;

    no calculation while in FAILSAFE (reports and clock ticks go on); recovery when
    the sensor returns, then calculations again.
    """
    rig = Rig(hass, freezer)
    coordinator = await rig.start(phases=[20.5] * 3, charger=[15.5] * 3, amp="16")
    calculate = coordinator._async_calculate
    calls: list[str] = []

    async def counting_calculate() -> None:
        calls.append(coordinator.state.value)
        await calculate()

    coordinator._async_calculate = counting_calculate
    try:
        hass.states.async_set(PHASE_SENSORS[1], "unavailable")
        await rig.advance(0.5)
        assert coordinator.state.value == "failsafe"
        assert rig.amps == [6]

        calls.clear()
        for car in ("Complete", "Charging"):
            rig.car(car)
            hass.states.async_set(
                CHARGER_ENTITIES["map"], "[1, 2]" if car == "Complete" else "[1, 2, 3]"
            )
            hass.states.async_set(PHASE_SENSORS[0], "20.50")
            hass.states.async_set(PHASE_SENSORS[2], "21.00")
            await rig.advance(5)
        assert calls == []

        rig.report([20.5] * 3)
        await rig.advance(0.5)
        assert coordinator.state.value == "balancing"
        assert calls == ["balancing"]
    finally:
        await rig.stop()
