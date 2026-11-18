"""Skattning av hushållets last per fas (spec 010, K-1).

Fasvärdet P från elmätaren är flera sekunder gammalt när det rapporteras,
laddarens ström är nästan färsk. Hushållslasten räknas därför inte som
P − aktuell laddarström utan begränsas av laddarens ström under fönstret W
före rapporten:

    låg = P − största laddarström i fönstret
    hög = P − minsta laddarström i fönstret
    h   = h begränsad till [låg, hög]

Ren Python utan HA-beroenden (Princip V).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import ESTIMATE_TIGHT_TOLERANCE


@dataclass(frozen=True)
class PhaseEstimate:
    """Resultatet av en fasrapport, med fälten som K-7 loggar."""

    reported: float
    """Fasvärdet P (A)."""

    device_min: float
    """Minsta laddarström i fönstret (A)."""

    device_max: float
    """Största laddarström i fönstret (A)."""

    low: float
    """P − device_max: lägsta hushållslast mätvärdet tillåter (A)."""

    high: float
    """P − device_min: högsta hushållslast mätvärdet tillåter (A)."""

    household: float
    """Skattningen h efter rapporten (A)."""

    tight: bool
    """Entydigt: hög − låg ≤ toleransen och historiken täcker hela fönstret."""


class LoadEstimator:
    """Håller skattningen h per fas och historiken den bygger på.

    Laddarens ström lagras som en stegfunktion: varje värde gäller tills nästa.
    Tider måste komma i icke-avtagande ordning.

    Args:
        phase_count: Antal konfigurerade faser.
        window_s: Fönstret W i sekunder.
        hold_s: Hålltiden för peak() i sekunder (K-4).
    """

    def __init__(self, phase_count: int, window_s: float, hold_s: float) -> None:
        """Initialisera utan historik."""
        self._window = timedelta(seconds=window_s)
        self._hold = timedelta(seconds=hold_s)
        self._device: list[tuple[datetime, list[float]]] = []
        self._commands: list[tuple[datetime, list[float]]] = []
        self._estimates: list[PhaseEstimate | None] = [None] * phase_count
        self._household_history: list[list[tuple[datetime, float]]] = [
            [] for _ in range(phase_count)
        ]

    @property
    def estimates(self) -> list[PhaseEstimate | None]:
        """Senaste skattning per fas, None för fas som ännu inte rapporterat."""
        return list(self._estimates)

    def record_device(self, now: datetime, values: list[float]) -> None:
        """Registrera laddarens ström per fas vid tidpunkten now."""
        self._device.append((now, list(values)))
        # 2W: rapporten behandlas efter sin rapporttid (last_reported), så dess
        # fönster börjar före now − W.
        cutoff = now - 2 * self._window
        # Senaste värdet före fönstret gäller vid fönstrets början och behålls.
        while len(self._device) > 1 and self._device[1][0] <= cutoff:
            self._device.pop(0)

    def record_command(self, now: datetime, values: list[float]) -> None:
        """Registrera ett skickat strömkommando som en ändring i fönstret.

        PRELIMINARY (spec 010 P-2): laddarens strömsensor antas visa en ändring
        inom ca 2 s. Kommandovärdet vidgar fönstret i W sekunder så att en
        sensor som släpar inte ger ett falskt entydigt mätvärde. Det ändrar
        inte stegfunktionen, eftersom bilen kan dra mindre än det begärda.
        """
        self._commands.append((now, list(values)))
        cutoff = now - self._window
        self._commands = [c for c in self._commands if c[0] >= cutoff]

    def on_phase_report(self, now: datetime, phase: int, value: float) -> PhaseEstimate:
        """Uppdatera h för fasen med fasvärdet value, rapporterat vid now.

        Kräver att record_device() anropats minst en gång före rapporten.
        Första rapporten sätter h = P − aktuell laddarström.

        PRELIMINARY (spec 010 P-1): ett fasvärde antas aldrig vara äldre än W
        när det rapporteras. Är det äldre kan laddarströmmen vid mättillfället
        saknas i fönstret, och h kan bli fel med högst en laddarändring per
        rapport (vakttest T-15).
        """
        start = now - self._window
        in_window = [v[phase] for t, v in self._device if start < t <= now]
        before = [v[phase] for t, v in self._device if t <= start]
        if before:
            in_window.append(before[-1])
        current = self._current_device(now, phase)
        if not in_window:
            # Rapporten är äldre än historiken (värdet skrevs före start). Laddarens
            # ström då är okänd; historiken täcker inte fönstret, så inte entydigt.
            in_window.append(current)
        in_window.extend(v[phase] for t, v in self._commands if start <= t <= now)

        device_min = min(in_window)
        device_max = max(in_window)
        low = value - device_max
        high = value - device_min

        previous = self._estimates[phase]
        base = previous.household if previous is not None else value - current
        household = min(max(base, low), high)

        covered = self._device[0][0] <= start
        estimate = PhaseEstimate(
            reported=value,
            device_min=device_min,
            device_max=device_max,
            low=low,
            high=high,
            household=household,
            tight=covered and high - low <= ESTIMATE_TIGHT_TOLERANCE,
        )
        self._estimates[phase] = estimate

        history = self._household_history[phase]
        history.append((now, household))
        hold_start = now - self._hold
        while len(history) > 1 and history[1][0] <= hold_start:
            history.pop(0)
        return estimate

    def peak(self, now: datetime) -> list[float | None]:
        """Högsta h per fas som gällt under hålltiden före now (K-4).

        Värdet som gällde när hålltiden började räknas med, eftersom det
        också var skattningen under en del av perioden.
        """
        hold_start = now - self._hold
        peaks: list[float | None] = []
        for history in self._household_history:
            seen = [h for t, h in history if t > hold_start]
            earlier = [h for t, h in history if t <= hold_start]
            if earlier:
                seen.append(earlier[-1])
            peaks.append(max(seen) if seen else None)
        return peaks

    def _current_device(self, now: datetime, phase: int) -> float:
        """Laddarens ström för fasen enligt senaste värde vid eller före now.

        Före första värdet gäller det första värdet.
        """
        earlier = [v[phase] for t, v in self._device if t <= now]
        return earlier[-1] if earlier else self._device[0][1][phase]
