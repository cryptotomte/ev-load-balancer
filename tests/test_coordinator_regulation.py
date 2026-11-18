"""Coordinator tests through the real listener path (spec 010, review round 13).

The coordinator is set up with its listeners and timers; phase sensors,
charger sensors and the charger's controls are plain Home Assistant states and
mocked services. The clock is frozen and moved explicitly.
"""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
    async_mock_service,
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
        self._number_calls: list[ServiceCall] = []
        self._select_calls: list[ServiceCall] = []

    @property
    def amps(self) -> list[int]:
        """Every amp value sent, in order."""
        return [int(call.data["value"]) for call in self._number_calls]

    @property
    def psm(self) -> list[str]:
        """Every psm option sent, in order."""
        return [
            call.data["option"]
            for call in self._select_calls
            if call.data["entity_id"] == CHARGER_ENTITIES["psm"]
        ]

    @property
    def frc(self) -> list[str]:
        """Every frc option sent, in order."""
        return [
            call.data["option"]
            for call in self._select_calls
            if call.data["entity_id"] == CHARGER_ENTITIES["frc"]
        ]

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
        balancing: bool = True,
    ) -> EVLoadBalancerCoordinator:
        """Set the inputs, set up the coordinator and, if balancing, give it its first sample.

        ``amp`` is the state of the charger's amp entity (None: entity missing).
        """
        self._number_calls = async_mock_service(self.hass, "number", "set_value")
        self._select_calls = async_mock_service(self.hass, "select", "select_option")
        self.hass.states.async_set(CHARGER_ENTITIES["map"], "[1, 2, 3]")
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
    coordinator._last_sent_amp = 16
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
