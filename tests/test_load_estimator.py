"""Unit tests for load_estimator.py (spec 010, K-1 and K-4).

Budgets below use the K-1 formula: max_ampere − h − safety_margin, with the
measured installation's 25 A per phase and 2 A margin (spec 2.2).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.ev_load_balancer.load_estimator import LoadEstimator

T0 = datetime(2026, 1, 1, 8, 0, 0)
MAX_A = 25.0
MARGIN = 2.0


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _budget(household: float) -> float:
    return MAX_A - household - MARGIN


def _estimator(window_s: float = 15, hold_s: float = 90) -> LoadEstimator:
    return LoadEstimator(phase_count=3, window_s=window_s, hold_s=hold_s)


def test_first_report_sets_household_from_current_charger_value():
    """The first report sets h = P − current charger current."""
    est = _estimator()
    est.record_device(_at(0), [15.5, 15.5, 15.5])

    result = est.on_phase_report(_at(1), 0, 23.5)

    assert result.household == pytest.approx(8.0)


def test_t12_not_tight_until_history_covers_window():
    """T-12: a steady charger gives low == high, but not tight before 15 s of history."""
    est = _estimator()
    est.record_device(_at(0), [10.0, 10.0, 10.0])

    early = est.on_phase_report(_at(14), 0, 18.0)
    late = est.on_phase_report(_at(15), 0, 18.0)

    assert early.low == early.high
    assert early.tight is False
    assert late.tight is True


def test_steady_charger_gives_exact_estimate():
    """With the charger unchanged in the window, h follows P exactly."""
    est = _estimator()
    est.record_device(_at(0), [12.5, 12.5, 12.5])
    est.on_phase_report(_at(20), 2, 21.7)

    result = est.on_phase_report(_at(30), 2, 24.6)

    assert result.household == pytest.approx(12.1)
    assert result.tight is True


def test_t10_charger_change_without_phase_report_does_not_move_household():
    """T-10: charger values only update the history; h moves on phase reports only."""
    est = _estimator()
    est.record_device(_at(0), [15.5, 15.5, 15.5])
    before = est.on_phase_report(_at(20), 0, 23.5)

    for second in range(21, 30):
        est.record_device(_at(second), [15.5 - (second - 20), 15.5, 15.5])

    assert est.estimates[0] == before


def test_stale_report_after_own_reduction_keeps_household():
    """An old P (taken before the reduction) is bounded by the window and keeps h."""
    est = _estimator()
    est.record_device(_at(0), [15.5, 15.5, 15.5])
    est.on_phase_report(_at(20), 0, 23.5)
    est.record_device(_at(22), [12.5, 12.5, 12.5])

    result = est.on_phase_report(_at(25), 0, 23.5)

    assert result.low == pytest.approx(8.0)
    assert result.high == pytest.approx(11.0)
    assert result.household == pytest.approx(8.0)
    assert result.tight is False


def test_proven_higher_load_raises_household_at_once():
    """When low exceeds h, h rises to low on that report (K-2)."""
    est = _estimator()
    est.record_device(_at(0), [15.5, 15.5, 15.5])
    est.on_phase_report(_at(20), 0, 23.5)
    est.record_device(_at(22), [12.5, 12.5, 12.5])

    result = est.on_phase_report(_at(25), 0, 29.5)

    assert result.household == pytest.approx(14.0)


def test_proven_lower_load_lowers_household():
    """When high is below h, h falls to high."""
    est = _estimator()
    est.record_device(_at(0), [15.5, 15.5, 15.5])
    est.on_phase_report(_at(20), 0, 23.5)
    est.record_device(_at(22), [12.5, 12.5, 12.5])

    result = est.on_phase_report(_at(25), 0, 17.5)

    assert result.household == pytest.approx(5.0)


def test_old_charger_value_leaves_window():
    """After W seconds without change the old value drops out and P is exact again."""
    est = _estimator()
    est.record_device(_at(0), [15.5, 15.5, 15.5])
    est.on_phase_report(_at(20), 0, 23.5)
    est.record_device(_at(22), [12.5, 12.5, 12.5])

    result = est.on_phase_report(_at(37), 0, 21.0)

    assert result.device_min == result.device_max == pytest.approx(12.5)
    assert result.household == pytest.approx(8.5)
    assert result.tight is True


def test_command_widens_window_without_changing_charger_value():
    """A sent command counts as a change in the window (P-2 guard) for W seconds."""
    est = _estimator()
    est.record_device(_at(0), [10.0, 10.0, 10.0])
    est.on_phase_report(_at(20), 0, 18.0)
    est.record_command(_at(21), [16.0, 16.0, 16.0])

    inside = est.on_phase_report(_at(25), 0, 18.0)
    after = est.on_phase_report(_at(37), 0, 18.0)

    assert inside.device_max == pytest.approx(16.0)
    assert inside.tight is False
    assert inside.household == pytest.approx(8.0)
    assert after.device_max == pytest.approx(10.0)
    assert after.tight is True


def test_t9_measured_moment_downswitch_0831():
    """T-9 / G-6: 08:31:28, P L1 = 24.0 A taken at 15.5 A, charger now at 6.57 A.

    Old formula: 25 − (24.0 − 6.57) − 2 = 5.6 A. Correct budget: 14.5 A.
    """
    est = _estimator()
    est.record_device(_at(0), [15.5, 15.5, 15.5])
    est.on_phase_report(_at(20), 0, 24.0)
    est.record_device(_at(22), [6.57, 6.57, 6.57])

    result = est.on_phase_report(_at(26), 0, 24.0)

    assert MAX_A - (24.0 - 6.57) - MARGIN == pytest.approx(5.6, abs=0.05)
    assert _budget(result.household) == pytest.approx(14.5)


def test_t9_measured_moment_upswitch_0832():
    """T-9 / G-6: 08:32:50, 1-phase at 12.4 A on L1, charger to 0 A for the switch.

    Old formula: 25 − (20.8 − 0) − 2 = 2.2 A. Correct budget: ca 14.6 A.
    """
    est = _estimator()
    est.record_device(_at(0), [12.4, 0.0, 0.0])
    est.on_phase_report(_at(20), 0, 20.8)
    est.record_device(_at(22), [0.0, 0.0, 0.0])

    result = est.on_phase_report(_at(25), 0, 20.8)

    assert MAX_A - 20.8 - MARGIN == pytest.approx(2.2)
    assert _budget(result.household) == pytest.approx(14.6)


def test_peak_holds_highest_household_for_hold_time():
    """peak() returns the highest h within the hold time, including the value at its start."""
    est = _estimator(hold_s=90)
    est.record_device(_at(0), [10.0, 10.0, 10.0])
    est.on_phase_report(_at(20), 1, 18.0)
    est.on_phase_report(_at(30), 1, 24.0)
    est.on_phase_report(_at(50), 1, 18.0)

    assert est.peak(_at(110))[1] == pytest.approx(14.0)
    assert est.peak(_at(141))[1] == pytest.approx(8.0)
    assert est.peak(_at(141))[0] is None


def test_substitute_charger_value_is_never_tight():
    """D-16 / R-8: a substitute charger value in the window makes no report unambiguous.

    A constant substitute (last sent amp) would otherwise look like a steady charger.
    """
    est = _estimator()
    est.record_device(_at(0), [10.0, 10.0, 10.0], substitute=[True, False, False])

    l1 = est.on_phase_report(_at(20), 0, 18.0)
    l2 = est.on_phase_report(_at(20), 1, 18.0)
    est.record_device(_at(21), [10.0, 10.0, 10.0])
    still = est.on_phase_report(_at(30), 0, 18.0)
    measured = est.on_phase_report(_at(37), 0, 18.0)

    assert l1.low == l1.high
    assert l1.tight is False
    assert l2.tight is True
    assert still.tight is False
    assert measured.tight is True


def test_first_report_uses_current_charger_value_after_change_in_window():
    """K-1 / 6.1: the first report sets h = P − current charger current, also when the

    charger changed inside the window: the charger fell on L1 and rose on L2.
    """
    est = _estimator()
    est.record_device(_at(0), [15.5, 6.5, 15.5])
    est.record_device(_at(10), [6.5, 15.5, 15.5])

    fell = est.on_phase_report(_at(12), 0, 20.0)
    rose = est.on_phase_report(_at(12), 1, 20.0)

    assert (fell.low, fell.high) == (pytest.approx(4.5), pytest.approx(13.5))
    assert fell.household == pytest.approx(13.5)
    assert (rose.low, rose.high) == (pytest.approx(4.5), pytest.approx(13.5))
    assert rose.household == pytest.approx(4.5)
