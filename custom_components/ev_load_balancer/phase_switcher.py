"""Fasväxlingslogik för EV Load Balancer.

Hanterar automatisk växling mellan 1-fas och 3-fas laddning baserat på
tillgänglig kapacitet per fas.

Ren Python utan HA-beroenden — fullt testbar med vanlig pytest (Princip V).

Regler:
- Nedväxling 3→1: Om någon fas < min_current och L2 har kapacitet
- Uppväxling 1→3: Om alla faser >= min_current i 60s (PHASE_SWITCH_UPSCALE_DELAY)
- PHEV-skydd: Om device_supports_3phase=False → returnera alltid None
- go-e Gemini använder L2 vid 1-fas laddning (bekräftat i PRD)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .const import PHASE_SWITCH_UPSCALE_DELAY


class PhaseMode(StrEnum):
    """Möjliga fasladdningslägen."""

    THREE_PHASE = "three_phase"
    """3-fas laddning (standard för go-e Gemini)."""

    ONE_PHASE = "one_phase"
    """1-fas laddning (via L2 för go-e Gemini)."""


@dataclass(frozen=True)
class PhaseSwitchCommand:
    """Kommando från PhaseSwitcher.evaluate().

    Attributes:
        action: Åtgärdens namn — "switch_to_1phase" eller "switch_to_3phase".
        target_mode: Önskat läge efter åtgärden.
        reason: Läsbar förklaring till varför åtgärden valdes.
    """

    action: str
    """Åtgärdens namn: "switch_to_1phase" eller "switch_to_3phase"."""

    target_mode: PhaseMode
    """Önskat fasläge efter fasväxlingen."""

    reason: str
    """Läsbar förklaring till åtgärden."""


class PhaseSwitcher:
    """Bestämmer när fasväxling ska ske baserat på tillgänglig kapacitet.

    Hanterar hysteres-timer för uppväxling (60s) och omedelbar nedväxling
    om L2-kapacitet finns. PHEV-skydd inaktiverar fasväxling om laddaren
    enbart stödjer 1-fas.

    Args:
        min_current: Lägsta tillåtna laddström i ampere.
    """

    def __init__(self, min_current: int) -> None:
        """Initialisera PhaseSwitcher.

        Args:
            min_current: Lägsta tillåtna laddström i ampere.
        """
        self._current_mode = PhaseMode.THREE_PHASE
        self._min_current = min_current
        self._device_supports_3phase = True
        # Timer: tidpunkt när alla faser senast gick över min_current (1-fas → 3-fas)
        self._all_phases_ok_since: datetime | None = None

    @property
    def current_mode(self) -> PhaseMode:
        """Returnerar aktuellt fasläge."""
        return self._current_mode

    def set_device_capability(self, supports_3phase: bool) -> None:
        """Sätt laddarens 3-fas-kapabilitet.

        Anropas av koordinatorn vid PHEV-detektion (map visar enbart 1-fas).
        Om supports_3phase=False inaktiveras all fasväxlingslogik.

        Args:
            supports_3phase: True om laddaren klarar 3-fas, annars False.
        """
        self._device_supports_3phase = supports_3phase

    def evaluate(
        self,
        available_per_phase: dict[str, float],
        min_current: int,
        now: datetime,
    ) -> PhaseSwitchCommand | None:
        """Utvärdera om fasväxling ska ske.

        Beslutslogik:
        1. PHEV-skydd: Om device_supports_3phase=False → returnera None.
        2. THREE_PHASE: Om någon fas < min_current → kontrollera L2.
           - L2 >= min_current → returnera switch_to_1phase.
           - L2 < min_current → returnera None (pauslogiken tar över).
        3. ONE_PHASE: Om ALLA faser >= min_current → starta/kontrollera
           hysteres-timer (60s). Efter 60s → returnera switch_to_3phase.
        4. ONE_PHASE och INTE alla faser OK → nollställ timer → returnera None.

        Args:
            available_per_phase: Tillgänglig ström per fas {"l1": A, "l2": A, "l3": A}.
            min_current: Lägsta tillåtna laddström i ampere.
            now: Aktuell tidpunkt.

        Returns:
            PhaseSwitchCommand om fasväxling ska ske, annars None.
        """
        # --- PHEV-skydd: laddaren stödjer inte 3-fas ---
        if not self._device_supports_3phase:
            return None

        l1 = available_per_phase.get("l1", 0.0)
        l2 = available_per_phase.get("l2", 0.0)
        l3 = available_per_phase.get("l3", 0.0)

        if self._current_mode == PhaseMode.THREE_PHASE:
            # --- Nedväxling 3→1: om någon fas under min_current ---
            if l1 < min_current or l2 < min_current or l3 < min_current:
                # Nollställ uppväxlings-timer (spelar inte roll i 3-fas, men för säkerhets skull)
                self._all_phases_ok_since = None

                if l2 >= min_current:
                    # L2 har kapacitet — växla till 1-fas via L2
                    return PhaseSwitchCommand(
                        action="switch_to_1phase",
                        target_mode=PhaseMode.ONE_PHASE,
                        reason=(
                            f"Nedväxling 3→1: kapacitetsbrist på fas "
                            f"(l1={l1:.1f}A, l2={l2:.1f}A, l3={l3:.1f}A), "
                            f"L2 har kapacitet ({l2:.1f}A >= {min_current}A)"
                        ),
                    )
                # L2 saknar också kapacitet — pauslogiken tar över
                return None

            # Alla faser OK i 3-fas — ingen åtgärd
            return None

        # self._current_mode == PhaseMode.ONE_PHASE
        # --- Uppväxling 1→3: alla faser måste ha kapacitet i 60s ---
        if l1 >= min_current and l2 >= min_current and l3 >= min_current:
            # Starta hysteres-timer om ej startad
            if self._all_phases_ok_since is None:
                self._all_phases_ok_since = now

            elapsed = (now - self._all_phases_ok_since).total_seconds()
            if elapsed >= PHASE_SWITCH_UPSCALE_DELAY:
                # Nollställ timer inför nästa cykel
                self._all_phases_ok_since = None
                return PhaseSwitchCommand(
                    action="switch_to_3phase",
                    target_mode=PhaseMode.THREE_PHASE,
                    reason=(
                        f"Uppväxling 1→3: alla faser >= {min_current}A "
                        f"i {elapsed:.0f}s (hysteres {PHASE_SWITCH_UPSCALE_DELAY}s uppfylld)"
                    ),
                )
            # Timer ej expired
            return None

        # Kapacitet otillräcklig för uppväxling — nollställ timer
        self._all_phases_ok_since = None
        return None

    def record_mode_change(self, new_mode: PhaseMode) -> None:
        """Registrera att en fasväxling genomfördes.

        Uppdaterar current_mode och nollställer hysteres-timer.
        Ska anropas av koordinatorn direkt efter att PSM-kommandot skickats.

        Args:
            new_mode: Nytt fasläge efter fasväxlingen.
        """
        self._current_mode = new_mode
        self._all_phases_ok_since = None
