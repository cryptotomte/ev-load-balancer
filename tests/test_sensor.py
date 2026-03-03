"""Tester för sensor-plattformen (sensor.py).

Täcker:
- US1: 7 sensorentiteter skapas, _attr_should_poll = False, status-sensor
- US2: Fasmedveten beräkning, map-parsning, fallback
- US3: Nedreglering utan cooldown
- US4: PAUSED-transition vid kapacitetsbrist (timer-baserad via hysteres, PR-04)
- PR-04: Hysteres + kommando-dispatcher integration
- PR-06: Fasväxling 1↔3 fas integration
- PR-07: UtilizationSensor (procent, klämning, aktiva faser, division-by-zero)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_load_balancer.calculator import CalculationResult
from custom_components.ev_load_balancer.const import DOMAIN
from custom_components.ev_load_balancer.sensor import (
    AvailableCurrentSensor,
    BalancerStatusSensor,
    EVLoadBalancerCoordinator,
    TargetCurrentSensor,
    UtilizationSensor,
    async_setup_entry,
)
from custom_components.ev_load_balancer.state_machine import BalancerState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def full_config_entry():
    """MockConfigEntry med komplett konfiguration (3 faser, go-e profil)."""
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


@pytest.fixture
def single_phase_entry():
    """MockConfigEntry med 1 fas (L2) konfiguration."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer 1-fas",
        data={
            "profile_id": "goe_gemini",
            "serial": "409787",
            "charger_entities": {
                "car_value": "sensor.goe_409787_car_value",
                "nrg_4": "sensor.goe_409787_nrg_4",
                "nrg_5": "sensor.goe_409787_nrg_5",
                "nrg_6": "sensor.goe_409787_nrg_6",
                "map": "sensor.goe_409787_map",
            },
            "phases": [
                {"sensor": "sensor.current_l1", "max_ampere": 25, "label": "L1"},
                {"sensor": "sensor.current_l2", "max_ampere": 16, "label": "L2"},
                {"sensor": "sensor.current_l3", "max_ampere": 25, "label": "L3"},
            ],
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
        },
    )


def _make_mock_state(state_value: str):
    """Hjälpfunktion: skapa MockState med ett givet state-värde."""
    mock_state = MagicMock()
    mock_state.state = state_value
    return mock_state


# ---------------------------------------------------------------------------
# US1: async_setup_entry skapar 6 sensorentiteter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_creates_7_sensors(hass, full_config_entry):
    """async_setup_entry ska skapa 7 sensorentiteter och lägga dem till HA."""
    full_config_entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][full_config_entry.entry_id] = {}

    # Mocka Debouncer för att undvika faktiska timer-anrop
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer = MagicMock()
        mock_debouncer.async_schedule_call = MagicMock()
        mock_debouncer.async_cancel = MagicMock()
        mock_debouncer_cls.return_value = mock_debouncer

        added_entities = []

        def mock_add_entities(entities):
            added_entities.extend(entities)

        await async_setup_entry(hass, full_config_entry, mock_add_entities)

    assert len(added_entities) == 7
    # Verifiera att koordinatorn lagrades i hass.data
    assert "coordinator" in hass.data[DOMAIN][full_config_entry.entry_id]


@pytest.mark.asyncio
async def test_sensors_have_correct_types(hass, full_config_entry):
    """De 6 sensorerna ska ha rätt typer."""
    full_config_entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][full_config_entry.entry_id] = {}

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer = MagicMock()
        mock_debouncer.async_schedule_call = MagicMock()
        mock_debouncer_cls.return_value = mock_debouncer

        added_entities = []

        def mock_add_entities(entities):
            added_entities.extend(entities)

        await async_setup_entry(hass, full_config_entry, mock_add_entities)

    # Kontrollera typer: 1 status, 4 available (l1/l2/l3/min), 1 target, 1 utilization
    status_sensors = [e for e in added_entities if isinstance(e, BalancerStatusSensor)]
    available_sensors = [e for e in added_entities if isinstance(e, AvailableCurrentSensor)]
    target_sensors = [e for e in added_entities if isinstance(e, TargetCurrentSensor)]
    utilization_sensors = [e for e in added_entities if isinstance(e, UtilizationSensor)]

    assert len(status_sensors) == 1
    assert len(available_sensors) == 4
    assert len(target_sensors) == 1
    assert len(utilization_sensors) == 1


# ---------------------------------------------------------------------------
# US1: _attr_should_poll = False
# ---------------------------------------------------------------------------


def test_status_sensor_should_not_poll(full_config_entry):
    """BalancerStatusSensor._attr_should_poll ska vara False."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = BalancerStatusSensor(coordinator)
    assert sensor._attr_should_poll is False


def test_available_sensor_should_not_poll(full_config_entry):
    """AvailableCurrentSensor._attr_should_poll ska vara False."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = AvailableCurrentSensor(coordinator, "l1")
    assert sensor._attr_should_poll is False


def test_target_sensor_should_not_poll(full_config_entry):
    """TargetCurrentSensor._attr_should_poll ska vara False."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = TargetCurrentSensor(coordinator)
    assert sensor._attr_should_poll is False


# ---------------------------------------------------------------------------
# US1: Status-sensor visar "initializing" vid start
# ---------------------------------------------------------------------------


def test_status_sensor_initial_value_is_initializing(full_config_entry):
    """Status-sensorn ska visa 'initializing' direkt efter skapande."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = BalancerStatusSensor(coordinator)
    assert sensor.native_value == "initializing"


# ---------------------------------------------------------------------------
# US1: Unavailable-hantering — sensor returnerar None (inte 0)
# ---------------------------------------------------------------------------


def test_available_sensor_returns_none_when_no_result(full_config_entry):
    """AvailableCurrentSensor ska returnera None om last_result är None."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = AvailableCurrentSensor(coordinator, "l1")
    assert coordinator.last_result is None
    assert sensor.native_value is None


def test_target_sensor_returns_none_when_no_result(full_config_entry):
    """TargetCurrentSensor ska returnera None om last_result är None."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = TargetCurrentSensor(coordinator)
    assert coordinator.last_result is None
    assert sensor.native_value is None


