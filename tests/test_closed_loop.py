"""Closed-loop regulation tests (spec 010, sections 3 G-1..G-6 and 7).

The models in ``simulator.py`` play household, charger and meter. The real
coordinator runs against them inside Home Assistant's test harness, so the
production glue in sensor.py is what is tested, not a copy of it. Time is
frozen and advanced in steps of ``DT`` seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
    async_mock_service,
)

from custom_components.ev_load_balancer.const import (
    DOMAIN,
    PHASE_SWITCH_MIN_DWELL,
)
from custom_components.ev_load_balancer.phase_switcher import PhaseMode
from custom_components.ev_load_balancer.sensor import EVLoadBalancerCoordinator

from .simulator import Charger, Household, Meter, constant, pulses, step

DT = 0.1
CHARGER_SENSOR_INTERVAL_S = 1.0

PHASE_SENSORS = ["sensor.sim_grid_l1", "sensor.sim_grid_l2", "sensor.sim_grid_l3"]
CHARGER_ENTITIES = {
    "amp": "number.sim_charger_amp",
    "frc": "select.sim_charger_frc",
    "psm": "select.sim_charger_psm",
    "car_value": "sensor.sim_charger_car_value",
    "nrg_4": "sensor.sim_charger_current_l1",
    "nrg_5": "sensor.sim_charger_current_l2",
    "nrg_6": "sensor.sim_charger_current_l3",
    "map": "sensor.sim_charger_map",
}
CHARGER_CURRENT_SENSORS = [
    CHARGER_ENTITIES["nrg_4"],
    CHARGER_ENTITIES["nrg_5"],
    CHARGER_ENTITIES["nrg_6"],
]

# Spec 2.2: the measured installation's settings.
SETTINGS = {"max_ampere": 25, "safety_margin": 2, "min_current": 6, "max_current": 16}


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Closed loop",
        data={
            "profile_id": "goe_gemini",
            "charger_entities": CHARGER_ENTITIES,
            "phases": [
                {"sensor": sensor, "max_ampere": SETTINGS["max_ampere"], "label": f"L{i + 1}"}
                for i, sensor in enumerate(PHASE_SENSORS)
            ],
            "safety_margin": SETTINGS["safety_margin"],
            "min_current": SETTINGS["min_current"],
            "max_current": SETTINGS["max_current"],
            "phase_count": "auto",
        },
        options=options or {},
    )


@dataclass
class Trace:
    """What the balancer did during a run, with simulated time in seconds."""

    amps: list[tuple[float, int]] = field(default_factory=list)
    psm: list[tuple[float, str]] = field(default_factory=list)
    frc: list[tuple[float, str]] = field(default_factory=list)
    meter_reports: list[float] = field(default_factory=list)
    new_meter_values: list[float] = field(default_factory=list)

    @property
    def amp_values(self) -> list[int]:
        return [amp for _, amp in self.amps]

    @property
    def phase_switches(self) -> list[tuple[float, str]]:
        return [(t, v) for t, v in self.psm if v != "0"]

    def reductions(self, start_amp: int) -> list[tuple[float, int]]:
        previous = start_amp
        result = []
        for t, amp in self.amps:
            if amp < previous:
                result.append((t, amp))
            previous = amp
        return result

    def increases(self, start_amp: int) -> list[tuple[float, int]]:
        previous = start_amp
        result = []
        for t, amp in self.amps:
            if amp > previous:
                result.append((t, amp))
            previous = amp
        return result


class ClosedLoop:
    """Runs the coordinator against household, charger and meter models."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer,
        household: Household,
        charger: Charger,
        meter: Meter,
        entry: MockConfigEntry | None = None,
        reports_unchanged: bool = True,
    ) -> None:
        self.hass = hass
        self.reports_unchanged = reports_unchanged
        self.freezer = freezer
        self.household = household
        self.charger = charger
        self.meter = meter
        self.entry = entry or _entry()
        self.trace = Trace()
        self.now = 0.0
        self._steps = 0
        self._next_charger_sensor = 0.0
        self._car_value = ""
        self._deferred: list[tuple[str, str]] = []
        self._number_calls: list[ServiceCall] = []
        self._select_calls: list[ServiceCall] = []
        self._seen_number = 0
        self._seen_select = 0
        self.coordinator: EVLoadBalancerCoordinator | None = None

    def _total(self) -> list[float]:
        return [h + c for h, c in zip(self.household(self.now), self.charger.per_phase())]

    def _write_inputs(self) -> None:
        for sensor, text in self._deferred:
            self._write_phase(sensor, text)
        self._deferred = []
        if self.now + 1e-9 >= self._next_charger_sensor:
            for sensor, value in zip(CHARGER_CURRENT_SENSORS, self.charger.per_phase()):
                self.hass.states.async_set(sensor, f"{value:.2f}")
            self._next_charger_sensor += CHARGER_SENSOR_INTERVAL_S
        car_value = "Complete" if self.charger.interrupted else "Charging"
        if car_value != self._car_value:
            self._car_value = car_value
            self.hass.states.async_set(CHARGER_ENTITIES["car_value"], car_value)
        self.meter.sample(self.now, self._total())
        read_t = self.meter.last_read_t
        report = self.meter.report(self.now)
        if report is not None:
            self.trace.meter_reports.append(self.now)
            if self.meter.last_read_t != read_t:
                self.trace.new_meter_values.append(self.now)
            texts = [f"{value:.2f}" for value in report]
            self._write_phase(PHASE_SENSORS[0], texts[0])
            # L2 and L3 arrive one step later, within the 0.2 s of one sample (K-6).
            self._deferred = list(zip(PHASE_SENSORS[1:], texts[1:]))

    def _write_phase(self, sensor: str, text: str) -> None:
        current = self.hass.states.get(sensor)
        # P-3 false: an integration that only writes changed values.
        if self.reports_unchanged or current is None or current.state != text:
            self.hass.states.async_set(sensor, text)

    def _apply_commands(self) -> None:
        for call in self._number_calls[self._seen_number :]:
            amp = int(call.data["value"])
            self.charger.set_amp(amp)
            self.hass.states.async_set(CHARGER_ENTITIES["amp"], str(amp))
            self.trace.amps.append((round(self.now, 1), amp))
        self._seen_number = len(self._number_calls)
        for call in self._select_calls[self._seen_select :]:
            option = call.data["option"]
            if call.data["entity_id"] == CHARGER_ENTITIES["psm"]:
                self.charger.set_psm(self.now, option)
                self.trace.psm.append((round(self.now, 1), option))
            else:
                self.charger.set_frc(option)
                self.trace.frc.append((round(self.now, 1), option))
        self._seen_select = len(self._select_calls)

    async def start(self) -> None:
        self._number_calls = async_mock_service(self.hass, "number", "set_value")
        self._select_calls = async_mock_service(self.hass, "select", "select_option")
        self.hass.states.async_set(CHARGER_ENTITIES["map"], "[1, 2, 3]")
        self.hass.states.async_set(CHARGER_ENTITIES["amp"], str(self.charger.amp))
        # The meter has a value from before the run started.
        for sensor, value in zip(PHASE_SENSORS, self._total()):
            self.hass.states.async_set(sensor, f"{value:.2f}")
        self._write_inputs()
        self.coordinator = EVLoadBalancerCoordinator(self.hass, self.entry)
        # As in the measurement, the balancer is already balancing and the
        # charger charging at its current value when the run starts.
        state_machine = self.coordinator._state_machine
        state_machine.record_successful_calculation()
        state_machine.record_successful_calculation()
        state_machine.on_car_connected()
        await self.coordinator.async_setup()
        await self.hass.async_block_till_done(wait_background_tasks=True)
        if not self.charger.three_phase:
            # The 1-phase mode was set long enough ago for the dwell time (K-5) to be over.
            self.coordinator._phase_switcher.record_mode_change(
                PhaseMode.ONE_PHASE,
                dt_util.utcnow() - timedelta(seconds=PHASE_SWITCH_MIN_DWELL),
            )
        self._apply_commands()

    async def run_until(self, end: float) -> Trace:
        while self.now + 1e-9 < end:
            self._steps += 1
            self.now = self._steps * DT
            self.freezer.tick(DT)
            self.charger.advance(self.now, DT)
            self._write_inputs()
            # Let the listeners schedule their timers before firing the due ones.
            await self.hass.async_block_till_done(wait_background_tasks=True)
            # Exact: the plain helper adds 0.5 s and would fire the 0.2 s coalescing
            # timer (K-6) one step early.
            async_fire_time_changed_exact(self.hass, dt_util.utcnow())
            await self.hass.async_block_till_done(wait_background_tasks=True)
            self._apply_commands()
        return self.trace

    async def stop(self) -> None:
        await self.coordinator.async_shutdown()


