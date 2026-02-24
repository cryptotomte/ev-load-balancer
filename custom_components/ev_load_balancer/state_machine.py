"""State machine för EV Load Balancer.

Hanterar tillståndsövergångar för lastbalansering.
Ren Python utan HA-beroenden — fullt testbar med vanlig pytest.
"""

from __future__ import annotations

from enum import StrEnum


class BalancerState(StrEnum):
    """Möjliga tillstånd för lastbalanseraren."""

    INITIALIZING = "initializing"
    IDLE = "idle"
    BALANCING = "balancing"
    PAUSED = "paused"


# Minsta antal lyckade beräkningar för övergång från INITIALIZING till IDLE
_MIN_SUCCESSFUL_CALCULATIONS = 2


class LoadBalancerStateMachine:
    """Hanterar tillståndsövergångar för lastbalansering.

    Tillståndsmaskin som styr övergångar baserat på beräkningsresultat
    och bilstatus. Startar alltid i INITIALIZING vid start/omstart.

    Tillståndsövergångar:
        INITIALIZING → IDLE       : 2 lyckade beräkningar utan unavailable-sensorer
        IDLE         → BALANCING  : bil ansluten (car_value != "Idle")
        BALANCING    → IDLE       : bil bortkopplad (car_value == "Idle")
        BALANCING    → PAUSED     : target_current < min_current
        PAUSED       → BALANCING  : target_current >= min_current
    """

    def __init__(self) -> None:
        """Initialisera state machine i INITIALIZING."""
        self._state = BalancerState.INITIALIZING
        self._successful_calculations = 0

    @property
    def state(self) -> BalancerState:
        """Returnerar aktuellt tillstånd."""
        return self._state

    def record_successful_calculation(self) -> bool:
        """Registrera en lyckad beräkning (alla sensorer tillgängliga).

        Anropas bara om INGA fassensorer har state unavailable eller unknown.
        Övergår från INITIALIZING till IDLE efter 2 lyckade beräkningar.

        Returns:
            True om tillståndet ändrades (INITIALIZING → IDLE), annars False.
        """
        if self._state != BalancerState.INITIALIZING:
            # Är inte i INITIALIZING — ingenting händer
            return False

        self._successful_calculations += 1
        if self._successful_calculations >= _MIN_SUCCESSFUL_CALCULATIONS:
            self._state = BalancerState.IDLE
            return True
        return False

    def on_car_connected(self) -> bool:
        """Bil ansluten — övergå från IDLE till BALANCING.

        Returns:
            True om tillståndet ändrades, annars False.

        Raises:
            ValueError: Om anropad i ogiltigt tillstånd (INITIALIZING).
        """
        if self._state == BalancerState.INITIALIZING:
            raise ValueError(
                f"Ogiltig övergång: on_car_connected() kan inte anropas i tillstånd {self._state}"
            )
        if self._state == BalancerState.IDLE:
            self._state = BalancerState.BALANCING
            return True
        # BALANCING eller PAUSED — bil redan ansluten, ignorera
        return False

    def on_car_disconnected(self) -> bool:
        """Bil bortkopplad — övergå från BALANCING/PAUSED till IDLE.

        Returns:
            True om tillståndet ändrades, annars False.

        Raises:
            ValueError: Om anropad i ogiltigt tillstånd (INITIALIZING).
        """
        if self._state == BalancerState.INITIALIZING:
            raise ValueError(
                "Ogiltig övergång: on_car_disconnected() kan inte anropas "
                f"i tillstånd {self._state}"
            )
        if self._state in (BalancerState.BALANCING, BalancerState.PAUSED):
            self._state = BalancerState.IDLE
            return True
        # Redan IDLE — ignorera
        return False

    def on_below_min_current(self) -> bool:
        """Kapacitetsbrist — övergå från BALANCING till PAUSED.

        Anropas när beräknad target_current < min_current.

        Returns:
            True om tillståndet ändrades, annars False.

        Raises:
            ValueError: Om anropad i ogiltigt tillstånd.
        """
        if self._state not in (BalancerState.BALANCING, BalancerState.PAUSED):
            raise ValueError(
                "Ogiltig övergång: on_below_min_current() kan inte anropas "
                f"i tillstånd {self._state}"
            )
        if self._state == BalancerState.BALANCING:
            self._state = BalancerState.PAUSED
            return True
        # Redan PAUSED — ignorera
        return False

    def on_above_min_current(self) -> bool:
        """Kapacitet återkommer — övergå från PAUSED till BALANCING.

        Anropas när beräknad target_current >= min_current.

        Returns:
            True om tillståndet ändrades, annars False.

        Raises:
            ValueError: Om anropad i ogiltigt tillstånd.
        """
        if self._state not in (BalancerState.BALANCING, BalancerState.PAUSED):
            raise ValueError(
                "Ogiltig övergång: on_above_min_current() kan inte anropas "
                f"i tillstånd {self._state}"
            )
        if self._state == BalancerState.PAUSED:
            self._state = BalancerState.BALANCING
            return True
        # Redan BALANCING — ignorera
        return False