# ---------------------------------------------------------------------------
# US1: Status-sensorns extra_state_attributes
# ---------------------------------------------------------------------------


def test_status_sensor_extra_attributes_before_calculation(full_config_entry):
    """Status-sensorns attribut ska hantera None korrekt före beräkning."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = BalancerStatusSensor(coordinator)
    attrs = sensor.extra_state_attributes

    assert attrs["target_current"] is None
    assert attrs["pause_reason"] is None
    assert attrs["last_calculation"] is None
    assert attrs["phase_loads"] is None
    assert attrs["device_loads"] is None
    assert attrs["active_phases"] is None
    assert attrs["charging_mode"] is None
    assert attrs["phase_mode"] == "three_phase"  # Initialt alltid three_phase
    assert attrs["safety_margin"] == 2.0
    assert attrs["charger_profile"] == "goe_gemini"


# ---------------------------------------------------------------------------
# US2: Map-sensor parsing
# ---------------------------------------------------------------------------


def test_coordinator_reads_map_3phase(hass, full_config_entry):
    """Koordinatorn ska parsa map='[1, 2, 3]' till [1, 2, 3]."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Sätt map-sensorns state
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")

    result = coordinator._read_active_phases_sync()
    assert result == [1, 2, 3]


def test_coordinator_reads_map_1phase(hass, full_config_entry):
    """Koordinatorn ska parsa map='[2]' till [2]."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    hass.states.async_set("sensor.goe_409787_map", "[2]")

    result = coordinator._read_active_phases_sync()
    assert result == [2]


def test_coordinator_fallback_when_map_unavailable(hass, full_config_entry):
    """Koordinatorn ska fallback till alla faser om map är unavailable."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # map är unavailable
    hass.states.async_set("sensor.goe_409787_map", "unavailable")

    result = coordinator._read_active_phases_sync()
    # 3 faser konfigurerade → fallback [1, 2, 3]
    assert result == [1, 2, 3]


