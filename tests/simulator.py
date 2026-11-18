"""Pure-Python models for the closed-loop regulation tests (spec 010, section 6.3).

A recorded log cannot be replayed, because the phase values in it depend on
what the old regulation did. These models close the loop instead: household
load per phase over time, a charger that follows the commanded current with
the measured ramp times, and a meter that samples every telegram interval and
is read by Home Assistant with a delay. Parameters come from spec 2.2
(measurement 2026-11-18).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

Household = Callable[[float], list[float]]

# Spec 2.2: the charger draws ca 0.5 A below the commanded value.
CHARGER_SHORTFALL_A = 0.5
# Spec 2.2: a lower value is reached in ca 1 s, a higher one in 5-6 s (6 -> 15.5 A).
CHARGER_DOWN_RATE_A_PER_S = 10.0
CHARGER_UP_RATE_A_PER_S = 9.5 / 5.5
# Spec 2.2 / G-4: a phase switch interrupts charging for 15 s, then ramps up in 6 s.
PHASE_SWITCH_INTERRUPTION_S = 15.0
PHASE_SWITCH_RAMP_S = 6.0


def constant(l1: float, l2: float, l3: float) -> Household:
    """Household load that never changes."""
    return lambda _t: [l1, l2, l3]


def step(base: list[float], phase: int, delta: float, start: float, end: float | None = None):
    """Household load with ``delta`` added on one phase between start and end."""

    def load(t: float) -> list[float]:
        values = list(base)
        if t >= start and (end is None or t < end):
            values[phase] += delta
        return values

    return load


def pulses(
    base: list[float], phase: int, amplitude: float, start: float, on_s: float, offs: list[float]
):
    """Household load with a pulse train on one phase: ``on_s`` on, then each gap in ``offs``."""
    windows: list[tuple[float, float]] = []
    t = start
    for off in offs:
        windows.append((t, t + on_s))
        t += on_s + off

    def load(now: float) -> list[float]:
        values = list(base)
        if any(a <= now < b for a, b in windows):
            values[phase] += amplitude
        return values

    return load


@dataclass
class Charger:
    """Charger that follows the commanded current per phase.

    In one-phase mode it draws on L1, as measured 2026-11-18 (spec 2.5).
    """

    amp: int
    three_phase: bool = True
    paused: bool = False
    draw: float = 0.0
    interrupted_until: float = -1.0
    ramp_rate: float = CHARGER_UP_RATE_A_PER_S
    pending_three_phase: bool | None = None

    def __post_init__(self) -> None:
        self.draw = self.amp - CHARGER_SHORTFALL_A

    def set_amp(self, amp: int) -> None:
        self.amp = amp

    def set_frc(self, value: str) -> None:
        self.paused = value == "1"

    def set_psm(self, now: float, value: str) -> None:
        # "0" is auto, sent once at Home Assistant start; the car stays as it is.
        if value == "0":
            return
        self.pending_three_phase = value == "2"
        self.interrupted_until = now + PHASE_SWITCH_INTERRUPTION_S
        self.draw = 0.0

    @property
    def interrupted(self) -> bool:
        return self.pending_three_phase is not None

    def advance(self, now: float, dt: float) -> None:
        if self.pending_three_phase is not None:
            if now < self.interrupted_until:
                return
            self.three_phase = self.pending_three_phase
            self.pending_three_phase = None
            self.ramp_rate = max(self.amp - CHARGER_SHORTFALL_A, 0.0) / PHASE_SWITCH_RAMP_S
        target = 0.0 if self.paused else self.amp - CHARGER_SHORTFALL_A
        if self.draw > target:
            self.draw = max(target, self.draw - CHARGER_DOWN_RATE_A_PER_S * dt)
        elif self.draw < target:
            self.draw = min(target, self.draw + self.ramp_rate * dt)
        if self.draw >= target:
            self.ramp_rate = CHARGER_UP_RATE_A_PER_S

    def per_phase(self) -> list[float]:
        if self.three_phase:
            return [self.draw] * 3
        return [self.draw, 0.0, 0.0]


@dataclass
class Meter:
    """Grid meter read by Home Assistant.

    A telegram samples the total phase current every ``telegram_s``. Home
    Assistant first sees it ``delay_s`` after the telegram and reads it again
    every ``poll_s`` until the next telegram arrives (spec 2.2: telegrams every
    10 s, value 2.2-6.3 s old on arrival, dongle polled every 5 s).

    With ``seed`` set, each telegram gets its own delay drawn from
    ``delay_range`` and each poll interval varies by up to ``poll_jitter_s``,
    reproducibly.
    """

    delay_s: float = 4.0
    telegram_s: float = 10.0
    poll_s: float = 5.0
    delay_range: tuple[float, float] | None = None
    poll_jitter_s: float = 0.0
    seed: int | None = None
    telegrams: list[tuple[float, float, list[float]]] = field(default_factory=list)
    next_telegram: float = 0.0
    next_poll: float = 0.0
    last_read_t: float | None = None

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def _poll_interval(self) -> float:
        return self.poll_s + self._rng.uniform(-self.poll_jitter_s, self.poll_jitter_s)

    def sample(self, now: float, total: list[float]) -> None:
        """Take a telegram when one is due."""
        if now + 1e-9 >= self.next_telegram:
            delay = self._rng.uniform(*self.delay_range) if self.delay_range else self.delay_s
            self.telegrams.append((now, delay, list(total)))
            self.next_telegram += self.telegram_s

    def report(self, now: float) -> list[float] | None:
        """Return the value Home Assistant reads at ``now``, or None between reads.

        A new telegram is read when it arrives; the same telegram is read again
        every poll interval until the next one arrives.
        """
        arrived = [(t, v) for t, d, v in self.telegrams if t + d <= now + 1e-9]
        if not arrived:
            return None
        latest_t, latest = arrived[-1]
        if latest_t != self.last_read_t:
            self.last_read_t = latest_t
            self.next_poll = now + self._poll_interval()
            return latest
        if now + 1e-9 >= self.next_poll:
            self.next_poll += self._poll_interval()
            return latest
        return None
