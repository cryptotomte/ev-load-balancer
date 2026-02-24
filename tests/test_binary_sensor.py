"""Tester för binary sensor-plattformen (binary_sensor.py) — PR-07.

Täcker:
- US2: EVLoadBalancerCapacityWarning is_on baserat på available_min vs threshold
- Options > data > default prioritering för threshold
- _attr_should_poll = False
- device_class = PROBLEM
- extra_state_attributes (available_min, threshold)
- async_setup_entry skapar entiteten
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_load_balancer.binary_sensor import (
    EVLoadBalancerCapacityWarning,
    async_setup_entry,
)
from custom_components.ev_load_balancer.calculator import CalculationResult
from custom_components.ev_load_balancer.const import (
    CONF_CAPACITY_WARNING_THRESHOLD,
    DEFAULT_CAPACITY_WARNING_THRESHOLD,
    DOMAIN,
)
from custom_components.ev_load_balancer.sensor import EVLoadBalancerCoordinator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def full_config_entry():
    """MockConfigEntry med komplett konfiguration."""
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
    """Hjälpfunktion: skapa koordinator med mockad hass."""
    mock_hass = MagicMock()
    mock_hass.states = MagicMock()
    mock_hass.states.get = MagicMock(return_value=None)

    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        return EVLoadBalancerCoordinator(mock_hass, entry)


def _make_result(available_min: float) -> CalculationResult:
    """Hjälpfunktion: skapa CalculationResult med givet available_min."""
    return CalculationResult(
        target_current=8,
        available_l1=available_min,
        available_l2=available_min,
        available_l3=available_min,
        available_min=available_min,
        active_phases=[1, 2, 3],
        phase_loads=[10.0, 10.0, 10.0],
        device_loads=[0.0, 0.0, 0.0],
        charging_mode="3-phase",
    )


# ---------------------------------------------------------------------------
# is_on: marginalen styr on/off
# ---------------------------------------------------------------------------


def test_capacity_warning_is_on_when_below_threshold(full_config_entry):
    """is_on ska vara True när available_min < threshold (default 3A)."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = _make_result(available_min=2.9)  # < 3A

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    assert sensor.is_on is True


def test_capacity_warning_is_off_when_above_threshold(full_config_entry):
    """is_on ska vara False när available_min >= threshold."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = _make_result(available_min=3.0)  # exakt = 3A, inte < 3A

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    assert sensor.is_on is False


def test_capacity_warning_is_off_when_well_above_threshold(full_config_entry):
    """is_on ska vara False vid god kapacitetsmarginal."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = _make_result(available_min=15.0)

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    assert sensor.is_on is False


def test_capacity_warning_is_none_when_no_result(full_config_entry):
    """is_on ska vara None (unavailable) om last_result är None."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = None

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    assert sensor.is_on is None


# ---------------------------------------------------------------------------
# Tröskel: options > data > default-prioritering
# ---------------------------------------------------------------------------


def test_capacity_warning_uses_default_threshold():
    """Threshold ska vara DEFAULT_CAPACITY_WARNING_THRESHOLD (3A) utan konfiguration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            "profile_id": "goe_gemini",
            "phases": [{"sensor": "sensor.l1", "max_ampere": 25, "label": "L1"}],
            "charger_entities": {},
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
        },
    )
    coordinator = _make_coordinator(entry)
    sensor = EVLoadBalancerCapacityWarning(coordinator)

    # Defaultvärde: 3A
    assert sensor._get_threshold() == DEFAULT_CAPACITY_WARNING_THRESHOLD


def test_capacity_warning_uses_data_threshold():
    """Threshold ska läsas från entry.data om options inte finns."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            "profile_id": "goe_gemini",
            "phases": [{"sensor": "sensor.l1", "max_ampere": 25, "label": "L1"}],
            "charger_entities": {},
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
            CONF_CAPACITY_WARNING_THRESHOLD: 5,
        },
    )
    coordinator = _make_coordinator(entry)
    sensor = EVLoadBalancerCapacityWarning(coordinator)

    assert sensor._get_threshold() == 5


def test_capacity_warning_uses_options_threshold():
    """Threshold ska läsas från entry.options med prioritet över data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            "profile_id": "goe_gemini",
            "phases": [{"sensor": "sensor.l1", "max_ampere": 25, "label": "L1"}],
            "charger_entities": {},
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
            CONF_CAPACITY_WARNING_THRESHOLD: 5,
        },
        options={
            CONF_CAPACITY_WARNING_THRESHOLD: 7,  # Options har prioritet
        },
    )
    coordinator = _make_coordinator(entry)
    sensor = EVLoadBalancerCapacityWarning(coordinator)

    assert sensor._get_threshold() == 7


