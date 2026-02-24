"""Kommando-dispatcher för EV Load Balancer.

Skickar kommandon till laddarens entiteter via HA-tjänster.
Hanterar ström (amp), laddstyrning (frc) och pausning.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class CommandDispatcher:
    """Skickar kommandon till laddaren via Home Assistant-tjänster.

    Ansvarar för kommunikationen med laddarens HA-entiteter:
    - amp-entitet (number): ställer in laddström
    - frc-entitet (select): styr laddning (0=normal, 1=paus)

    Felhantering: loggar ERROR men propagerar inte undantag, för att
    undvika att ett misslyckat kommando kraschrar koordinatorn.

    Args:
        hass: Home Assistant-instansen.
        charger_entities: Dict med entitets-ID:n, t.ex.
            {"amp": "number.goe_amp", "frc": "select.goe_frc"}.
    """

    def __init__(self, hass: HomeAssistant, charger_entities: dict[str, str]) -> None:
        """Initialisera dispatcher med HA och entitets-ID:n."""
        self._hass = hass
        self._amp_entity = charger_entities.get("amp", "")
        self._frc_entity = charger_entities.get("frc", "")
        self._psm_entity = charger_entities.get("psm", "")

    async def send_amp(self, amp: int) -> bool:
        """Skicka ny laddström till laddarens amp-entitet.

        Anropar HA-tjänsten number.set_value med det nya amp-värdet.
        Loggar ERROR vid misslyckande men propagerar inte undantaget.

        Args:
            amp: Laddström i ampere.

        Returns:
            True om kommandot skickades utan fel, annars False.
        """
        if not self._amp_entity:
            _LOGGER.warning("Ingen amp-entitet konfigurerad — kan inte skicka ström %sA", amp)
            return False

        try:
            await self._hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": self._amp_entity, "value": amp},
                blocking=False,
            )
            _LOGGER.debug("Skickade amp=%sA till %s", amp, self._amp_entity)
            return True
        except Exception:
            _LOGGER.error(
                "Misslyckades att skicka amp=%sA till %s",
                amp,
                self._amp_entity,
                exc_info=True,
            )
            return False

    async def send_frc(self, value: str, *, blocking: bool = False) -> bool:
        """Skicka laddstyrningskommando till laddarens frc-entitet.

        Anropar HA-tjänsten select.select_option med det givna alternativet.
        Loggar ERROR vid misslyckande men propagerar inte undantaget.

        Args:
            value: Alternativ att välja, t.ex. "0" (normal) eller "1" (paus).
            blocking: Om True väntar tjänsteanropet på bekräftelse (default False).

        Returns:
            True om kommandot skickades utan fel, annars False.
        """
        if not self._frc_entity:
            _LOGGER.warning("Ingen frc-entitet konfigurerad — kan inte skicka frc='%s'", value)
            return False

        try:
            await self._hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": self._frc_entity, "option": value},
                blocking=blocking,
            )
            _LOGGER.debug("Skickade frc='%s' till %s", value, self._frc_entity)
            return True
        except Exception:
            _LOGGER.error(
                "Misslyckades att skicka frc='%s' till %s",
                value,
                self._frc_entity,
                exc_info=True,
            )
            return False

    async def send_psm(self, value: str) -> bool:
        """Skicka fasväxlingskommando till laddarens psm-entitet.

        Anropar HA-tjänsten select.select_option med det givna alternativet.
        PSM-entiteten är ALLTID en select (aldrig number) — Princip III.
        Loggar ERROR vid misslyckande men propagerar inte undantaget.

        Args:
            value: Alternativ att välja — "1" (1-fas) eller "2" (3-fas).
                   Använd PSM_VALUE_1PHASE och PSM_VALUE_3PHASE från const.py.

        Returns:
            True om kommandot skickades utan fel, annars False.
        """
        if not self._psm_entity:
            _LOGGER.warning("Ingen psm-entitet konfigurerad — kan inte skicka psm='%s'", value)
            return False

        try:
            await self._hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": self._psm_entity, "option": value},
                blocking=False,
            )
            _LOGGER.debug("Skickade psm='%s' till %s", value, self._psm_entity)
            return True
        except Exception:
            _LOGGER.error(
                "Misslyckades att skicka psm='%s' till %s",
                value,
                self._psm_entity,
                exc_info=True,
            )
            return False

    async def pause(self) -> bool:
        """Pausa laddning genom att sätta frc='1'.

        Skickar pauskommando till laddarens frc-entitet.

        Returns:
            True om kommandot skickades utan fel, annars False.
        """
        _LOGGER.debug("Pausar laddning (frc='1')")
        return await self.send_frc("1")

    async def resume(self, amp: int) -> bool:
        """Återuppta laddning genom att sätta frc='0' och ström till amp.

        Skickar resume-kommando (frc='0', blocking=True) följt av ström-kommando (amp).
        Om frc lyckas men amp misslyckas, görs ett säkerhetsåterställningsförsök
        med frc='1' (re-pause) för att undvika att laddaren körs utan strömgräns.

        Args:
            amp: Laddström i ampere att återuppta med.

        Returns:
            True om båda kommandona skickades utan fel, annars False.
        """
        _LOGGER.debug("Återupptar laddning (frc='0', amp=%sA)", amp)
        frc_ok = await self.send_frc("0", blocking=True)
        if not frc_ok:
            return False

        amp_ok = await self.send_amp(amp)
        if not amp_ok:
            # frc='0' lyckades men amp misslyckades — försök återpausa som säkerhetsåtgärd
            _LOGGER.warning(
                "amp=%sA misslyckades efter frc='0' — försöker återpausa (frc='1')",
                amp,
            )
            await self.send_frc("1")
            return False

        return True