async def _run(
    hass, freezer, household, charger, meter, duration, entry=None, reports_unchanged=True
) -> Trace:
    loop = ClosedLoop(hass, freezer, household, charger, meter, entry, reports_unchanged)
    await loop.start()
    try:
        return await loop.run_until(duration)
    finally:
        await loop.stop()


# Meter variants. T-8 / G-5: 2 s and 6 s delay, and a meter reporting every second.
METERS = {
    "delay_4s": {"delay_s": 4.0},
    "delay_2s": {"delay_s": 2.0},
    "delay_6s": {"delay_s": 6.0},
    "meter_1s": {"delay_s": 1.0, "telegram_s": 1.0, "poll_s": 1.0},
}

# Spec 2.2: calm household per phase during the measured 3-phase period
CALM = [8.0, 6.5, 9.2]


def _first_new_value_after(trace: Trace, meter: dict, at: float) -> float:
    """Arrival time of the first meter value sampled at or after ``at``."""
    return min(t for t in trace.new_meter_values if t - meter["delay_s"] >= at - 1e-6)


def _commands_per_meter_report(trace: Trace) -> int:
    """Highest number of amp/psm/frc commands between two meter reports (K-6)."""
    times = sorted(t for t, _ in trace.amps + trace.phase_switches + trace.frc)
    counts: dict[int, int] = {}
    for t in times:
        index = sum(1 for r in trace.meter_reports if r <= t + 1e-6)
        counts[index] = counts.get(index, 0) + 1
    return max(counts.values(), default=0)


