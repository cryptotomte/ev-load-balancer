"""Closed-loop regulation tests (spec 010, sections 3 G-1..G-6 and 7).

The models in ``simulator.py`` play household, charger and meter. The real
coordinator runs against them inside Home Assistant's test harness, so the
production glue in sensor.py is what is tested, not a copy of it. Time is
frozen and advanced in steps of ``DT`` seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.ev_load_balancer.const import DOMAIN
from custom_components.ev_load_balancer.sensor import EVLoadBalancerCoordinator

from .simulator import Charger, Household, Meter, constant, step

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
    ) -> None:
        self.hass = hass
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
        self._number_calls: list[ServiceCall] = []
        self._select_calls: list[ServiceCall] = []
        self._seen_number = 0
        self._seen_select = 0
        self.coordinator: EVLoadBalancerCoordinator | None = None

    def _total(self) -> list[float]:
        return [h + c for h, c in zip(self.household(self.now), self.charger.per_phase())]

    def _write_inputs(self) -> None:
        if self.now + 1e-9 >= self._next_charger_sensor:
            for sensor, value in zip(CHARGER_CURRENT_SENSORS, self.charger.per_phase()):
                self.hass.states.async_set(sensor, f"{value:.2f}")
            self._next_charger_sensor += CHARGER_SENSOR_INTERVAL_S
        car_value = "Complete" if self.charger.interrupted else "Charging"
        if car_value != self._car_value:
            self._car_value = car_value
            self.hass.states.async_set(CHARGER_ENTITIES["car_value"], car_value)
        self.meter.sample(self.now, self._total())
        report = self.meter.report(self.now)
        if report is not None:
            self.trace.meter_reports.append(self.now)
            for sensor, value in zip(PHASE_SENSORS, report):
                self.hass.states.async_set(sensor, f"{value:.2f}")

    def _apply_commands(self) -> None:
        for call in self._number_calls[self._seen_number :]:
            amp = int(call.data["value"])
            self.charger.set_amp(amp)
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
        self.coordinator._last_sent_amp = self.charger.amp
        await self.coordinator.async_setup()
        await self.hass.async_block_till_done()
        self._apply_commands()

    async def run_until(self, end: float) -> Trace:
        while self.now + 1e-9 < end:
            self._steps += 1
            self.now = self._steps * DT
            self.freezer.tick(DT)
            self.charger.advance(self.now, DT)
            self._write_inputs()
            async_fire_time_changed(self.hass, dt_util.utcnow())
            await self.hass.async_block_till_done()
            self._apply_commands()
        return self.trace

    async def stop(self) -> None:
        await self.coordinator.async_shutdown()


async def _run(hass, freezer, household, charger, meter, duration, entry=None) -> Trace:
    loop = ClosedLoop(hass, freezer, household, charger, meter, entry)
    await loop.start()
    try:
        return await loop.run_until(duration)
    finally:
        await loop.stop()


# ---------------------------------------------------------------------------
# T-1 / G-1: constant household
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delay_s", [4.0])
async def test_t1_constant_household_stays_put(hass, freezer, delay_s):
    """T-1 / G-1: household 8/6.5/9.2 A, start at 16 A.

    At most two changes, ends at 13 A, never below 12 A, no phase switch.
    """
    trace = await _run(
        hass,
        freezer,
        constant(8.0, 6.5, 9.2),
        Charger(amp=16),
        Meter(delay_s=delay_s),
        duration=300,
    )

    assert len(trace.amps) <= 2, trace.amps
    assert trace.amp_values[-1] == 13, trace.amps
    assert min(trace.amp_values) >= 12, trace.amps
    assert trace.phase_switches == [], trace.psm


# ---------------------------------------------------------------------------
# T-2 / G-2: household step +6 A on one phase
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delay_s", [4.0])
async def test_t2_step_gives_one_reduction(hass, freezer, delay_s):
    """T-2 / G-2: +6 A on L2 at 16 A gives exactly one reduction, to 12 A, at the first report.

    Household 5/5/5 A allows 16 A; with 11 A on L2 the budget is 25 - 11 - 2 = 12 A.
    """
    step_at = 60.0
    trace = await _run(
        hass,
        freezer,
        step([5.0, 5.0, 5.0], phase=1, delta=6.0, start=step_at),
        Charger(amp=16),
        Meter(delay_s=delay_s),
        duration=240,
    )

    first_report_after_step = min(t for t in trace.meter_reports if t >= step_at + delay_s)
    reductions = trace.reductions(start_amp=16)
    assert len(reductions) == 1, trace.amps
    assert reductions[0][1] == 12, trace.amps
    assert first_report_after_step <= reductions[0][0] <= first_report_after_step + 1.0, (
        first_report_after_step,
        trace.amps,
    )
    assert trace.increases(start_amp=16) == [], trace.amps
    assert trace.phase_switches == [], trace.psm
