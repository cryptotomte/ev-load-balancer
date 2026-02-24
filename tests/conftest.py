"""Gemensam testkonfiguration för EV Load Balancer."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_load_balancer.const import DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Aktivera custom integrations automatiskt för alla tester."""
    return enable_custom_integrations


@pytest.fixture
def ev_config_entry():
    """Mock config entry som enkel dict (bakåtkompatibel fixture för test_init.py)."""
    return {
        "entry_id": "test_entry",
        "domain": "ev_load_balancer",
        "title": "Test EV Load Balancer",
        "data": {},
        "options": {},
    }


@pytest.fixture
def mock_config_entry():
    """MockConfigEntry med fullt konfigurationsdata för config flow-tester."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer",
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
            "phases": [{"sensor": "sensor.current_be1_30051", "max_ampere": 25, "label": "L1"}],
            "safety_margin": 2,
            "min_current": 6,
            "max_current": 16,
            "phase_count": "auto",
        },
    )