# ---------------------------------------------------------------------------
# T-1 / G-1 (and T-8 / G-5): constant household
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("meter", METERS.values(), ids=METERS.keys())
async def test_t1_constant_household_stays_put(hass, freezer, meter):
    """T-1 / G-1: household 8/6.5/9.2 A, start at 16 A.

    At most two changes, ends at 13 A, never below 12 A, no phase switch.
    """
    trace = await _run(hass, freezer, constant(*CALM), Charger(amp=16), Meter(**meter), 600)

    assert len(trace.amps) <= 2, trace.amps
    assert trace.amp_values[-1] == 13, trace.amps
    assert min(trace.amp_values) >= 12, trace.amps
    assert trace.phase_switches == [], trace.psm


# ---------------------------------------------------------------------------
# T-2 / G-2 (and T-8 / G-5): household step +6 A on one phase
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("meter", METERS.values(), ids=METERS.keys())
async def test_t2_step_gives_one_reduction(hass, freezer, meter):
    """T-2 / G-2: +6 A on L2 at 16 A gives exactly one reduction, to 12 A, at the first report.

    Household 5/5/5 A allows 16 A; with 11 A on L2 the budget is 25 - 11 - 2 = 12 A.
    """
    step_at = 61.0
    trace = await _run(
        hass,
        freezer,
        step([5.0, 5.0, 5.0], phase=1, delta=6.0, start=step_at),
        Charger(amp=16),
        Meter(**meter),
        duration=300,
    )

    first_report = _first_new_value_after(trace, meter, step_at)
    reductions = trace.reductions(start_amp=16)
    assert len(reductions) == 1, trace.amps
    assert reductions[0][1] == 12, trace.amps
    assert first_report <= reductions[0][0] <= first_report + 0.5, (first_report, trace.amps)
    assert trace.increases(start_amp=16) == [], trace.amps
    assert trace.phase_switches == [], trace.psm


# ---------------------------------------------------------------------------
# T-3: step that goes away again
# ---------------------------------------------------------------------------


async def test_t3_step_back_raises_only_after_hold(hass, freezer):
    """T-3: +6 A for 20 s gives one reduction; the increase waits 90 s after the load is gone."""
    meter = METERS["delay_4s"]
    trace = await _run(
        hass,
        freezer,
        step([5.0, 5.0, 5.0], phase=1, delta=6.0, start=61.0, end=81.0),
        Charger(amp=16),
        Meter(**meter),
        duration=300,
    )

    gone_seen = _first_new_value_after(trace, meter, 81.0)
    reductions = trace.reductions(start_amp=16)
    increases = trace.increases(start_amp=16)
    assert len(reductions) == 1, trace.amps
    assert increases, trace.amps
    assert increases[0][0] >= gone_seen + 90, (gone_seen, trace.amps)
    assert trace.amp_values[-1] == 16, trace.amps


# ---------------------------------------------------------------------------
# T-4 / G-3 (and T-8 / G-5): heat pump pulses
# ---------------------------------------------------------------------------