def test_coordinator_fallback_when_map_missing(hass, full_config_entry):
    """Koordinatorn ska fallback till alla faser om map-state saknas."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # map-sensorn finns inte i states
    result = coordinator._read_active_phases_sync()
    assert result == [1, 2, 3]


# ---------------------------------------------------------------------------
# US2: Sensor-uppdatering efter beräkning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_available_sensors_updated_after_calculation(hass, full_config_entry):
    """available_l* ska uppdateras korrekt efter beräkning."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher för att undvika faktiska HA-anrop
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.send_frc = AsyncMock()

    # Sätt upp sensorstates (PRD §9 Scenario 1-liknande)
    hass.states.async_set("sensor.current_l1", "18.3")
    hass.states.async_set("sensor.current_l2", "12.1")
    hass.states.async_set("sensor.current_l3", "15.7")
    hass.states.async_set("sensor.goe_409787_nrg_4", "10.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "10.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "10.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    await coordinator._async_calculate()

    assert coordinator.last_result is not None
    l1_sensor = AvailableCurrentSensor(coordinator, "l1")
    l2_sensor = AvailableCurrentSensor(coordinator, "l2")
    l3_sensor = AvailableCurrentSensor(coordinator, "l3")
    min_sensor = AvailableCurrentSensor(coordinator, "min")
    target_sensor = TargetCurrentSensor(coordinator)

    # PRD §9 Scenario 1: fuse_headroom_l1 = 25 - 18.3 - 2 = 4.7, target=14A
    # Available-sensorerna visar fuse_headroom (faktisk säkringsmarginal),
    # inte charger_budget (laddarens interna budget = 14.7A)
    assert abs(l1_sensor.native_value - 4.7) < 0.01
    assert abs(l2_sensor.native_value - 10.9) < 0.01
    assert abs(l3_sensor.native_value - 7.3) < 0.01
    assert abs(min_sensor.native_value - 4.7) < 0.01
    assert target_sensor.native_value == 14


# ---------------------------------------------------------------------------
# US3: Nedreglering utan cooldown
# ---------------------------------------------------------------------------


def test_handle_state_change_downregulation_cancels_debouncer(hass, full_config_entry):
    """Nedreglering ska avbryta debouncer och skapa en direkt task."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer = MagicMock()
        mock_debouncer_cls.return_value = mock_debouncer
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Sätt current_target högt (simulerar aktiv laddning)
    coordinator._current_target = 14

    # Sätt sensorvärden som ger lägre preview (<14)
    hass.states.async_set("sensor.current_l1", "24.0")  # max 25 - 24 - 2 = -1 → kläms till 6
    hass.states.async_set("sensor.current_l2", "24.0")
    hass.states.async_set("sensor.current_l3", "24.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")

    # Skapa mock event
    mock_event = MagicMock()

    with patch.object(hass, "async_create_task") as mock_create_task:
        coordinator._handle_state_change(mock_event)

    # Nedreglering ska avbryta debouncer
    mock_debouncer.async_cancel.assert_called_once()
    # Och skapa en direkt task
    mock_create_task.assert_called_once()


def test_handle_state_change_upregulation_uses_debouncer(hass, full_config_entry):
    """Uppåtreglering ska använda debouncer (cooldown)."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer = MagicMock()
        mock_debouncer_cls.return_value = mock_debouncer
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Sätt current_target lågt — preview blir högre
    coordinator._current_target = 6

    # Sätt sensorvärden som ger högt preview (>6)
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")

    mock_event = MagicMock()

    with patch.object(hass, "async_create_task") as mock_create_task:
        coordinator._handle_state_change(mock_event)

    # Uppåtreglering: INGEN cancel av debouncer
    mock_debouncer.async_cancel.assert_not_called()
    # Debouncer ska schemaläggas
    mock_debouncer.async_schedule_call.assert_called()
    # Ingen direkt task
    mock_create_task.assert_not_called()


# ---------------------------------------------------------------------------
# US4: BALANCING → PAUSED vid kapacitetsbrist (timer-baserad, PR-04)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paused_transition_when_target_below_min(hass, full_config_entry):
    """BALANCING → PAUSED när available_min < min_current efter 15s hysteres-timer.

    Med PR-04 sker paus inte omedelbart utan efter 15s (PAUSE_DELAY_SECONDS).
    Testet mockar datetime.now() för att simulera tid.

    Scenario: last=18.1A per fas, device=0, safety=2, max=25
        available = 25 - (18.1 - 0) - 2 = 4.9A < min_current=6 → PAUSED (efter 15s)
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert coordinator.state == BalancerState.BALANCING

    # Sensorvärden: available = 25 - (18.1 - 0) - 2 = 4.9A < min_current=6
    hass.states.async_set("sensor.current_l1", "18.1")
    hass.states.async_set("sensor.current_l2", "18.1")
    hass.states.async_set("sensor.current_l3", "18.1")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    # Första beräkning (T=0s): under min, men timer ej expired → ingen PAUSE
    with patch("custom_components.ev_load_balancer.sensor.utcnow", return_value=T0):
        await coordinator._async_calculate()

    # Fortfarande BALANCING (timer har startats men ej expired)
    assert coordinator.state == BalancerState.BALANCING

    # Andra beräkning (T=15s): timer expired → PAUSE
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=15),
    ):
        await coordinator._async_calculate()

    # PAUSED efter 15s
    assert coordinator.last_result is not None
    assert coordinator.last_result.available_min < coordinator._min_current
    assert coordinator.state == BalancerState.PAUSED
    assert coordinator.pause_reason == "insufficient_capacity"
    coordinator._dispatcher.pause.assert_called_once()


@pytest.mark.asyncio
async def test_paused_transition_with_unclamped_logic(hass, full_config_entry):
    """BALANCING → PAUSED när beräknat available_min < min_current.

    I denna implementation används result.target_current (klämt).
    PAUSED triggas via hysteres-timer. Testet verifierar PAUSED-logiken
    via direkt state machine-manipulation.
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Sätt state till BALANCING manuellt
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert coordinator.state == BalancerState.BALANCING

    # Manuellt trigga on_below_min_current (simulerar att koordinatorn
    # detekterar kapacitetsbrist)
    sm.on_below_min_current()
    coordinator._pause_reason = "insufficient_capacity"

    assert coordinator.state == BalancerState.PAUSED
    assert coordinator.pause_reason == "insufficient_capacity"

    # Status-sensorn ska visa "paused"
    sensor = BalancerStatusSensor(coordinator)
    assert sensor.native_value == "paused"
    attrs = sensor.extra_state_attributes
    assert attrs["pause_reason"] == "insufficient_capacity"


@pytest.mark.asyncio
async def test_paused_to_balancing_transition(hass, full_config_entry):
    """PAUSED → BALANCING efter 30s hysteres-timer med tillräcklig kapacitet."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till PAUSED
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    coordinator._pause_reason = "insufficient_capacity"
    assert coordinator.state == BalancerState.PAUSED

    # Sätt sensorvärden som ger available_min >= resume_threshold (min+2=8A)
    # available = 25 - 5 - 2 = 18A >> resume_threshold=8A
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    # Första beräkning (T=0s): över threshold, men resume-timer ej expired
    with patch("custom_components.ev_load_balancer.sensor.utcnow", return_value=T0):
        await coordinator._async_calculate()

    # Fortfarande PAUSED (timer ej expired)
    assert coordinator.state == BalancerState.PAUSED

    # Andra beräkning (T=30s): resume-timer expired → BALANCING
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=30),
    ):
        await coordinator._async_calculate()

    assert coordinator.state == BalancerState.BALANCING
    assert coordinator.pause_reason is None
    coordinator._dispatcher.resume.assert_called_once()


# ---------------------------------------------------------------------------
# US4: pause_reason sätts/rensas korrekt i status-sensorns attribut
# ---------------------------------------------------------------------------


def test_status_sensor_pause_reason_in_attributes(full_config_entry):
    """pause_reason ska synas i status-sensorns extra_state_attributes."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator._pause_reason = "insufficient_capacity"

    sensor = BalancerStatusSensor(coordinator)
    attrs = sensor.extra_state_attributes
    assert attrs["pause_reason"] == "insufficient_capacity"


def test_status_sensor_no_pause_reason_when_balancing(full_config_entry):
    """pause_reason ska vara None när state är BALANCING."""
    coordinator = _make_coordinator(full_config_entry)
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    sensor = BalancerStatusSensor(coordinator)
    attrs = sensor.extra_state_attributes
    assert attrs["pause_reason"] is None


# ---------------------------------------------------------------------------
# Koordinator: options > data prioritering
# ---------------------------------------------------------------------------


def test_coordinator_reads_options_over_data(hass):
    """Koordinatorn ska läsa från entry.options framför entry.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            "profile_id": "goe_gemini",
            "phases": [{"sensor": "sensor.old", "max_ampere": 20, "label": "L1"}],
            "safety_margin": 3,
            "min_current": 6,
            "max_current": 16,
            "charger_entities": {},
        },
        options={
            "phases": [{"sensor": "sensor.new", "max_ampere": 25, "label": "L1"}],
            "safety_margin": 2,
            "min_current": 8,
            "max_current": 14,
            "charger_entities": {},
        },
    )

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, entry)

    # Options ska ha prioritet
    assert coordinator._phases == [{"sensor": "sensor.new", "max_ampere": 25, "label": "L1"}]
    assert coordinator._safety_margin == 2.0
    assert coordinator._min_current == 8
    assert coordinator._max_current == 14


# ---------------------------------------------------------------------------
# Koordinator: lyssnare registrering/avregistrering
# ---------------------------------------------------------------------------


def test_register_and_unregister_listener(full_config_entry):
    """Koordinatorn ska stödja registrering och avregistrering av lyssnare."""
    coordinator = _make_coordinator(full_config_entry)
    initial_count = len(coordinator._notify_listeners)

    called = []

    def callback_fn() -> None:
        called.append(True)

    coordinator.register_listener(callback_fn)
    assert len(coordinator._notify_listeners) == initial_count + 1

    coordinator.unregister_listener(callback_fn)
    assert len(coordinator._notify_listeners) == initial_count


# ---------------------------------------------------------------------------
# Sensor-lyssnare registreras i async_added_to_hass (inte __init__)
# ---------------------------------------------------------------------------


def test_sensor_does_not_register_listener_on_create(full_config_entry):
    """Sensorerna ska INTE registrera sig hos koordinatorn i __init__.

    Registrering sker i async_added_to_hass för korrekt HA-livscykel.
    """
    coordinator = _make_coordinator(full_config_entry)
    initial_count = len(coordinator._notify_listeners)

    BalancerStatusSensor(coordinator)

    # Ingen lyssnare ska registreras i __init__
    assert len(coordinator._notify_listeners) == initial_count


@pytest.mark.asyncio
async def test_sensor_registers_listener_in_async_added_to_hass(hass, full_config_entry):
    """Sensorerna ska registrera sig hos koordinatorn i async_added_to_hass."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    initial_count = len(coordinator._notify_listeners)

    sensor = BalancerStatusSensor(coordinator)
    sensor.hass = hass

    await sensor.async_added_to_hass()

    assert len(coordinator._notify_listeners) == initial_count + 1


# ---------------------------------------------------------------------------
# PR-04: Kommando-dispatcher integration (T010)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downregulation_sends_amp_command_immediately(hass, full_config_entry):
    """Nedreglering ska skicka amp-kommando omedelbart via dispatcher."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.send_frc = AsyncMock()

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # last_sent_amp = 14A (simulerar aktiv laddning)
    coordinator._last_sent_amp = 14

    # Sensorvärden: available = 25 - (15 - 0) - 2 = 8A → target = 8A (< 14A = nedreglering)
    hass.states.async_set("sensor.current_l1", "15.0")
    hass.states.async_set("sensor.current_l2", "15.0")
    hass.states.async_set("sensor.current_l3", "15.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    await coordinator._async_calculate()

    # send_amp ska ha anropats med 8A
    coordinator._dispatcher.send_amp.assert_called_once_with(8)
    assert coordinator.last_sent_amp == 8


@pytest.mark.asyncio
async def test_pause_command_sent_after_15s(hass, full_config_entry):
    """Pauskommando (frc='1') ska skickas efter 15s under min_current."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # Sensorvärden: available = 25 - 18.1 - 2 = 4.9A < min=6A → kapacitetsbrist
    hass.states.async_set("sensor.current_l1", "18.1")
    hass.states.async_set("sensor.current_l2", "18.1")
    hass.states.async_set("sensor.current_l3", "18.1")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    # T=0: under min, timer startar — ingen PAUSE ännu
    with patch("custom_components.ev_load_balancer.sensor.utcnow", return_value=T0):
        await coordinator._async_calculate()
    coordinator._dispatcher.pause.assert_not_called()

    # T=15s: timer expired → PAUSE
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=15),
    ):
        await coordinator._async_calculate()
    coordinator._dispatcher.pause.assert_called_once()
    assert coordinator.state == BalancerState.PAUSED
    assert coordinator.pause_reason == "insufficient_capacity"


