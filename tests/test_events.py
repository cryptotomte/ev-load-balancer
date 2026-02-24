"""Tester för event-generering i EV Load Balancer (PR-07).

Täcker:
- US1: current_adjusted, device_paused, device_resumed events
- US3: sensor_lost, failsafe_activated events
- US4: phase_switched event
- US5: Loggningsnivåer
- Event-data: entity_id och timestamp validering
- Events skickas INTE vid dispatcher-failure
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_load_balancer.const import (
    DOMAIN,
    EVENT_CAPACITY_WARNING,
    EVENT_CURRENT_ADJUSTED,
    EVENT_DEVICE_PAUSED,
    EVENT_DEVICE_RESUMED,
    EVENT_FAILSAFE_ACTIVATED,
    EVENT_PHASE_SWITCHED,
    EVENT_SENSOR_LOST,
)
from custom_components.ev_load_balancer.sensor import EVLoadBalancerCoordinator
from custom_components.ev_load_balancer.state_machine import BalancerState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def full_config_entry():
    """MockConfigEntry med komplett konfiguration (3 faser)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer Test",
        data={
            "profile_id": "goe_gemini",
            "serial": "409787",
            "charger_entities": {
                "amp": "number.goe_409787_amp",
                "frc": "select.goe_409787_frc",
                "psm": "select.goe_409787_psm",
                "car_value": "sensor.goe_409787_car_value",
                "nrg_4": "sensor.goe_409787_nrg_4",
                "nrg_5": "sensor.goe_409787_nrg_5",
                "nrg_6": "sensor.goe_409787_nrg_6",
                "map": "sensor.goe_409787_map",
            },
            "phases": [
                {"sensor": "sensor.current_l1", "max_ampere": 25, "label": "L1"},
                {"sensor": "sensor.current_l2", "max_ampere": 25, "label": "L2"},
                {"sensor": "sensor.current_l3", "max_ampere": 25, "label": "L3"},
            ],
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
            "phase_count": "auto",
        },
    )


def _make_coordinator(entry: MockConfigEntry) -> EVLoadBalancerCoordinator:
    """Hjälpfunktion: skapa koordinator med mockad Debouncer och hass.bus."""
    mock_hass = MagicMock()
    mock_hass.states = MagicMock()
    mock_hass.states.get = MagicMock(return_value=None)
    mock_hass.bus = MagicMock()
    mock_hass.bus.async_fire = MagicMock()

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        return EVLoadBalancerCoordinator(mock_hass, entry)


def _set_balancing_state(coordinator: EVLoadBalancerCoordinator) -> None:
    """Hjälpfunktion: sätt koordinatorn i BALANCING-state."""
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert coordinator.state == BalancerState.BALANCING


def _setup_sensor_states(hass, l1: str = "15.0", l2: str = "15.0", l3: str = "15.0") -> None:
    """Hjälpfunktion: sätt upp standard sensorstates."""
    hass.states.async_set("sensor.current_l1", l1)
    hass.states.async_set("sensor.current_l2", l2)
    hass.states.async_set("sensor.current_l3", l3)
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")


