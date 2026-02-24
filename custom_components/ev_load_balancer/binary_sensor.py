"""Binary sensor-plattform för EV Load Balancer.

Exponerar en HA-binärsensorentitet:
  - ev_load_balancer_capacity_warning : True när tillgänglig kapacitet understiger tröskel
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CAPACITY_WARNING_THRESHOLD,
    DEFAULT_CAPACITY_WARNING_THRESHOLD,
    DOMAIN,
)

if TYPE_CHECKING:
    from .sensor import EVLoadBalancerCoordinator

_LOGGER = logging.getLogger(__name__)

# Sensorns suffix för entitets-ID
BINARY_SENSOR_CAPACITY_WARNING = "capacity_warning"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sätt upp binary sensor-plattformen för en config entry.

    Hämtar koordinatorn från hass.data och registrerar kapacitetsvarnings-sensorn.
    """
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = domain_data.get("coordinator")
    if coordinator is None:
        _LOGGER.error(
            "binary_sensor: koordinatorn hittades inte för entry_id=%s — "
            "sensor-plattformen kanske inte är initialiserad ännu",
            entry.entry_id,
        )
        return

    async_add_entities(
        [
            EVLoadBalancerCapacityWarning(coordinator),
        ]
    )


class EVLoadBalancerCapacityWarning(BinarySensorEntity):
    """Binärsensor för kapacitetsvarning.

    Är True (on) när tillgänglig minsta ström (available_min) understiger
    det konfigurerade tröskelvärdet (default 3A).

    Uppdateras event-drivet via koordinator-lyssnare.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: EVLoadBalancerCoordinator) -> None:
        """Initialisera kapacitetsvarnings-sensorn.

        Args:
            coordinator: Koordinatorn som äger beräkningsdata.
        """
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{BINARY_SENSOR_CAPACITY_WARNING}"
        self._attr_name = "Capacity Warning"
        self._attr_translation_key = BINARY_SENSOR_CAPACITY_WARNING

    @property
    def device_info(self) -> DeviceInfo:
        """Returnerar device-info för att koppla sensorn till rätt device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.entry.entry_id)},
        )

    @property
    def is_on(self) -> bool | None:
        """Returnerar True om available_min är under tröskel.

        Returnerar None (unavailable) om beräkningsresultat saknas
        eller max_ampere är 0.
        """
        result = self._coordinator.last_result
        if result is None:
            return None

        threshold = self._get_threshold()
        return result.available_min < threshold

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Returnerar diagnostikattribut för kapacitetsvarning."""
        result = self._coordinator.last_result
        threshold = self._get_threshold()

        return {
            "available_min": result.available_min if result else None,
            "threshold": threshold,
        }

    def _get_threshold(self) -> int:
        """Läs kapacitetsvarnings-tröskel med options > data > default-prioritering."""
        opts = self._coordinator.entry.options
        data = self._coordinator.entry.data
        raw = opts.get(
            CONF_CAPACITY_WARNING_THRESHOLD,
            data.get(CONF_CAPACITY_WARNING_THRESHOLD, DEFAULT_CAPACITY_WARNING_THRESHOLD),
        )
        try:
            return int(raw)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Ogiltigt värde för %s: %r — faller tillbaka till default %sA",
                CONF_CAPACITY_WARNING_THRESHOLD,
                raw,
                DEFAULT_CAPACITY_WARNING_THRESHOLD,
            )
            return DEFAULT_CAPACITY_WARNING_THRESHOLD

    async def async_added_to_hass(self) -> None:
        """Registrera lyssnare och cleanup-callback när sensorn läggs till i HA."""
        await super().async_added_to_hass()
        self._coordinator.register_listener(self.async_write_ha_state)
        self.async_on_remove(
            lambda: self._coordinator.unregister_listener(self.async_write_ha_state)
        )
