"""Tester för EV Load Balancer __init__.py stubs."""

from unittest.mock import MagicMock

import pytest

from custom_components.ev_load_balancer import async_setup_entry, async_unload_entry


@pytest.mark.asyncio
async def test_async_setup_entry_returns_true():
    """async_setup_entry ska returnera True och initialisera hass.data."""
    hass = MagicMock()
    hass.data = {}
    entry = MagicMock()
    entry.entry_id = "test_entry"

    result = await async_setup_entry(hass, entry)

    assert result is True
    assert "ev_load_balancer" in hass.data
    assert hass.data["ev_load_balancer"]["test_entry"] == {}


@pytest.mark.asyncio
async def test_async_unload_entry_returns_true():
    """async_unload_entry ska returnera True och rensa hass.data."""
    hass = MagicMock()
    hass.data = {"ev_load_balancer": {"test_entry": {}}}
    entry = MagicMock()
    entry.entry_id = "test_entry"

    result = await async_unload_entry(hass, entry)

    assert result is True
    assert "test_entry" not in hass.data["ev_load_balancer"]