# ---------------------------------------------------------------------------
# US1: current_adjusted event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_current_adjusted_fires_on_amp_change(hass, full_config_entry):
    """EVENT_CURRENT_ADJUSTED ska skickas vid strömsändning."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)

    # Sätt BALANCING + last_sent_amp = 14 → nedreglering
    _set_balancing_state(coordinator)
    coordinator._last_sent_amp = 14

    # Sensorvärden: available = 25 - 15 - 2 = 8A → target = 8A (nedreglering)
    _setup_sensor_states(hass)

    # Lyssna på events via hass.bus.async_listen
    fired_events = []

    def capture_event(event) -> None:
        fired_events.append((event.event_type, event.data))

    hass.bus.async_listen_once(EVENT_CURRENT_ADJUSTED, capture_event)
    hass.bus.async_listen_once(EVENT_DEVICE_PAUSED, capture_event)
    hass.bus.async_listen_once(EVENT_DEVICE_RESUMED, capture_event)

    await coordinator._async_calculate()
    # Ge HA event loop tid att bearbeta events
    await hass.async_block_till_done()

    # Verifiera att eventet skickades
    current_adjusted_events = [e for e in fired_events if e[0] == EVENT_CURRENT_ADJUSTED]
    assert len(current_adjusted_events) == 1

    event_data = current_adjusted_events[0][1]
    assert event_data["old_current"] == 14
    assert event_data["new_current"] == 8
    assert "reason" in event_data
    assert "phase_loads" in event_data
    assert "available" in event_data
    assert "timestamp" in event_data
    assert "entity_id" in event_data


@pytest.mark.asyncio
async def test_event_current_adjusted_not_fired_when_dispatcher_fails(hass, full_config_entry):
    """EVENT_CURRENT_ADJUSTED ska INTE skickas om dispatcher returnerar False."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Dispatcher returnerar False (misslyckad sändning)
    coordinator._dispatcher.send_amp = AsyncMock(return_value=False)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)

    _set_balancing_state(coordinator)
    coordinator._last_sent_amp = 14

    _setup_sensor_states(hass)

    # Mocka _fire_event för att verifiera att det INTE anropas med current_adjusted
    with patch.object(coordinator, "_fire_event", wraps=coordinator._fire_event) as mock_fire:
        await coordinator._async_calculate()

    # Verifiera att _fire_event INTE anropats med EVENT_CURRENT_ADJUSTED
    calls_for_current = [c for c in mock_fire.call_args_list if c.args[0] == EVENT_CURRENT_ADJUSTED]
    assert len(calls_for_current) == 0


# ---------------------------------------------------------------------------
# US1: device_paused event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_device_paused_fires_on_pause(hass, full_config_entry):
    """EVENT_DEVICE_PAUSED ska skickas när laddning pausas."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)

    _set_balancing_state(coordinator)

    # Sensorvärden som ger available < min (kapacitetsbrist)
    _setup_sensor_states(hass, l1="18.1", l2="18.1", l3="18.1")

    fired_events: list = []

    def capture_event(event) -> None:
        fired_events.append((event.event_type, event.data))

    hass.bus.async_listen_once(EVENT_DEVICE_PAUSED, capture_event)

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    # Sätt below_min_since 15s innan T0 så att timern är expired vid T0
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0,
    ):
        coordinator._hysteresis._below_min_since = T0 - timedelta(seconds=15)
        await coordinator._async_calculate()

    await hass.async_block_till_done()

    # Pausa event ska ha skickats
    paused_events = [e for e in fired_events if e[0] == EVENT_DEVICE_PAUSED]
    assert len(paused_events) == 1

    event_data = paused_events[0][1]
    assert "reason" in event_data
    assert "available_min" in event_data
    assert "min_current" in event_data
    assert "phase_loads" in event_data
    assert "timestamp" in event_data
    assert "entity_id" in event_data


@pytest.mark.asyncio
async def test_event_device_paused_not_fired_when_dispatcher_fails(hass, full_config_entry):
    """EVENT_DEVICE_PAUSED ska INTE skickas om dispatcher returnerar False."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=False)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)  # Misslyckad paus
    coordinator._dispatcher.resume = AsyncMock(return_value=False)

    _set_balancing_state(coordinator)
    _setup_sensor_states(hass, l1="18.1", l2="18.1", l3="18.1")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    with patch.object(coordinator, "_fire_event", wraps=coordinator._fire_event) as mock_fire:
        with patch(
            "custom_components.ev_load_balancer.sensor.utcnow",
            return_value=T0,
        ):
            coordinator._hysteresis._below_min_since = T0
            await coordinator._async_calculate()

    calls_for_paused = [c for c in mock_fire.call_args_list if c.args[0] == EVENT_DEVICE_PAUSED]
    assert len(calls_for_paused) == 0