# Spec 2.2: pulses of 2.9 A on L3, ca 30 s each, 10-70 s apart; G-3 uses 10-40 s off.
PULSE_OFFS = [10, 40, 25, 15, 35, 20, 30, 10, 40, 25, 15, 35]


@pytest.mark.parametrize("meter", METERS.values(), ids=METERS.keys())
async def test_t4_pulse_train_stays_on_lower_value(hass, freezer, meter):
    """T-4 / G-3: 2.9 A pulses on L3 for 10 min: at most one reduction and no increase.

    Calm L3 9.2 A gives 13 A; during a pulse 12.1 A gives 10 A.
    """
    load = pulses(CALM, phase=2, amplitude=2.9, start=30.0, on_s=30.0, offs=PULSE_OFFS)
    train_end = 30.0 + 30.0 * len(PULSE_OFFS) + sum(PULSE_OFFS)
    trace = await _run(hass, freezer, load, Charger(amp=13), Meter(**meter), train_end)

    assert len(trace.reductions(start_amp=13)) <= 1, trace.amps
    assert trace.increases(start_amp=13) == [], trace.amps
    assert trace.amp_values[-1:] in ([], [10]), trace.amps
    assert trace.phase_switches == [], trace.psm


# ---------------------------------------------------------------------------
# T-5 / G-4 (and T-8 / G-5): up-switch 1 -> 3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("meter", METERS.values(), ids=METERS.keys())
async def test_t5_upswitch_is_not_followed_by_downswitch(hass, freezer, meter):
    """T-5 / G-4: 1-phase at 13 A switches up; the 15 s interruption and 6 s ramp

    give no down-switch and no reduction.
    """
    trace = await _run(
        hass,
        freezer,
        constant(*CALM),
        Charger(amp=13, three_phase=False),
        Meter(**meter),
        duration=400,
    )

    assert [v for _, v in trace.phase_switches] == ["2"], trace.psm
    assert trace.reductions(start_amp=13) == [], trace.amps


# ---------------------------------------------------------------------------
# T-6: real shortage on one phase in 3-phase
# ---------------------------------------------------------------------------


async def test_t6_real_shortage_switches_down_once_then_dwells(hass, freezer):
    """T-6: L3 +8 A in 3-phase switches down at the first report; no new switch for 300 s.

    The extra load leaves after 60 s, so an up-switch is wanted from then on.
    """
    meter = METERS["delay_4s"]
    trace = await _run(
        hass,
        freezer,
        step(CALM, phase=2, delta=8.0, start=61.0, end=121.0),
        Charger(amp=13),
        Meter(**meter),
        duration=600,
    )

    first_report = _first_new_value_after(trace, meter, 61.0)
    switches = trace.phase_switches
    assert switches[0][1] == "1", trace.psm
    assert first_report <= switches[0][0] <= first_report + 0.5, (first_report, trace.psm)
    assert len(switches) == 2, trace.psm
    assert switches[1][1] == "2", trace.psm
    assert switches[1][0] - switches[0][0] >= PHASE_SWITCH_MIN_DWELL, trace.psm


# ---------------------------------------------------------------------------
# T-7: shortage during the dwell time
# ---------------------------------------------------------------------------


async def test_t7_shortage_during_dwell_reduces_then_pauses(hass, freezer):
    """T-7: after an up-switch, a shortage on L1 within 300 s gives no down-switch,

    but a reduction to 6 A at once and a pause 15 s later (existing logic).
    """
    meter = METERS["delay_4s"]
    loop = ClosedLoop(
        hass, freezer, constant(*CALM), Charger(amp=13, three_phase=False), Meter(**meter)
    )
    await loop.start()
    try:
        trace = await loop.run_until(150)
        assert [v for _, v in trace.phase_switches] == ["2"], trace.psm
        up_switch = trace.phase_switches[0][0]
        shortage_at = up_switch + 120
        loop.household = step(CALM, phase=0, delta=12.0, start=shortage_at)
        await loop.run_until(up_switch + 200)
    finally:
        await loop.stop()

    shortage_seen = _first_new_value_after(trace, meter, shortage_at)
    assert [v for _, v in trace.phase_switches] == ["2"], trace.psm
    reduction = [(t, a) for t, a in trace.amps if t >= shortage_seen]
    assert reduction[0] == (pytest.approx(shortage_seen, abs=0.5), 6), trace.amps
    pauses = [t for t, v in trace.frc if v == "1"]
    assert len(pauses) == 1, trace.frc
    assert 15 <= pauses[0] - shortage_seen <= 15 + meter["delay_s"] + 5.5, (shortage_seen, pauses)


