"""Dedikerade failsafe-tester för EV Load Balancer (PR-05).

Täcker:
- US1: Enskild sensorförlust → reduce (send_amp med safe_default)
- US1: Enskild sensorförlust → pause (send_frc)
- US2: Total sensorförlust → alltid pause
- US3: Laddarens sensor unavailable → fallback till _last_sent_amp
- US4: HA-omstart → INITIALIZING → normalt tillstånd
- US5: Konfigurerbar safe_default_current
- Automatisk återhämtning från FAILSAFE
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_load_balancer.const import (
    CONF_ACTION_ON_SENSOR_LOSS,
    CONF_SAFE_DEFAULT_CURRENT,
    DEFAULT_SAFE_CURRENT,
    DOMAIN,
)
from custom_components.ev_load_balancer.sensor import (
    EVLoadBalancerCoordinator,
    async_setup_entry,
)
from custom_components.ev_load_balancer.state_machine import BalancerState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def full_config_entry():
    """MockConfigEntry med 3 faser och fullständig konfiguration."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer Failsafe Test",
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
def pause_config_entry():
    """MockConfigEntry med action_on_sensor_loss='pause'."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer Failsafe Pause",
        data={
            "profile_id": "goe_gemini",
            "serial": "409787",
            "charger_entities": {
                "amp": "number.goe_409787_amp",
                "frc": "select.goe_409787_frc",
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
            CONF_ACTION_ON_SENSOR_LOSS: "pause",
        },
    )


@pytest.fixture
def safe_current_8a_entry():
    """MockConfigEntry med safe_default_current=8A."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer Safe 8A",
        data={
            "profile_id": "goe_gemini",
            "serial": "409787",
            "charger_entities": {
                "amp": "number.goe_409787_amp",
                "frc": "select.goe_409787_frc",
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
            CONF_SAFE_DEFAULT_CURRENT: 8,
        },
    )