# ---------------------------------------------------------------------------
# US1: device_resumed event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_device_resumed_fires_on_resume(hass, full_config_entry):
    """EVENT_DEVICE_RESUMED ska skickas när laddning återupptas."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    # Sätt PAUSED-state
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    coordinator._pause_reason = "insufficient_capacity"
    assert coordinator.state == BalancerState.PAUSED

    # Sensorvärden med gott om kapacitet för resume
    _setup_sensor_states(hass, l1="5.0", l2="5.0", l3="5.0")

    fired_events: list = []

    def capture_event(event) -> None:
        fired_events.append((event.event_type, event.data))

    hass.bus.async_listen_once(EVENT_DEVICE_RESUMED, capture_event)

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    # Sätt above_resume_since 30s innan T0 så att timern är expired vid T0
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0,
    ):
        coordinator._hysteresis._above_resume_since = T0 - timedelta(seconds=30)
        await coordinator._async_calculate()

    await hass.async_block_till_done()

    resumed_events = [e for e in fired_events if e[0] == EVENT_DEVICE_RESUMED]
    assert len(resumed_events) == 1

    event_data = resumed_events[0][1]
    assert "new_current" in event_data
    assert "available_per_phase" in event_data
    assert "timestamp" in event_data
    assert "entity_id" in event_data


@pytest.mark.asyncio
async def test_event_device_resumed_not_fired_when_dispatcher_fails(hass, full_config_entry):
    """EVENT_DEVICE_RESUMED ska INTE skickas om dispatcher returnerar False."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=False)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)  # Misslyckad resume

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    coordinator._pause_reason = "insufficient_capacity"

    _setup_sensor_states(hass, l1="5.0", l2="5.0", l3="5.0")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    with patch.object(coordinator, "_fire_event", wraps=coordinator._fire_event) as mock_fire:
        with patch(
            "custom_components.ev_load_balancer.sensor.utcnow",
            return_value=T0,
        ):
            coordinator._hysteresis._above_resume_since = T0
            await coordinator._async_calculate()

    calls_for_resumed = [c for c in mock_fire.call_args_list if c.args[0] == EVENT_DEVICE_RESUMED]
    assert len(calls_for_resumed) == 0


# ---------------------------------------------------------------------------
# Kapacitetsvarning: off→on, on→off, re-fire cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_warning_event_fires_on_transition_off_to_on(hass, full_config_entry):
    """EVENT_CAPACITY_WARNING ska skickas vid övergång off→on."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)
    coordinator._last_capacity_warning = False  # Startar utan varning

    _set_balancing_state(coordinator)

    # Sensorvärden som ger available_min < threshold (3A)
    # available = 25 - 20.1 - 2 = 2.9A < 3A → varning
    _setup_sensor_states(hass, l1="20.1", l2="20.1", l3="20.1")

    fired_events: list = []

    def capture_event(event) -> None:
        fired_events.append((event.event_type, event.data))

    hass.bus.async_listen_once(EVENT_CAPACITY_WARNING, capture_event)

    await coordinator._async_calculate()
    await hass.async_block_till_done()

    warning_events = [e for e in fired_events if e[0] == EVENT_CAPACITY_WARNING]
    assert len(warning_events) == 1, (
        f"Förväntat 1 capacity_warning event, fick {len(warning_events)}. "
        f"last_result={coordinator.last_result}"
    )
    assert warning_events[0][1]["active"] is True
    assert coordinator._last_capacity_warning is True


@pytest.mark.asyncio
async def test_capacity_warning_event_not_fired_when_already_on(hass, full_config_entry):
    """EVENT_CAPACITY_WARNING ska INTE skickas igen om varning redan är aktiv."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)
    coordinator._last_capacity_warning = True  # Varning redan aktiv

    _set_balancing_state(coordinator)

    # Kapacitetsbrist kvar
    _setup_sensor_states(hass, l1="20.1", l2="20.1", l3="20.1")

    with patch.object(coordinator, "_fire_event", wraps=coordinator._fire_event) as mock_fire:
        await coordinator._async_calculate()

    # Inget nytt event (varning redan aktiv)
    calls_for_warning = [c for c in mock_fire.call_args_list if c.args[0] == EVENT_CAPACITY_WARNING]
    assert len(calls_for_warning) == 0