# ---------------------------------------------------------------------------
# T-11 / K-6: one sample, one command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("meter", METERS.values(), ids=METERS.keys())
async def test_t11_one_command_per_sample(hass, freezer, meter):
    """T-11 / K-6: phase values 0.1 s apart are one sample and give one command.

    L1 +3 A alone would give 15 A, L2 +6 A gives 12 A: one command to 12 A, not two.
    """

    def load(t: float) -> list[float]:
        return [8.0, 11.0, 5.0] if t >= 61.0 else [5.0, 5.0, 5.0]

    trace = await _run(
        hass,
        freezer,
        load,
        Charger(amp=16),
        Meter(**meter),
        duration=120,
    )

    assert [a for _, a in trace.amps] == [12], trace.amps
    assert _commands_per_meter_report(trace) == 1, (trace.amps, trace.meter_reports)


# ---------------------------------------------------------------------------
# P-3 guard: phase sensor that only writes changed values
# ---------------------------------------------------------------------------


async def test_p3_constant_household_without_unchanged_reports(hass, freezer):
    """P-3 guard: with no state_reported events, T-1 still holds."""
    trace = await _run(
        hass,
        freezer,
        constant(*CALM),
        Charger(amp=16),
        Meter(**METERS["delay_4s"]),
        600,
        reports_unchanged=False,
    )

    assert len(trace.amps) <= 2, trace.amps
    assert trace.amp_values[-1] == 13, trace.amps
    assert trace.phase_switches == [], trace.psm


async def test_p3_step_without_unchanged_reports(hass, freezer):
    """P-3 guard: with no state_reported events, T-2 still holds."""
    trace = await _run(
        hass,
        freezer,
        step([5.0, 5.0, 5.0], phase=1, delta=6.0, start=61.0),
        Charger(amp=16),
        Meter(**METERS["delay_4s"]),
        300,
        reports_unchanged=False,
    )

    assert [a for _, a in trace.reductions(start_amp=16)] == [12], trace.amps
    assert trace.increases(start_amp=16) == [], trace.amps


# ---------------------------------------------------------------------------
# T-15: window shorter than the meter delay (P-1 guard)
# ---------------------------------------------------------------------------


async def test_t15_window_too_short_limits_error_to_one_step_per_sample(hass, freezer):
    """T-15 / P-1 guard: meter values 20 s old with a 15 s window.

    The charger value at the measuring moment can be missing from the window,
    so h may be wrong, but never by more than one step per meter value: no
    cascade of commands on one value, and no phase switch.
    """
    trace = await _run(
        hass,
        freezer,
        constant(*CALM),
        Charger(amp=16),
        Meter(delay_s=20.0),
        600,
    )

    assert _commands_per_meter_report(trace) <= 1, (trace.amps, trace.meter_reports)
    assert trace.phase_switches == [], trace.psm


# ---------------------------------------------------------------------------
# T-18: short window with a fast meter, no hold time
# ---------------------------------------------------------------------------

FAST = {"estimate_window_seconds": 5, "up_hold_seconds": 0}


async def test_t18_short_window_with_fast_meter_is_stable(hass, freezer):
    """T-18: window 5 s with a 1 s meter: T-1 still holds."""
    trace = await _run(
        hass,
        freezer,
        constant(*CALM),
        Charger(amp=16),
        Meter(**METERS["meter_1s"]),
        600,
        entry=_entry(FAST),
    )

    assert len(trace.amps) <= 2, trace.amps
    assert trace.amp_values[-1] == 13, trace.amps
    assert trace.phase_switches == [], trace.psm


async def test_t18_no_hold_time_raises_without_waiting(hass, freezer):
    """T-18: hold time 0 s: after a 20 s step the increase comes within the window and cooldown."""
    meter = METERS["meter_1s"]
    trace = await _run(
        hass,
        freezer,
        step([5.0, 5.0, 5.0], phase=1, delta=6.0, start=61.0, end=81.0),
        Charger(amp=16),
        Meter(**meter),
        200,
        entry=_entry(FAST),
    )

    gone_seen = _first_new_value_after(trace, meter, 81.0)
    increases = trace.increases(start_amp=16)
    assert len(trace.reductions(start_amp=16)) == 1, trace.amps
    assert increases, trace.amps
    assert increases[0][0] - gone_seen < 15, (gone_seen, trace.amps)
    assert trace.amp_values[-1] == 16, trace.amps