def _make_coordinator_in_balancing(hass, entry) -> EVLoadBalancerCoordinator:
    """Hjälpfunktion: skapa koordinator i BALANCING-tillstånd."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.send_frc = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert coordinator.state == BalancerState.BALANCING

    return coordinator


# ---------------------------------------------------------------------------
# US1: Enskild sensorförlust → reduce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_sensor_loss_reduce(hass, full_config_entry):
    """Enskild sensorförlust med action='reduce' → send_amp(6) och state=FAILSAFE."""
    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    # Sätt en fassensor till unavailable
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")

    await coordinator._async_handle_sensor_unavailable()

    # Ska ha skickat send_amp med safe_default (6A)
    coordinator._dispatcher.send_amp.assert_called_once_with(DEFAULT_SAFE_CURRENT)
    # Ska INTE ha pausat
    coordinator._dispatcher.pause.assert_not_called()
    # State machine ska vara i FAILSAFE
    assert coordinator.state == BalancerState.FAILSAFE
    # pause_reason ska vara 'sensor_unavailable'
    assert coordinator.pause_reason == "sensor_unavailable"


@pytest.mark.asyncio
async def test_single_sensor_loss_logs_error(hass, full_config_entry, caplog):
    """Enskild sensorförlust ska loggas med ERROR-nivå."""
    import logging

    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")

    with caplog.at_level(logging.ERROR, logger="custom_components.ev_load_balancer.sensor"):
        await coordinator._async_handle_sensor_unavailable()

    assert any("FAILSAFE" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# US1: Enskild sensorförlust → pause (action_on_sensor_loss='pause')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_sensor_loss_pause_action(hass, pause_config_entry):
    """Enskild sensorförlust med action='pause' → pause() anropas, ej send_amp."""
    coordinator = _make_coordinator_in_balancing(hass, pause_config_entry)

    # Verifiera att konfigurationen är korrekt läst
    assert coordinator._action_on_sensor_loss == "pause"

    # Sätt en fassensor till unavailable
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")

    await coordinator._async_handle_sensor_unavailable()

    # Ska ha pausat (frc='1')
    coordinator._dispatcher.pause.assert_called_once()
    # Ska INTE ha skickat send_amp
    coordinator._dispatcher.send_amp.assert_not_called()
    # State machine ska vara i FAILSAFE
    assert coordinator.state == BalancerState.FAILSAFE
    assert coordinator.pause_reason == "sensor_unavailable"


# ---------------------------------------------------------------------------
# US2: Total sensorförlust → alltid pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_sensor_loss_always_pauses(hass, full_config_entry):
    """Total sensorförlust → alltid pause oavsett action_on_sensor_loss."""
    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    # Verifiera att default-action är 'reduce'
    assert coordinator._action_on_sensor_loss == "reduce"

    # Alla fassensorer unavailable
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "unavailable")
    hass.states.async_set("sensor.current_l3", "unavailable")

    await coordinator._async_handle_sensor_unavailable()

    # Ska ha pausat (frc='1') — TROTS att action är 'reduce'
    coordinator._dispatcher.pause.assert_called_once()
    coordinator._dispatcher.send_amp.assert_not_called()
    assert coordinator.state == BalancerState.FAILSAFE
    assert coordinator.pause_reason == "sensor_unavailable"


@pytest.mark.asyncio
async def test_total_sensor_loss_logs_critical(hass, full_config_entry, caplog):
    """Total sensorförlust ska loggas med CRITICAL-nivå."""
    import logging

    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "unavailable")
    hass.states.async_set("sensor.current_l3", "unavailable")

    with caplog.at_level(logging.CRITICAL, logger="custom_components.ev_load_balancer.sensor"):
        await coordinator._async_handle_sensor_unavailable()

    assert any(
        record.levelno >= logging.CRITICAL and "FAILSAFE" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_total_sensor_loss_pauses_even_with_pause_action(hass, pause_config_entry):
    """Total sensorförlust pauses oavsett om action='pause' (konsistens)."""
    coordinator = _make_coordinator_in_balancing(hass, pause_config_entry)

    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "unavailable")
    hass.states.async_set("sensor.current_l3", "unavailable")

    await coordinator._async_handle_sensor_unavailable()

    coordinator._dispatcher.pause.assert_called_once()
    assert coordinator.state == BalancerState.FAILSAFE


# ---------------------------------------------------------------------------
# US3: Laddarens sensor unavailable → fallback till _last_sent_amp
# ---------------------------------------------------------------------------


def test_charger_sensor_unavailable_uses_last_sent_amp(hass, full_config_entry):
    """Laddarens nrg-sensor unavailable → fallback till _last_sent_amp."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Sätt last_sent_amp till 10A (simulerar aktiv laddning)
    coordinator._last_sent_amp = 10

    # Laddarens sensor är unavailable
    hass.states.async_set("sensor.goe_409787_nrg_4", "unavailable")
    hass.states.async_set("sensor.goe_409787_nrg_5", "unavailable")
    hass.states.async_set("sensor.goe_409787_nrg_6", "unavailable")

    device_values = coordinator._read_device_values_sync()

    # Ska använda _last_sent_amp (10A) som fallback för alla tre faser
    assert device_values == [10.0, 10.0, 10.0]


