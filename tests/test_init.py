"""Tester för EV Load Balancer __init__.py."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_load_balancer import async_unload_entry
from custom_components.ev_load_balancer.const import DOMAIN


@pytest.mark.asyncio
async def test_async_setup_entry_returns_true(hass):
    """async_setup_entry ska returnera True och initialisera hass.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer",
        data={"profile_id": "goe_gemini"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.async_setup(entry.entry_id)

    assert result is True
    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_async_unload_entry_returns_true(hass):
    """async_unload_entry ska returnera True och rensa hass.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer",
        data={"profile_id": "goe_gemini"},
    )
    entry.add_to_hass(hass)

    # Sätt upp först
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.entry_id in hass.data[DOMAIN]

    # Avinstallera
    result = await async_unload_entry(hass, entry)

    assert result is True
    assert entry.entry_id not in hass.data[DOMAIN]
