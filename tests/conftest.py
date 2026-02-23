"""Gemensam testkonfiguration för EV Load Balancer."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def ev_config_entry():
    """Mock config entry för tester."""
    return {
        "entry_id": "test_entry",
        "domain": "ev_load_balancer",
        "title": "Test EV Load Balancer",
        "data": {},
        "options": {},
    }
