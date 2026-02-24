"""Tester för LoadBalancerStateMachine (state_machine.py)."""

import pytest

from custom_components.ev_load_balancer.state_machine import (
    BalancerState,
    LoadBalancerStateMachine,
)

# ---------------------------------------------------------------------------
# Initialt tillstånd
# ---------------------------------------------------------------------------


def test_initial_state_is_initializing():
    """State machine ska börja i INITIALIZING."""
    sm = LoadBalancerStateMachine()
    assert sm.state == BalancerState.INITIALIZING


def test_state_is_string():
    """BalancerState ska vara en StrEnum med korrekt strängvärde."""
    assert BalancerState.INITIALIZING == "initializing"
    assert BalancerState.IDLE == "idle"
    assert BalancerState.BALANCING == "balancing"
    assert BalancerState.PAUSED == "paused"


# ---------------------------------------------------------------------------
# INITIALIZING → IDLE (kräver 2 lyckade beräkningar)
# ---------------------------------------------------------------------------


def test_first_calculation_stays_in_initializing():
    """Första lyckade beräkning — fortfarande INITIALIZING."""
    sm = LoadBalancerStateMachine()
    result = sm.record_successful_calculation()
    assert result is False
    assert sm.state == BalancerState.INITIALIZING


def test_second_calculation_transitions_to_idle():
    """Andra lyckade beräkning — övergår till IDLE."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    result = sm.record_successful_calculation()
    assert result is True
    assert sm.state == BalancerState.IDLE


def test_record_calculation_in_idle_returns_false():
    """record_successful_calculation() i IDLE ska returnera False (ignoreras)."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert sm.state == BalancerState.IDLE

    result = sm.record_successful_calculation()
    assert result is False
    assert sm.state == BalancerState.IDLE


def test_record_calculation_in_balancing_returns_false():
    """record_successful_calculation() i BALANCING ska returnera False."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert sm.state == BalancerState.BALANCING

    result = sm.record_successful_calculation()
    assert result is False


# ---------------------------------------------------------------------------
# IDLE → BALANCING
# ---------------------------------------------------------------------------


def test_car_connected_idle_to_balancing():
    """IDLE → BALANCING när bil ansluts."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert sm.state == BalancerState.IDLE

    result = sm.on_car_connected()
    assert result is True
    assert sm.state == BalancerState.BALANCING


def test_car_connected_already_balancing_returns_false():
    """on_car_connected() i BALANCING ska returnera False (bil redan ansluten)."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()

    result = sm.on_car_connected()
    assert result is False
    assert sm.state == BalancerState.BALANCING


def test_car_connected_in_initializing_raises_error():
    """on_car_connected() i INITIALIZING ska ge ValueError."""
    sm = LoadBalancerStateMachine()
    with pytest.raises(ValueError):
        sm.on_car_connected()


# ---------------------------------------------------------------------------
# BALANCING → IDLE
# ---------------------------------------------------------------------------


def test_car_disconnected_balancing_to_idle():
    """BALANCING → IDLE när bil kopplas bort."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert sm.state == BalancerState.BALANCING

    result = sm.on_car_disconnected()
    assert result is True
    assert sm.state == BalancerState.IDLE


def test_car_disconnected_in_idle_returns_false():
    """on_car_disconnected() i IDLE ska returnera False."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert sm.state == BalancerState.IDLE

    result = sm.on_car_disconnected()
    assert result is False
    assert sm.state == BalancerState.IDLE


def test_car_disconnected_in_initializing_raises_error():
    """on_car_disconnected() i INITIALIZING ska ge ValueError."""
    sm = LoadBalancerStateMachine()
    with pytest.raises(ValueError):
        sm.on_car_disconnected()


# ---------------------------------------------------------------------------
# BALANCING → PAUSED
# ---------------------------------------------------------------------------


def test_below_min_current_balancing_to_paused():
    """BALANCING → PAUSED vid kapacitetsbrist."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert sm.state == BalancerState.BALANCING

    result = sm.on_below_min_current()
    assert result is True
    assert sm.state == BalancerState.PAUSED


def test_below_min_current_already_paused_returns_false():
    """on_below_min_current() i PAUSED ska returnera False."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    assert sm.state == BalancerState.PAUSED

    result = sm.on_below_min_current()
    assert result is False
    assert sm.state == BalancerState.PAUSED


def test_below_min_current_in_idle_raises_error():
    """on_below_min_current() i IDLE ska ge ValueError."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert sm.state == BalancerState.IDLE

    with pytest.raises(ValueError):
        sm.on_below_min_current()


def test_below_min_current_in_initializing_raises_error():
    """on_below_min_current() i INITIALIZING ska ge ValueError."""
    sm = LoadBalancerStateMachine()
    with pytest.raises(ValueError):
        sm.on_below_min_current()


# ---------------------------------------------------------------------------
# PAUSED → BALANCING
# ---------------------------------------------------------------------------


def test_above_min_current_paused_to_balancing():
    """PAUSED → BALANCING när kapacitet återkommer."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    assert sm.state == BalancerState.PAUSED

    result = sm.on_above_min_current()
    assert result is True
    assert sm.state == BalancerState.BALANCING


def test_above_min_current_already_balancing_returns_false():
    """on_above_min_current() i BALANCING ska returnera False."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    assert sm.state == BalancerState.BALANCING

    result = sm.on_above_min_current()
    assert result is False
    assert sm.state == BalancerState.BALANCING


def test_above_min_current_in_idle_raises_error():
    """on_above_min_current() i IDLE ska ge ValueError."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert sm.state == BalancerState.IDLE

    with pytest.raises(ValueError):
        sm.on_above_min_current()


def test_above_min_current_in_initializing_raises_error():
    """on_above_min_current() i INITIALIZING ska ge ValueError."""
    sm = LoadBalancerStateMachine()
    with pytest.raises(ValueError):
        sm.on_above_min_current()


# ---------------------------------------------------------------------------
# PAUSED → IDLE (via bil bortkoppling)
# ---------------------------------------------------------------------------


def test_car_disconnected_paused_to_idle():
    """PAUSED → IDLE när bil kopplas bort."""
    sm = LoadBalancerStateMachine()
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    sm.on_car_connected()
    sm.on_below_min_current()
    assert sm.state == BalancerState.PAUSED

    result = sm.on_car_disconnected()
    assert result is True
    assert sm.state == BalancerState.IDLE


# ---------------------------------------------------------------------------
# Fullständiga flöden
# ---------------------------------------------------------------------------


def test_full_lifecycle():
    """Komplett livscykel: INITIALIZING → IDLE → BALANCING → PAUSED → BALANCING → IDLE."""
    sm = LoadBalancerStateMachine()
    assert sm.state == BalancerState.INITIALIZING

    # 2 lyckade beräkningar → IDLE
    sm.record_successful_calculation()
    sm.record_successful_calculation()
    assert sm.state == BalancerState.IDLE

    # Bil ansluts → BALANCING
    sm.on_car_connected()
    assert sm.state == BalancerState.BALANCING

    # Kapacitetsbrist → PAUSED
    sm.on_below_min_current()
    assert sm.state == BalancerState.PAUSED

    # Kapacitet åter → BALANCING
    sm.on_above_min_current()
    assert sm.state == BalancerState.BALANCING

    # Bil kopplas bort → IDLE
    sm.on_car_disconnected()
    assert sm.state == BalancerState.IDLE