def test_charger_sensor_unavailable_logs_warning(hass, full_config_entry, caplog):
    """Laddarens nrg-sensor unavailable ska loggas med WARNING-nivå."""
    import logging

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._last_sent_amp = 8
    hass.states.async_set("sensor.goe_409787_nrg_4", "unavailable")
    hass.states.async_set("sensor.goe_409787_nrg_5", "12.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "12.0")

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_load_balancer.sensor"):
        device_values = coordinator._read_device_values_sync()

    # Fallback används
    assert device_values[0] == 8.0
    assert any("fallback" in record.message.lower() for record in caplog.records)


@pytest.mark.asyncio
async def test_charger_sensor_unavailable_continues_calculation(hass, full_config_entry):
    """Laddarens sensor unavailable → beräkning fortsätter med fallback-värde (ej FAILSAFE)."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)
    coordinator._dispatcher.resume = AsyncMock(return_value=True)

    # Sätt state till BALANCING
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    coordinator._last_sent_amp = 10

    # Fassensorer OK, men laddarens sensor unavailable
    hass.states.async_set("sensor.current_l1", "5.0")
    hass.states.async_set("sensor.current_l2", "5.0")
    hass.states.async_set("sensor.current_l3", "5.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "unavailable")
    hass.states.async_set("sensor.goe_409787_nrg_5", "unavailable")
    hass.states.async_set("sensor.goe_409787_nrg_6", "unavailable")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    await coordinator._async_calculate()

    # Ska INTE vara i FAILSAFE — beräkning fortsatte med fallback
    assert coordinator.state != BalancerState.FAILSAFE
    # Beräkningsresultat ska finnas (beräkning lyckades)
    assert coordinator.last_result is not None


# ---------------------------------------------------------------------------
# US4: HA-omstart — INITIALIZING-tillstånd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ha_restart_starts_in_initializing(hass, full_config_entry):
    """Vid HA-omstart ska systemet starta i INITIALIZING."""
    full_config_entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][full_config_entry.entry_id] = {}

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer = MagicMock()
        mock_debouncer.async_schedule_call = MagicMock()
        mock_debouncer.async_cancel = MagicMock()
        mock_debouncer_cls.return_value = mock_debouncer

        added_entities = []

        def mock_add_entities(entities):
            added_entities.extend(entities)

        await async_setup_entry(hass, full_config_entry, mock_add_entities)

    coordinator = hass.data[DOMAIN][full_config_entry.entry_id]["coordinator"]
    # Vid start ska state vara INITIALIZING
    assert coordinator.state == BalancerState.INITIALIZING


@pytest.mark.asyncio
async def test_ha_restart_stays_initializing_with_unavailable_sensors(hass, full_config_entry):
    """Vid HA-omstart med unavailable sensorer → stannar i INITIALIZING."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock()
    coordinator._dispatcher.pause = AsyncMock()

    # Sensorer är unavailable (HA-omstart — data ej tillgänglig ännu)
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "unavailable")
    hass.states.async_set("sensor.current_l3", "unavailable")
    hass.states.async_set("sensor.goe_409787_car_value", "Charging")

    await coordinator._async_calculate()

    # Ska stanna i INITIALIZING (inte övergå)
    assert coordinator.state == BalancerState.INITIALIZING
    # Inga kommandon ska ha skickats
    coordinator._dispatcher.send_amp.assert_not_called()
    coordinator._dispatcher.pause.assert_not_called()


@pytest.mark.asyncio
async def test_ha_restart_normalizes_after_sensors_available(hass, full_config_entry):
    """Efter HA-omstart: sensorer tillgängliga → normalisering till IDLE."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)

    # Sensorer tillgängliga
    hass.states.async_set("sensor.current_l1", "10.0")
    hass.states.async_set("sensor.current_l2", "10.0")
    hass.states.async_set("sensor.current_l3", "10.0")
    hass.states.async_set("sensor.goe_409787_nrg_4", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_5", "0.0")
    hass.states.async_set("sensor.goe_409787_nrg_6", "0.0")
    hass.states.async_set("sensor.goe_409787_map", "[1, 2, 3]")
    hass.states.async_set("sensor.goe_409787_car_value", "Idle")

    # Kör 2 beräkningar → IDLE
    await coordinator._async_calculate()
    await coordinator._async_calculate()

    assert coordinator.state == BalancerState.IDLE


# ---------------------------------------------------------------------------
# US5: Konfigurerbar safe_default_current
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configurable_safe_default_8a(hass, safe_current_8a_entry):
    """Enskild sensorförlust med safe_default=8A → send_amp(8)."""
    coordinator = _make_coordinator_in_balancing(hass, safe_current_8a_entry)

    # Verifiera konfigurationen
    assert coordinator._safe_default_current == 8

    # En fassensor unavailable
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")

    await coordinator._async_handle_sensor_unavailable()

    # Ska ha skickat 8A, inte 6A
    coordinator._dispatcher.send_amp.assert_called_once_with(8)
    assert coordinator.state == BalancerState.FAILSAFE


def test_safe_default_current_read_from_options(hass, full_config_entry):
    """safe_default_current ska läsas från entry.options med prioritet över entry.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            "profile_id": "goe_gemini",
            "phases": [],
            "charger_entities": {},
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
            CONF_SAFE_DEFAULT_CURRENT: 6,  # data: 6A
        },
        options={
            CONF_SAFE_DEFAULT_CURRENT: 10,  # options: 10A (ska ha prioritet)
        },
    )

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, entry)

    # Options ska ha prioritet
    assert coordinator._safe_default_current == 10


# ---------------------------------------------------------------------------
# Automatisk återhämtning (T021-T022)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_automatic_recovery_when_sensor_returns(hass, full_config_entry):
    """När sensor återkommer → recover_from_failsafe() → föregående tillstånd."""
    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    # Trigga failsafe
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")
    await coordinator._async_handle_sensor_unavailable()
    assert coordinator.state == BalancerState.FAILSAFE
    assert coordinator._state_machine.previous_state == BalancerState.BALANCING

    # Sensor återkommer
    hass.states.async_set("sensor.current_l1", "10.0")

    await coordinator._async_check_recovery()

    # Ska ha återhämtat sig till BALANCING
    assert coordinator.state == BalancerState.BALANCING
    assert coordinator.pause_reason is None


