"""Hysteres-styrning för EV Load Balancer.

Implementerar timer-baserad hysteres för att undvika snabb switching
mellan BALANCING och PAUSED vid kapacitetsgränsen.

Ren Python utan HA-beroenden — fullt testbar med vanlig pytest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class HysteresisAction(StrEnum):
    """Möjliga åtgärder som evaluate() kan returnera."""

    NONE = "none"
    """Ingen åtgärd — vänta eller ingen förändring."""

    SET_AMP = "set_amp"
    """Skicka ny laddström till laddaren."""

    PAUSE = "pause"
    """Pausa laddning (frc='1')."""

    RESUME = "resume"
    """Återuppta laddning (frc='0' + amp)."""


@dataclass(frozen=True)
class HysteresisCommand:
    """Kommando från HysteresisController.evaluate().

    Attributes:
        action: Vilken åtgärd som ska utföras.
        amp: Laddström i ampere — satt vid SET_AMP och RESUME, annars None.
        reason: Läsbar förklaring till varför åtgärden valdes.
    """

    action: HysteresisAction
    amp: int | None
    reason: str


class HysteresisController:
    """Hysteres-kontroller som bestämmer när ström-kommandon ska skickas.

    Hanterar timer-baserad hysteres för paus och resume, samt cooldown
    för uppåtreglering. Koordinatorn ansvarar för att anropa
    record_amp_change() varje gång ett SET_AMP eller RESUME-kommando faktiskt
    skickats till laddaren.

    Args:
        min_current: Lägsta tillåtna laddström i ampere.
        resume_threshold_offset: Ampere ovanför min_current för resume (default 2).
        pause_delay: Sekunder under min_current innan PAUSE skickas (default 15.0).
        resume_delay: Sekunder över resume_threshold innan RESUME skickas (default 30.0).
        cooldown: Sekunder mellan uppåtregleringar (default 5.0).
    """

    def __init__(
        self,
        min_current: int,
        resume_threshold_offset: int = 2,
        pause_delay: float = 15.0,
        resume_delay: float = 30.0,
        cooldown: float = 5.0,
    ) -> None:
        """Initialisera hysteres-kontrollern."""
        self._min_current = min_current
        self._resume_threshold = min_current + resume_threshold_offset
        self._pause_delay = pause_delay
        self._resume_delay = resume_delay
        self._cooldown = cooldown

        # Tidpunkt när available_min gick under min_current (None = ej aktiv)
        self._below_min_since: datetime | None = None

        # Tidpunkt när available_min gick över resume_threshold (None = ej aktiv)
        self._above_resume_since: datetime | None = None

        # Tidpunkt för senast skickad amp-förändring (sätts av koordinatorn via record_amp_change)
        self._last_amp_change_time: datetime | None = None

    def record_amp_change(self, now: datetime) -> None:
        """Registrera att ett amp-kommando (SET_AMP eller RESUME) nyss skickades.

        Ska anropas av koordinatorn direkt efter att kommandot skickats.
        Startar cooldown-timer för nästa uppåtreglering.

        Args:
            now: Aktuell tidpunkt.
        """
        self._last_amp_change_time = now

    def reset(self) -> None:
        """Nollställ alla timers.

        Anropas av koordinatorn vid bilfrånkoppling (BALANCING/PAUSED → IDLE).
        """
        self._below_min_since = None
        self._above_resume_since = None
        self._last_amp_change_time = None

    def evaluate(
        self,
        available_min: float,
        target_current: int,
        last_sent_amp: int,
        is_paused: bool,
        now: datetime,
    ) -> HysteresisCommand:
        """Utvärdera aktuellt tillstånd och returnera lämpligt kommando.

        Beslutslogik (prioritetsordning):
        1. Om is_paused: hantera resume-timer och returnera tidigt.
        2. Nedreglering (target < last_sent): skicka omedelbart.
        3. Kapacitetsbrist (available_min < min): starta/fortsätt paus-timer.
        4. Tillräcklig kapacitet (available_min >= min): nollställ paus-timer.
        5. Uppreglering (target > last_sent): skicka om cooldown passerat.
        6. Oförändrat (target == last_sent): ingen åtgärd.

        Args:
            available_min: Oklämd minsta tillgängliga ström (A).
            target_current: Klämd beräknad målström (A).
            last_sent_amp: Senast bekräftad laddström (A).
            is_paused: True om laddaren är pausad.
            now: Aktuell tidpunkt.

        Returns:
            HysteresisCommand med åtgärd och eventuell ström.
        """
        # --- Prioritet 1: PAUSED-läge — hantera resume ---
        if is_paused:
            return self._evaluate_paused(available_min, target_current, now)

        # --- Prioritet 2: Nedreglering — skicka omedelbart ---
        if target_current < last_sent_amp:
            # Nollställ paus-timer (nedreglering är inte kapacitetsbrist)
            self._below_min_since = None
            return HysteresisCommand(
                action=HysteresisAction.SET_AMP,
                amp=target_current,
                reason=f"Nedreglering: {last_sent_amp}A → {target_current}A",
            )

        # --- Prioritet 3 & 4: Kapacitetsbrist / Tillräcklig kapacitet ---
        if available_min < self._min_current:
            # Kapacitetsbrist — starta eller fortsätt paus-timer
            if self._below_min_since is None:
                self._below_min_since = now

            elapsed = (now - self._below_min_since).total_seconds()
            if elapsed >= self._pause_delay:
                return HysteresisCommand(
                    action=HysteresisAction.PAUSE,
                    amp=None,
                    reason=(
                        f"Kapacitetsbrist: available_min={available_min:.1f}A < "
                        f"min={self._min_current}A i {elapsed:.0f}s"
                    ),
                )
            return HysteresisCommand(
                action=HysteresisAction.NONE,
                amp=None,
                reason=(
                    f"Kapacitetsbrist: väntar på paus-timer ({elapsed:.1f}s / {self._pause_delay}s)"
                ),
            )

        # Tillräcklig kapacitet — nollställ paus-timer
        self._below_min_since = None

        # --- Prioritet 5: Uppreglering med cooldown ---
        if target_current > last_sent_amp:
            if self._last_amp_change_time is None:
                return HysteresisCommand(
                    action=HysteresisAction.SET_AMP,
                    amp=target_current,
                    reason=f"Uppreglering (ingen tidigare amp-ändring): → {target_current}A",
                )
            elapsed_cooldown = (now - self._last_amp_change_time).total_seconds()
            if elapsed_cooldown >= self._cooldown:
                return HysteresisCommand(
                    action=HysteresisAction.SET_AMP,
                    amp=target_current,
                    reason=(
                        f"Uppreglering: {last_sent_amp}A → {target_current}A "
                        f"(cooldown {elapsed_cooldown:.1f}s >= {self._cooldown}s)"
                    ),
                )
            return HysteresisCommand(
                action=HysteresisAction.NONE,
                amp=None,
                reason=(
                    f"Uppreglering blockerad av cooldown "
                    f"({elapsed_cooldown:.1f}s < {self._cooldown}s)"
                ),
            )

        # --- Prioritet 6: Oförändrat ---
        return HysteresisCommand(
            action=HysteresisAction.NONE,
            amp=None,
            reason="Ingen förändring (target == last_sent)",
        )

    def _evaluate_paused(
        self,
        available_min: float,
        target_current: int,
        now: datetime,
    ) -> HysteresisCommand:
        """Hantera utvärdering i PAUSED-läge.

        Args:
            available_min: Oklämd minsta tillgängliga ström (A).
            target_current: Klämd beräknad målström (A).
            now: Aktuell tidpunkt.

        Returns:
            HysteresisCommand med RESUME eller NONE.
        """
        if available_min >= self._resume_threshold:
            # Tillräcklig kapacitet — starta eller fortsätt resume-timer
            if self._above_resume_since is None:
                self._above_resume_since = now

            elapsed = (now - self._above_resume_since).total_seconds()
            if elapsed >= self._resume_delay:
                # Nollställ resume-timer för nästa paus/resume-cykel
                self._above_resume_since = None
                return HysteresisCommand(
                    action=HysteresisAction.RESUME,
                    amp=target_current,
                    reason=(
                        f"Resume: available_min={available_min:.1f}A >= "
                        f"threshold={self._resume_threshold}A i {elapsed:.0f}s"
                    ),
                )
            return HysteresisCommand(
                action=HysteresisAction.NONE,
                amp=None,
                reason=(f"Resume: väntar på resume-timer ({elapsed:.1f}s / {self._resume_delay}s)"),
            )

        # Kapacitet otillräcklig för resume — nollställ resume-timer
        self._above_resume_since = None
        return HysteresisCommand(
            action=HysteresisAction.NONE,
            amp=None,
            reason=(
                f"PAUSED: available_min={available_min:.1f}A < threshold={self._resume_threshold}A"
            ),
        )