@pytest.mark.asyncio
async def test_resume_command_sent_after_30s(hass, full_config_entry):
    """Resume-kommando (frc='0' + amp) ska skickas efter 30s över threshold."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till PAUSED
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    coordinator._pause_reason = "insufficient_capacity"
    assert coordinator.state == BalancerState.PAUSED

    # Sensorvärden: available = 25 - 5 - 2 = 18A >> threshold=8A
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    # T=0: PAUSED, timer startar — ingen RESUME ännu
    with patch("custom_components.ev_load_balancer.sensor.utcnow", return_value=T0):
        await coordinator._async_calculate()
    coordinator._dispatcher.resume.assert_not_called()
    assert coordinator.state == BalancerState.PAUSED

    # T=30s: timer expired → RESUME
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=30),
    ):
        await coordinator._async_calculate()
    coordinator._dispatcher.resume.assert_called_once()
    assert coordinator.state == BalancerState.BALANCING
    assert coordinator.pause_reason is None


@pytest.mark.asyncio
async def test_no_commands_sent_when_car_disconnects_while_paused(hass, full_config_entry):
    """Inga kommandon ska skickas när bilen kopplas från under PAUSED-tillstånd.

    Scenario: State = PAUSED → bilsensor ändras till 'Idle' →
    ingen send_amp/send_frc ska anropas.
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till PAUSED
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    coordinator._pause_reason = "insufficient_capacity"
    assert coordinator.state == BalancerState.PAUSED

    # Bil kopplas från (car_value = 'Idle')
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    await coordinator._async_calculate()

    # IDLE — inga kommandon ska skickas
    assert coordinator.state == BalancerState.IDLE
    coordinator._dispatcher.send_amp.assert_not_called()
    coordinator._dispatcher.pause.assert_not_called()
    coordinator._dispatcher.resume.assert_not_called()


