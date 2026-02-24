"""EV Load Balancer — dynamisk lastbalansering för Home Assistant."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .charger_profiles import PROFILES
from .const import CONF_PROFILE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sätt upp integrationen från en config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # Hämta profil-ID och validera att det är känt
    profile_id = entry.data.get(CONF_PROFILE_ID, "")
    if not profile_id or profile_id not in PROFILES:
        _LOGGER.error(
            "Okänt profil-ID '%s' i config entry '%s'. "
            "Kontrollera att integrationen är korrekt konfigurerad.",
            profile_id,
            entry.entry_id,
        )
        return False

    # Skapa eller hämta device i HA:s device registry
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="EV Load Balancer",
        model=entry.data.get(CONF_PROFILE_ID, "generic"),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Avinstallera integrationen och rensa hass.data."""
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
