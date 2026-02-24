"""Tester för charger_profiles.py — profildefinitioner och resolve()."""

import pytest

from custom_components.ev_load_balancer.charger_profiles import PROFILES

# ---------------------------------------------------------------------------
# T018: resolve() returnerar korrekta entity-ID:n
# ---------------------------------------------------------------------------


def test_goe_gemini_resolve_returns_correct_entity_ids():
    """resolve('409787') ska ge korrekta entity-ID:n för amp, frc och nrg_4."""
    result = PROFILES["goe_gemini"].resolve("409787")

    assert result["amp"] == "number.goe_409787_amp"
    assert result["frc"] == "select.goe_409787_frc"
    assert result["nrg_4"] == "sensor.goe_409787_nrg_4"


# ---------------------------------------------------------------------------
# T019: frc och psm måste vara select-typ (säkerhetskritiskt)
# ---------------------------------------------------------------------------


def test_goe_gemini_frc_psm_are_select_type():
    """frc och psm MÅSTE vara platform='select' — aldrig 'number' (Princip III)."""
    controls = PROFILES["goe_gemini"].controls

    assert controls["frc"].platform == "select"
    assert controls["psm"].platform == "select"


# ---------------------------------------------------------------------------
# T020: ama måste ha flash=True
# ---------------------------------------------------------------------------


def test_goe_gemini_ama_is_flash():
    """ama MÅSTE ha flash=True — skyddas av datamodellen (Princip I)."""
    assert PROFILES["goe_gemini"].controls["ama"].flash is True


# ---------------------------------------------------------------------------
# T021: Alla 8 kapabiliteter ska finnas
# ---------------------------------------------------------------------------


def test_goe_gemini_has_all_8_capabilities():
    """goe_gemini ska ha exakt de 8 definierade kapabiliteterna."""
    expected = {
        "per_phase_current",
        "per_phase_power",
        "phase_detection",
        "dynamic_current",
        "pause_resume",
        "phase_switching",
        "car_status",
        "min_current_sensor",
    }
    assert expected == PROFILES["goe_gemini"].capabilities


# ---------------------------------------------------------------------------
# T022: car_value ska ha rätt allowed_values
# ---------------------------------------------------------------------------


def test_goe_gemini_car_value_has_allowed_values():
    """car_value.allowed_values ska vara exakt ['Idle', 'Charging', 'WaitCar', 'Complete']."""
    assert PROFILES["goe_gemini"].sensors["car_value"].allowed_values == [
        "Idle",
        "Charging",
        "WaitCar",
        "Complete",
    ]


# ---------------------------------------------------------------------------
# T023: resolve() kastar ValueError för tom sträng
# ---------------------------------------------------------------------------


def test_resolve_raises_on_empty_serial():
    """resolve('') ska kasta ValueError."""
    with pytest.raises(ValueError):
        PROFILES["goe_gemini"].resolve("")


# ---------------------------------------------------------------------------
# T024: goe_gemini ska ha 11 sensorer och 4 kontroller
# ---------------------------------------------------------------------------


def test_goe_gemini_has_11_sensors_and_4_controls():
    """goe_gemini ska ha exakt 11 sensorer och 4 kontroller."""
    profile = PROFILES["goe_gemini"]

    assert len(profile.sensors) == 11
    assert len(profile.controls) == 4


# ---------------------------------------------------------------------------
# T026 (US3): generic-profilen ska existera som stub
# ---------------------------------------------------------------------------


def test_generic_profile_exists_as_stub():
    """generic ska finnas i PROFILES med requires_serial=False och tomma samlingar."""
    assert "generic" in PROFILES
    assert PROFILES["generic"].requires_serial is False
    assert len(PROFILES["generic"].sensors) == 0
    assert len(PROFILES["generic"].controls) == 0