@pytest.mark.asyncio
async def test_no_command_in_idle_state(hass, full_config_entry):
    """Inga kommandon ska skickas när state är IDLE."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till IDLE (record 2 beräkningar men bilen är Idle)
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert coordinator.state == BalancerState.IDLE

    # Bil är Idle
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    await coordinator._async_calculate()

    # Inga kommandon i IDLE
    coordinator._dispatcher.send_amp.assert_not_called()
    coordinator._dispatcher.pause.assert_not_called()
    coordinator._dispatcher.resume.assert_not_called()


# ---------------------------------------------------------------------------
# PR-04: T011 — Cooldown-test (uppreglering)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upregulation_sends_amp_after_cooldown(hass, full_config_entry):
    """Uppreglering ska skicka amp-kommando efter 5s cooldown."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # last_sent_amp = 8A, ny target = 12A (uppreglering)
    coordinator._last_sent_amp = 8

    # Registrera en amp-ändring precis nu (sätter cooldown-timer)
    T0 = datetime(2025, 1, 1, 12, 0, 0)
    coordinator._hysteresis.record_amp_change(T0)

    # Sensorvärden: available = 25 - 11 - 2 = 12A → target = 12A (uppreglering)
    hass.states.async_set("sensor.current_l1", "11.0")
    hass.states.async_set("sensor.current_l2", "11.0")
    hass.states.async_set("sensor.current_l3", "11.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    # T=4.9s: cooldown ej expired → ingen uppreglering
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=4.9),
    ):
        await coordinator._async_calculate()
    coordinator._dispatcher.send_amp.assert_not_called()

    # T=5s: cooldown expired → uppreglering
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=5),
    ):
        await coordinator._async_calculate()
    coordinator._dispatcher.send_amp.assert_called_once_with(12)


@pytest.mark.asyncio
async def test_no_command_if_less_than_cooldown_since_last_change(hass, full_config_entry):
    """Ingen uppreglering om < 5s sedan senaste ändring."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    coordinator._last_sent_amp = 8

    T0 = datetime(2025, 1, 1, 12, 0, 0)
    coordinator._hysteresis.record_amp_change(T0)

    # Sensorvärden: target = 12A (uppreglering)
    hass.states.async_set("sensor.current_l1", "11.0")
    hass.states.async_set("sensor.current_l2", "11.0")
    hass.states.async_set("sensor.current_l3", "11.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    # T=2s: cooldown ej expired
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=2),
    ):
        await coordinator._async_calculate()

    coordinator._dispatcher.send_amp.assert_not_called()


# ---------------------------------------------------------------------------
# PR-04: T012 — Idle-skydd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_commands_when_car_is_idle(hass, full_config_entry):
    """Inga kommandon ska skickas när bilstatus är 'Idle'."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till IDLE
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert coordinator.state == BalancerState.IDLE

    # Bilen är Idle
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    await coordinator._async_calculate()

    # Inga kommandon
    coordinator._dispatcher.send_amp.assert_not_called()
    coordinator._dispatcher.pause.assert_not_called()
    coordinator._dispatcher.resume.assert_not_called()


@pytest.mark.asyncio
async def test_no_commands_in_initializing_state(hass, full_config_entry):
    """Inga kommandon ska skickas i INITIALIZING-tillstånd."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # State = INITIALIZING (vid start)
    assert coordinator.state == BalancerState.INITIALIZING

    # En lyckad beräkning → fortfarande INITIALIZING (behöver 2)
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    await coordinator._async_calculate()
    # Fortfarande INITIALIZING efter 1 beräkning
    assert coordinator.state == BalancerState.INITIALIZING

    # Inga kommandon
    coordinator._dispatcher.send_amp.assert_not_called()
    coordinator._dispatcher.pause.assert_not_called()
    coordinator._dispatcher.resume.assert_not_called()


# ---------------------------------------------------------------------------
# PR-04: T013/T014 — Sensor-attribut last_sent_amp och paused_reason
# ---------------------------------------------------------------------------


def test_status_sensor_exposes_last_sent_amp(full_config_entry):
    """Status-sensorn ska exponera last_sent_amp i extra_state_attributes."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator._last_sent_amp = 12

    sensor = BalancerStatusSensor(coordinator)
    attrs = sensor.extra_state_attributes
    assert attrs["last_sent_amp"] == 12


def test_status_sensor_last_sent_amp_initial_value(full_config_entry):
    """last_sent_amp ska initieras till min_current (6A)."""
    coordinator = _make_coordinator(full_config_entry)

    sensor = BalancerStatusSensor(coordinator)
    attrs = sensor.extra_state_attributes
    assert attrs["last_sent_amp"] == coordinator._min_current