def test_capacity_warning_custom_threshold_affects_is_on():
    """Anpassat threshold ska påverka is_on."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            "profile_id": "goe_gemini",
            "phases": [{"sensor": "sensor.l1", "max_ampere": 25, "label": "L1"}],
            "charger_entities": {},
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
            CONF_CAPACITY_WARNING_THRESHOLD: 10,
        },
    )
    coordinator = _make_coordinator(entry)
    # 8A < threshold=10 → on
    coordinator.last_result = _make_result(available_min=8.0)

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    assert sensor.is_on is True

    # Ändra result: 10A = threshold → off (inte < 10)
    coordinator.last_result = _make_result(available_min=10.0)
    assert sensor.is_on is False


# ---------------------------------------------------------------------------
# Sensor-egenskaper
# ---------------------------------------------------------------------------


def test_capacity_warning_should_not_poll(full_config_entry):
    """EVLoadBalancerCapacityWarning._attr_should_poll ska vara False."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = EVLoadBalancerCapacityWarning(coordinator)
    assert sensor._attr_should_poll is False


def test_capacity_warning_device_class_is_problem(full_config_entry):
    """EVLoadBalancerCapacityWarning ska ha device_class=PROBLEM."""
    coordinator = _make_coordinator(full_config_entry)
    sensor = EVLoadBalancerCapacityWarning(coordinator)
    assert sensor._attr_device_class == BinarySensorDeviceClass.PROBLEM


def test_capacity_warning_extra_attributes_with_result(full_config_entry):
    """extra_state_attributes ska inkludera available_min och threshold."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = _make_result(available_min=2.5)

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    attrs = sensor.extra_state_attributes

    assert "available_min" in attrs
    assert attrs["available_min"] == 2.5
    assert "threshold" in attrs
    assert attrs["threshold"] == DEFAULT_CAPACITY_WARNING_THRESHOLD


def test_capacity_warning_extra_attributes_without_result(full_config_entry):
    """extra_state_attributes ska hantera None-result korrekt."""
    coordinator = _make_coordinator(full_config_entry)
    coordinator.last_result = None

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    attrs = sensor.extra_state_attributes

    assert attrs["available_min"] is None
    assert "threshold" in attrs


# ---------------------------------------------------------------------------
# Lyssnare: registreras i async_added_to_hass
# ---------------------------------------------------------------------------


def test_capacity_warning_does_not_register_on_create(full_config_entry):
    """Sensorn ska INTE registrera sig hos koordinatorn i __init__."""
    coordinator = _make_coordinator(full_config_entry)
    initial_count = len(coordinator._notify_listeners)

    EVLoadBalancerCapacityWarning(coordinator)

    assert len(coordinator._notify_listeners) == initial_count


@pytest.mark.asyncio
async def test_capacity_warning_registers_in_async_added_to_hass(hass, full_config_entry):
    """Sensorn ska registrera sig hos koordinatorn i async_added_to_hass."""
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    initial_count = len(coordinator._notify_listeners)

    sensor = EVLoadBalancerCapacityWarning(coordinator)
    sensor.hass = hass

    await sensor.async_added_to_hass()

    assert len(coordinator._notify_listeners) == initial_count + 1


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_creates_binary_sensor(hass, full_config_entry):
    """async_setup_entry ska skapa 1 binary sensor och lägga till i HA."""
    full_config_entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][full_config_entry.entry_id] = {}

    # Skapa koordinatorn manuellt (normalt sköts detta av sensor platform)
    with patch("custom_components.ev_load_balancer.sensor.Debouncer") as mock_debouncer_cls:
        mock_debouncer_cls.return_value = MagicMock()
        coordinator = EVLoadBalancerCoordinator(hass, full_config_entry)

    hass.data[DOMAIN][full_config_entry.entry_id]["coordinator"] = coordinator

    added_entities = []

    def mock_add_entities(entities):
        added_entities.extend(entities)

    await async_setup_entry(hass, full_config_entry, mock_add_entities)

    assert len(added_entities) == 1
    assert isinstance(added_entities[0], EVLoadBalancerCapacityWarning)