@pytest.mark.asyncio
async def test_no_recovery_if_sensor_still_unavailable(hass, full_config_entry):
    """Ingen återhämtning om minst en sensor fortfarande är unavailable."""
    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    # Trigga failsafe
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "unavailable")
    hass.states.async_set("sensor.current_l3", "12.0")
    await coordinator._async_handle_sensor_unavailable()
    assert coordinator.state == BalancerState.FAILSAFE

    # Bara en sensor återkommer (L1 fortfarande borta)
    hass.states.async_set("sensor.current_l2", "10.0")

    await coordinator._async_check_recovery()

    # Ska fortfarande vara i FAILSAFE
    assert coordinator.state == BalancerState.FAILSAFE


@pytest.mark.asyncio
async def test_recovery_from_idle_failsafe(hass, full_config_entry):
    """Återhämtning när föregående tillstånd var IDLE."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)

    # Sätt state till IDLE
    sm = coordinator._state_machine
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert coordinator.state == BalancerState.IDLE

    # Trigga failsafe från IDLE
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")
    await coordinator._async_handle_sensor_unavailable()
    assert coordinator.state == BalancerState.FAILSAFE

    # Sensor återkommer
    hass.states.async_set("sensor.current_l1", "10.0")
    await coordinator._async_check_recovery()

    # Ska ha återhämtat sig till IDLE (föregående tillstånd)
    assert coordinator.state == BalancerState.IDLE


@pytest.mark.asyncio
async def test_no_double_failsafe(hass, full_config_entry):
    """Ny unavailable-event ska inte trigga enter_failsafe() igen om redan i FAILSAFE."""
    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    # Trigga failsafe
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")
    await coordinator._async_handle_sensor_unavailable()
    assert coordinator.state == BalancerState.FAILSAFE

    previous_state = coordinator._state_machine.previous_state

    # Ny unavailable-händelse — ska ignoreras (ej dubbel-enter)
    await coordinator._async_handle_sensor_unavailable()

    # previous_state ska inte ha ändrats (ingen ny enter_failsafe)
    assert coordinator._state_machine.previous_state == previous_state
    assert coordinator.state == BalancerState.FAILSAFE


@pytest.mark.asyncio
async def test_recovery_logs_info(hass, full_config_entry, caplog):
    """Återhämtning ska loggas med INFO-nivå."""
    import logging

    coordinator = _make_coordinator_in_balancing(hass, full_config_entry)

    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")
    await coordinator._async_handle_sensor_unavailable()

    hass.states.async_set("sensor.current_l1", "10.0")

    with caplog.at_level(logging.INFO, logger="custom_components.ev_load_balancer.sensor"):
        await coordinator._async_check_recovery()

    assert any("återhämtning" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# Finding 5.1: INITIALIZING-guard — sensor unavailable under INITIALIZING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_unavailable_during_initializing_no_crash(hass, full_config_entry):
    """Sensor unavailable under INITIALIZING ska INTE kasta ValueError.

    enter_failsafe() kastar ValueError om anropas i INITIALIZING.
    _async_handle_sensor_unavailable ska returnera tidigt utan undantag.
    """
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    # Mocka dispatcher
    coordinator._dispatcher.send_amp = AsyncMock(return_value=True)
    coordinator._dispatcher.pause = AsyncMock(return_value=True)

    # Verifiera att state machine är i INITIALIZING (startläge)
    assert coordinator.state == BalancerState.INITIALIZING

    # Sätt en fassensor till "unavailable"
    hass.states.async_set("sensor.current_l1", "unavailable")
    hass.states.async_set("sensor.current_l2", "12.0")
    hass.states.async_set("sensor.current_l3", "12.0")

    # Anropa — ska INTE kasta ValueError
    await coordinator._async_handle_sensor_unavailable()

    # State ska fortfarande vara INITIALIZING (ingen övergång, inget undantag)
    assert coordinator.state == BalancerState.INITIALIZING
    # Inga dispatcher-kommandon ska ha skickats
    coordinator._dispatcher.send_amp.assert_not_called()
    coordinator._dispatcher.pause.assert_not_called()