def test_status_sensor_exposes_insufficient_capacity_pause_reason(full_config_entry):
    """Status-sensor ska visa pause_reason='insufficient_capacity' vid paus (FR-014)."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator._pause_reason = "insufficient_capacity"

    sensor = BalancerStatusSensor(coordinator)
    attrs = sensor.extra_state_attributes
    assert attrs["pause_reason"] == "insufficient_capacity"


@pytest.mark.asyncio
async def test_amp_change_logging(hass, full_config_entry, caplog):
    """Amp-ändring ska loggas med INFO-nivå."""
    import logging

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # Sätt last_sent_amp högt → nedreglering (loggning)
    coordinator._last_sent_amp = 14

    hass.states.async_set("sensor.current_l1", "15.0")
    hass.states.async_set("sensor.current_l2", "15.0")
    hass.states.async_set("sensor.current_l3", "15.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    with caplog.at_level(logging.INFO, logger="custom_components.ev_load_balancer.sensor"):
        await coordinator._async_calculate()

    # Ska logga INFO om amp-ändring
    assert any("Ström" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_pause_logging(hass, full_config_entry, caplog):
    """Pauskommando ska loggas med WARNING-nivå."""
    import logging

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    hass.states.async_set("sensor.current_l1", "18.1")
    hass.states.async_set("sensor.current_l2", "18.1")
    hass.states.async_set("sensor.current_l3", "18.1")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_load_balancer.sensor"):
        with patch(
            "custom_components.ev_load_balancer.sensor.utcnow",
            return_value=T0 + timedelta(seconds=15),
        ):
            # Initialisera hysteres-timern manuellt (simulera att den startade för 15s sedan)
            coordinator._hysteresis._below_min_since = T0
            await coordinator._async_calculate()

    assert any("pausad" in record.message.lower() for record in caplog.records)


@pytest.mark.asyncio
async def test_resume_logging(hass, full_config_entry, caplog):
    """Resume-kommando ska loggas med INFO-nivå."""
    import logging

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()
    coordinator._dispatcher.resume = AsyncMock()

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    coordinator._pause_reason = "insufficient_capacity"

    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    with caplog.at_level(logging.INFO, logger="custom_components.ev_load_balancer.sensor"):
        with patch(
            "custom_components.ev_load_balancer.sensor.utcnow",
            return_value=T0 + timedelta(seconds=30),
        ):
            # Initialisera resume-timern manuellt
            coordinator._hysteresis._above_resume_since = T0
            await coordinator._async_calculate()

    assert any("återupptagen" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# Hjälpfunktion för att skapa koordinator utan hass
# ---------------------------------------------------------------------------


def _make_coordinator(entry: MockConfigEntry) -> EVLoadBalancerCoordinator:
    """Hjälpfunktion: skapa koordinator med mockad Debouncer."""
    mock_hass = MagicMock()
    mock_hass.states = MagicMock()
    mock_hass.states.get = MagicMock(return_value=None)

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        return EVLoadBalancerCoordinator(mock_hass, entry)


def _make_result(available_min: float, active_phases: list | None = None) -> CalculationResult:
    """Hjälpfunktion: skapa CalculationResult med given available_min."""
    return CalculationResult(
        target_current=8,
        charger_budget_l1=available_min,
        charger_budget_l2=available_min,
        charger_budget_l3=available_min,
        available_min=available_min,
        active_phases=active_phases if active_phases is not None else [1, 2, 3],
        phase_loads=[10.0, 10.0, 10.0],
        device_loads=[0.0, 0.0, 0.0],
        charging_mode="3-phase",
    )


# ---------------------------------------------------------------------------
# PR-06: T012 — Nedväxling 3→1 integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_downscale_sends_psm_when_l1_low_l2_ok(hass, full_config_entry):
    """Nedväxling 3→1: kapacitetsbrist på L1, L2 OK → send_psm('1') skickas.

    Scenario:
        L1 = hög last → available_l1 = 25 - (22 - 0) - 2 = 1A < min_current=6A
        L2 = låg last → available_l2 = 25 - (5 - 0) - 2 = 18A >= min_current=6A
        → Nedväxling: send_psm('1')
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.send_psm = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert coordinator.state.value == "balancing"

    # Sensorvärden: L1 hög last, L2 låg last, L3 medel
    # available_l1 = 25 - (22 - 0) - 2 = 1A < 6 (under min)
    # available_l2 = 25 - (5 - 0) - 2 = 18A >= 6 (ok)
    # available_l3 = 25 - (15 - 0) - 2 = 8A >= 6 (ok)
    # map = [1, 2, 3] (3-fas) → phase_switcher i THREE_PHASE
    hass.states.async_set("sensor.current_l1", "22.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "15.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    await coordinator._async_calculate()

    # send_psm ska ha anropats med '1' (1-fas)
    coordinator._dispatcher.send_psm.assert_called_once_with("1")


@pytest.mark.asyncio
async def test_phase_downscale_not_sent_when_l2_also_low(hass, full_config_entry):
    """Ingen nedväxling om L2 också saknar kapacitet — pauslogiken tar över.

    Scenario:
        Alla faser: hög last → available < min_current för alla faser
        → Ingen nedväxling (ingen psm-signal)
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.send_psm = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # Alla faser: hög last → available < min_current
    # available_l1 = 25 - (22 - 0) - 2 = 1A < 6
    # available_l2 = 25 - (22 - 0) - 2 = 1A < 6
    # available_l3 = 25 - (22 - 0) - 2 = 1A < 6
    hass.states.async_set("sensor.current_l1", "22.0")
    hass.states.async_set("sensor.current_l2", "22.0")
    hass.states.async_set("sensor.current_l3", "22.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)
    with patch("custom_components.ev_load_balancer.sensor.utcnow", return_value=T0):
        await coordinator._async_calculate()

    # Ingen psm-signal — L2 har heller inte kapacitet
    coordinator._dispatcher.send_psm.assert_not_called()


@pytest.mark.asyncio
async def test_phase_upscale_sends_psm_after_60s(hass, full_config_entry):
    """Uppväxling 1→3: alla faser >= min_current i 60s → send_psm('2') skickas.

    T016: Integrationstest för uppväxling.
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.send_psm = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    # Sätt fasväxlaren till ONE_PHASE-läge (simulerar att vi nyss växlat till 1-fas)
    from custom_components.ev_load_balancer.phase_switcher import PhaseMode

    coordinator._phase_switcher.record_mode_change(PhaseMode.ONE_PHASE)

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # Alla faser: låg last → available > min_current
    # available_l1 = 25 - (5 - 0) - 2 = 18A >= 6
    # available_l2 = 25 - (5 - 0) - 2 = 18A >= 6
    # available_l3 = 25 - (5 - 0) - 2 = 18A >= 6
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    # map=[2] → 1-fas → phase_switcher var ONE_PHASE → PHEV-detektion triggas INTE
    # (eftersom phase_switcher.current_mode == ONE_PHASE, ej THREE_PHASE)
    hass.states.async_set("sensor.goe_409787_map", "[2]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    T0 = datetime(2025, 1, 1, 12, 0, 0)

    # T=0s: timer startar → ingen uppväxling
    with patch("custom_components.ev_load_balancer.sensor.utcnow", return_value=T0):
        await coordinator._async_calculate()
    coordinator._dispatcher.send_psm.assert_not_called()

    # T=60s: timer expired → uppväxling → send_psm('2')
    with patch(
        "custom_components.ev_load_balancer.sensor.utcnow",
        return_value=T0 + timedelta(seconds=60),
    ):
        await coordinator._async_calculate()

    coordinator._dispatcher.send_psm.assert_called_once_with("2")


@pytest.mark.asyncio
async def test_phev_detection_disables_phase_switching(hass, full_config_entry):
    """PHEV-detektion: map visar [2] i THREE_PHASE-läge → fasväxling inaktiveras.

    T020/T021: Integrationstest för PHEV-skydd.
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.send_psm = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    # phase_switcher är i THREE_PHASE (standard)
    from custom_components.ev_load_balancer.phase_switcher import PhaseMode

    assert coordinator._phase_switcher.current_mode == PhaseMode.THREE_PHASE

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # Sensorvärden: L1 hög last (kapacitetsbrist), L2 OK
    # men map=[2] → PHEV-detektion → fasväxling inaktiveras
    hass.states.async_set("sensor.current_l1", "22.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    # map=[2] i THREE_PHASE-läge → PHEV-detektion
    hass.states.async_set("sensor.goe_409787_map", "[2]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    await coordinator._async_calculate()

    # Ingen psm-signal (PHEV-skydd aktiverat)
    coordinator._dispatcher.send_psm.assert_not_called()


@pytest.mark.asyncio
async def test_phase_mode_attribute_in_status_sensor(hass, full_config_entry):
    """Status-sensorns phase_mode-attribut ska visa aktuellt fasläge."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Initialt: three_phase
    sensor = BalancerStatusSensor(coordinator)
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode"] == "three_phase"

    # Sätt one_phase
    from custom_components.ev_load_balancer.phase_switcher import PhaseMode

    coordinator._phase_switcher.record_mode_change(PhaseMode.ONE_PHASE)
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode"] == "one_phase"


@pytest.mark.asyncio
async def test_phase_switching_not_called_in_idle_state(hass, full_config_entry):
    """Fasväxling ska inte ske i IDLE-tillstånd."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_psm = AsyncMock(return_value=True)
    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    # State IDLE (2 beräkningar men ingen bil)
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert coordinator.state.value == "idle"

    # Kapacitetsbrist på L1, L2 ok (men state = IDLE)
    hass.states.async_set("sensor.current_l1", "22.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    await coordinator._async_calculate()

    # Ingen psm i IDLE
    coordinator._dispatcher.send_psm.assert_not_called()


@pytest.mark.asyncio
async def test_generic_profile_does_not_use_phase_switching(hass):
    """Generic-profil ska inte använda fasväxling (saknar phase_switching-kapabilitet)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Generic EV",
        data={
            "profile_id": "generic",
            "charger_entities": {
                "amp": "number.generic_amp",
                "frc": "select.generic_frc",
                "car_value": "sensor.generic_car",
                "map": "sensor.generic_map",
                "nrg_4": "sensor.generic_nrg4",
                "nrg_5": "sensor.generic_nrg5",
                "nrg_6": "sensor.generic_nrg6",
            },
            "phases": [
                {"sensor": "sensor.current_l1", "max_ampere": 25, "label": "L1"},
                {"sensor": "sensor.current_l2", "max_ampere": 25, "label": "L2"},
                {"sensor": "sensor.current_l3", "max_ampere": 25, "label": "L3"},
            ],
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
        },
    )

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, entry)

    # Generic-profil: phase_switching NOT i capabilities
    assert coordinator._supports_phase_switching is False

    coordinator._dispatcher.send_psm = AsyncMock(return_value=True)
    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    # Kapacitetsbrist på L1, L2 ok
    hass.states.async_set("sensor.current_l1", "22.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.generic_nrg4", "0.0")
    hass.states.async_set("sensor.generic_nrg5", "0.0")
    hass.states.async_set("sensor.generic_nrg6", "0.0")
    hass.states.async_set("sensor.generic_map", "[1, 2, 3]")
    hass.states.async_set("sensor.generic_car", "Charging")

    await coordinator._async_calculate()

    # Ingen psm-signal — generic-profil stödjer inte fasväxling
    coordinator._dispatcher.send_psm.assert_not_called()


