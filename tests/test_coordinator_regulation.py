"""Coordinator tests through the real listener path (spec 010, review round 13).

The coordinator is set up with its listeners and timers; phase sensors,
charger sensors and the charger's controls are plain Home Assistant states and
mocked services. The clock is frozen and moved explicitly.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
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