@pytest.mark.asyncio
async def test_capacity_warning_re_fire_cycle(hass, full_config_entry):
    """EVENT_CAPACITY_WARNING ska skickas vid on→off→on-cykel.

    Cykeln: varning on → kapacitet återställd (no event) → varning on igen (event).
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)
    coordinator._last_capacity_warning = False

    _set_balancing_state(coordinator)

    fire_call_counts: dict = {EVENT_CAPACITY_WARNING: 0}
    original_fire = coordinator._fire_event

    def counting_fire(event_type: str, data: dict) -> None:
        if event_type in fire_call_counts:
            fire_call_counts[event_type] += 1
        original_fire(event_type, data)

    coordinator._fire_event = counting_fire  # type: ignore[method-assign]

    # Beräkning 1: kapacitetsbrist (off→on)
    _setup_sensor_states(hass, l1="20.1", l2="20.1", l3="20.1")
    await coordinator._async_calculate()
    assert coordinator._last_capacity_warning is True
    assert fire_call_counts[EVENT_CAPACITY_WARNING] == 1

    # Beräkning 2: kapacitet återställd (on→off, inget event)
    _setup_sensor_states(hass, l1="5.0", l2="5.0", l3="5.0")
    await coordinator._async_calculate()
    assert coordinator._last_capacity_warning is False
    assert fire_call_counts[EVENT_CAPACITY_WARNING] == 1  # Fortfarande 1

    # Beräkning 3: kapacitetsbrist igen (off→on, event skickas igen)
    _setup_sensor_states(hass, l1="20.1", l2="20.1", l3="20.1")
    await coordinator._async_calculate()
    assert coordinator._last_capacity_warning is True
    assert fire_call_counts[EVENT_CAPACITY_WARNING] == 2  # Nu 2


# ---------------------------------------------------------------------------
# US3: sensor_lost event
# ---------------------------------------------------------------------------


def test_event_sensor_lost_fires_via_hook(full_config_entry):
    """fire_sensor_lost_event ska skicka EVENT_SENSOR_LOST med korrekt data."""
    coordinator = _make_coordinator(full_config_entry)

    coordinator.fire_sensor_lost_event(
        sensor_entity="sensor.current_l1",
        action_taken="failsafe_reduce",
    )

    coordinator.hass.bus.async_fire.assert_called_once()
    call_args = coordinator.hass.bus.async_fire.call_args
    event_type = call_args[0][0]
    event_data = call_args[0][1]

    assert event_type == EVENT_SENSOR_LOST
    assert event_data["sensor_entity"] == "sensor.current_l1"
    assert event_data["action_taken"] == "failsafe_reduce"
    assert "timestamp" in event_data
    assert "entity_id" in event_data


def test_event_sensor_lost_logged_at_warning(full_config_entry, caplog):
    """fire_sensor_lost_event ska logga med WARNING-nivå."""
    coordinator = _make_coordinator(full_config_entry)

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_load_balancer.sensor"):
        coordinator.fire_sensor_lost_event(
            sensor_entity="sensor.current_l2",
            action_taken="pause_charging",
        )

    assert any("Sensorförlust" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# US3: failsafe_activated event
# ---------------------------------------------------------------------------


def test_event_failsafe_activated_fires_via_hook(full_config_entry):
    """fire_failsafe_activated_event ska skicka EVENT_FAILSAFE_ACTIVATED."""
    coordinator = _make_coordinator(full_config_entry)

    coordinator.fire_failsafe_activated_event(
        trigger="all_sensors_unavailable",
        action="total_pause",
        sensors_status={"sensor.l1": "unavailable", "sensor.l2": "unavailable"},
    )

    coordinator.hass.bus.async_fire.assert_called_once()
    call_args = coordinator.hass.bus.async_fire.call_args
    event_type = call_args[0][0]
    event_data = call_args[0][1]

    assert event_type == EVENT_FAILSAFE_ACTIVATED
    assert event_data["trigger"] == "all_sensors_unavailable"
    assert event_data["action"] == "total_pause"
    assert "sensors_status" in event_data
    assert "timestamp" in event_data
    assert "entity_id" in event_data


def test_event_failsafe_activated_logged_at_error(full_config_entry, caplog):
    """fire_failsafe_activated_event ska logga med ERROR-nivå."""
    coordinator = _make_coordinator(full_config_entry)

    with caplog.at_level(logging.ERROR, logger="custom_components.ev_load_balancer.sensor"):
        coordinator.fire_failsafe_activated_event(
            trigger="sensor_timeout",
            action="reduce_to_min",
            sensors_status={},
        )

    assert any("Failsafe" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# US4: phase_switched event
# ---------------------------------------------------------------------------


def test_event_phase_switched_fires_via_hook(full_config_entry):
    """fire_phase_switched_event ska skicka EVENT_PHASE_SWITCHED med korrekt data."""
    coordinator = _make_coordinator(full_config_entry)

    coordinator.fire_phase_switched_event(
        from_mode="3-phase",
        to_mode="1-phase",
        reason="low_load",
        available_per_phase=[18.0, 20.0, 17.0],
    )

    coordinator.hass.bus.async_fire.assert_called_once()
    call_args = coordinator.hass.bus.async_fire.call_args
    event_type = call_args[0][0]
    event_data = call_args[0][1]

    assert event_type == EVENT_PHASE_SWITCHED
    assert event_data["from_mode"] == "3-phase"
    assert event_data["to_mode"] == "1-phase"
    assert event_data["reason"] == "low_load"
    assert event_data["available_per_phase"] == [18.0, 20.0, 17.0]
    assert "timestamp" in event_data
    assert "entity_id" in event_data


def test_event_phase_switched_logged_at_info(full_config_entry, caplog):
    """fire_phase_switched_event ska logga med INFO-nivå."""
    coordinator = _make_coordinator(full_config_entry)

    with caplog.at_level(logging.INFO, logger="custom_components.ev_load_balancer.sensor"):
        coordinator.fire_phase_switched_event(
            from_mode="1-phase",
            to_mode="3-phase",
            reason="sufficient_load",
            available_per_phase=[10.0, 12.0, 11.0],
        )

    assert any("Fasväxling" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Event-data: entity_id och timestamp format
# ---------------------------------------------------------------------------


def test_fire_event_includes_entity_id(full_config_entry):
    """_fire_event ska inkludera entity_id i event-data."""
    coordinator = _make_coordinator(full_config_entry)

    coordinator._fire_event("test_event", {"key": "value"})

    call_args = coordinator.hass.bus.async_fire.call_args
    event_data = call_args[0][1]
    assert "entity_id" in event_data
    assert DOMAIN in event_data["entity_id"]


def test_fire_event_includes_timestamp(full_config_entry):
    """_fire_event ska inkludera ISO 8601-tidsstämpel i event-data."""
    coordinator = _make_coordinator(full_config_entry)

    coordinator._fire_event("test_event", {"key": "value"})

    call_args = coordinator.hass.bus.async_fire.call_args
    event_data = call_args[0][1]
    assert "timestamp" in event_data
    # Verifiera att det är ett giltigt ISO 8601-format
    timestamp = event_data["timestamp"]
    assert isinstance(timestamp, str)
    assert "T" in timestamp  # ISO 8601 innehåller "T" mellan datum och tid


def test_fire_event_merges_data(full_config_entry):
    """_fire_event ska slå ihop given data med entity_id och timestamp."""
    coordinator = _make_coordinator(full_config_entry)

    coordinator._fire_event("test_event", {"custom_key": "custom_value", "number": 42})

    call_args = coordinator.hass.bus.async_fire.call_args
    event_data = call_args[0][1]
    assert event_data["custom_key"] == "custom_value"
    assert event_data["number"] == 42
    assert "entity_id" in event_data
    assert "timestamp" in event_data


# ---------------------------------------------------------------------------
# US5: Loggningsnivåer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculation_cycle_logged_at_debug(hass, full_config_entry, caplog):
    """Normal beräkningscykel ska loggas med DEBUG-nivå."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)

    hass.states.async_set("sensor.current_l1", "10.0")
    hass.states.async_set("sensor.current_l2", "10.0")
    hass.states.async_set("sensor.current_l3", "10.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    with caplog.at_level(logging.DEBUG, logger="custom_components.ev_load_balancer.sensor"):
        await coordinator._async_calculate()

    # Ska finnas DEBUG-meddelanden om beräkning
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug_records) > 0


@pytest.mark.asyncio
async def test_capacity_warning_logged_at_warning(hass, full_config_entry, caplog):
    """Kapacitetsvarning ska loggas med WARNING-nivå."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=False)
    coordinator._dispatcher.resume = AsyncMock(return_value=False)
    coordinator._last_capacity_warning = False

    _set_balancing_state(coordinator)

    # Kapacitetsbrist
    _setup_sensor_states(hass, l1="20.1", l2="20.1", l3="20.1")

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_load_balancer.sensor"):
        await coordinator._async_calculate()

    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "Kapacitetsvarning" in r.message
    ]
    assert len(warning_records) >= 1