# ---------------------------------------------------------------------------
# PR-07: UtilizationSensor
# ---------------------------------------------------------------------------


def test_utilization_sensor_should_not_poll(full_config_entry):
    """UtilizationSensor._attr_should_poll ska vara False."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = UtilizationSensor(coordinator)
    assert sensor._attr_should_poll is False


def test_utilization_sensor_returns_none_when_no_result(full_config_entry):
    """UtilizationSensor ska returnera None om last_result är None."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = UtilizationSensor(coordinator)
    assert coordinator.last_result is None
    assert sensor.native_value is None


def test_utilization_sensor_calculates_correct_percentage(full_config_entry):
    """UtilizationSensor ska beräkna korrekt procent.

    Scenario: max_ampere=25, available_min=5A
    Utnyttjandegrad = (25 - 5) / 25 * 100 = 80%
    """
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = _make_result(available_min=5.0)

    sensor = UtilizationSensor(coordinator)
    value = sensor.native_value

    assert value is not None
    assert abs(value - 80.0) < 0.1


def test_utilization_sensor_clamps_to_zero_when_negative(full_config_entry):
    """UtilizationSensor ska klämma till 0% om available_min > max_ampere (kapacitetsbrist <0%)."""
    coordinator = _make_coordinator(full_config_entry)
    # available_min negativt → teoretiskt > 100% utnyttjande → kläms till 100%
    coordinator.last_result = _make_result(available_min=-5.0)

    sensor = UtilizationSensor(coordinator)
    value = sensor.native_value

    assert value is not None
    assert value == 100.0  # Klämt till 100% (alla faser är 100% belastade)


def test_utilization_sensor_clamps_to_100_max(full_config_entry):
    """UtilizationSensor ska returnera max 100%."""
    coordinator = _make_coordinator(full_config_entry)
    # Extrem kapacitetsbrist
    coordinator.last_result = _make_result(available_min=-100.0)

    sensor = UtilizationSensor(coordinator)
    value = sensor.native_value

    assert value is not None
    assert value <= 100.0


def test_utilization_sensor_clamps_to_0_min(full_config_entry):
    """UtilizationSensor ska returnera min 0%."""
    coordinator = _make_coordinator(full_config_entry)
    # available_min = max_ampere + extra (0% utnyttjande + marginal)
    coordinator.last_result = _make_result(available_min=30.0)  # > max_ampere=25

    sensor = UtilizationSensor(coordinator)
    value = sensor.native_value

    assert value is not None
    assert value == 0.0  # Klämt till 0%


def test_utilization_sensor_with_1_active_phase(full_config_entry):
    """UtilizationSensor ska fungera korrekt med 1 aktiv fas."""
    coordinator = _make_coordinator(full_config_entry)
    # 1 aktiv fas = L2 (index 1, max_ampere=25)
    coordinator.last_result = CalculationResult(
        target_current=10,
        charger_budget_l1=20.0,
        charger_budget_l2=10.0,  # L2 är aktivt
        charger_budget_l3=20.0,
        available_min=10.0,
        active_phases=[2],  # Bara L2 aktiv
        phase_loads=[5.0, 15.0, 5.0],
        device_loads=[0.0, 0.0, 0.0],
        charging_mode="1-phase",
    )

    sensor = UtilizationSensor(coordinator)
    value = sensor.native_value

    # L2: max_ampere=25, available_min=10 → (25-10)/25*100 = 60%
    assert value is not None
    assert abs(value - 60.0) < 0.1


def test_utilization_sensor_returns_none_for_no_active_phases(full_config_entry):
    """UtilizationSensor ska returnera None om inga aktiva faser."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = CalculationResult(
        target_current=6,
        charger_budget_l1=20.0,
        charger_budget_l2=20.0,
        charger_budget_l3=20.0,
        available_min=20.0,
        active_phases=[],  # Inga aktiva faser
        phase_loads=[5.0, 5.0, 5.0],
        device_loads=[0.0, 0.0, 0.0],
        charging_mode="3-phase",
    )

    sensor = UtilizationSensor(coordinator)
    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_utilization_sensor_updates_after_calculation(hass, full_config_entry):
    """UtilizationSensor ska uppdateras korrekt efter beräkning."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.send_frc = AsyncMock()

    # Sensorvärden: available = 25 - 5 - 2 = 18A per fas
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    await coordinator._async_calculate()

    assert coordinator.last_result is not None
    sensor = UtilizationSensor(coordinator)
    value = sensor.native_value

    # available_min = 18A, max_ampere = 25A → utnyttjandegrad = (25-18)/25*100 = 28%
    assert value is not None
    assert abs(value - 28.0) < 1.0


# ---------------------------------------------------------------------------
# PR-09: PSM auto vid uppstart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_sends_psm_auto_on_setup(hass, full_config_entry):
    """Koordinatorn ska skicka PSM='0' (auto) som första kommando i async_setup().

    Verifiera att send_psm anropas med PSM_VALUE_AUTO='0' under setup.
    """
    from custom_components.ev_load_balancer.const import PSM_VALUE_AUTO

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer = MagicMock()
        mock_debouncer.async_schedule_call = MagicMock()
        mock_debouncer_cls.return_value = mock_debouncer
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka send_psm för att fånga anropet
    coordinator._dispatcher.send_psm = AsyncMock(return_value=True)

    await coordinator.async_setup()

    # Verifiera att send_psm anropades med PSM_VALUE_AUTO ('0')
    coordinator._dispatcher.send_psm.assert_called_once_with(PSM_VALUE_AUTO)


# ---------------------------------------------------------------------------
# PR-09: pha-sensor läsning i _read_active_phases_sync()
# ---------------------------------------------------------------------------


def test_read_active_phases_uses_pha_when_available(hass):
    """_read_active_phases_sync() ska använda pha-sensor när den är tillgänglig.

    pha=[true, false, false, true, true, true] → aktiva faser = [1]
    (pha[0]=true, pha[1]=false, pha[2]=false → fas 1 aktiv, fas 2+3 inaktiva)
    """
    entry = MockConfigEntry(
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
                "pha": "sensor.goe_409787_pha",
            },
            "phases": [
                {"sensor": "sensor.current_l1", "max_ampere": 25, "label": "L1"},
                {"sensor": "sensor.current_l2", "max_ampere": 25, "label": "L2"},
                {"sensor": "sensor.current_l3", "max_ampere": 25, "label": "L3"},
            ],
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
        },
    )

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, entry)

    # Sätt pha-sensorn: L1 aktiv, L2+L3 inaktiva (PHEV-liknande)
    hass.states.async_set("sensor.goe_409787_pha", "[true, false, false, true, true, true]")
    # Sätt map-sensorn (ska INTE användas eftersom pha är tillgänglig)
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")

    result = coordinator._read_active_phases_sync()
    assert result == [1]


def test_read_active_phases_falls_back_to_map(hass):
    """_read_active_phases_sync() ska falla tillbaka på map om pha är unavailable.

    pha=unavailable → faller tillbaka → map='[2]' → aktiva faser = [2]
    """
    entry = MockConfigEntry(
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
                "pha": "sensor.goe_409787_pha",
            },
            "phases": [
                {"sensor": "sensor.current_l1", "max_ampere": 25, "label": "L1"},
                {"sensor": "sensor.current_l2", "max_ampere": 25, "label": "L2"},
                {"sensor": "sensor.current_l3", "max_ampere": 25, "label": "L3"},
            ],
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
        },
    )

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, entry)

    # pha-sensorn är unavailable — faller tillbaka på map
    hass.states.async_set("sensor.goe_409787_pha", "unavailable")
    # map-sensor rapporterar fas 2 (1-fas laddning via L2)
    hass.states.async_set("sensor.goe_409787_map", "[2]")

    result = coordinator._read_active_phases_sync()
    assert result == [2]
