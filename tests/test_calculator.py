"""Tester för beräkningsmotorn (calculator.py).

Täcker PRD §9 Scenario 1 (3-fas, 14A) och Scenario 2 (1-fas L2, 8A),
map-fallback, klämning och edge cases.
"""

from custom_components.ev_load_balancer.calculator import CalculationResult, calculate

# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

# 3-fas konfiguration: L1 (max 25A), L2 (max 25A), L3 (max 25A)
PHASES_3 = [
    {"sensor": "sensor.current_l1", "max_ampere": 25, "label": "L1"},
    {"sensor": "sensor.current_l2", "max_ampere": 25, "label": "L2"},
    {"sensor": "sensor.current_l3", "max_ampere": 25, "label": "L3"},
]

# 3-fas konfiguration för scenario 2: L1 (25A), L2 (16A), L3 (25A)
PHASES_3_S2 = [
    {"sensor": "sensor.current_l1", "max_ampere": 25, "label": "L1"},
    {"sensor": "sensor.current_l2", "max_ampere": 16, "label": "L2"},
    {"sensor": "sensor.current_l3", "max_ampere": 25, "label": "L3"},
]


# ---------------------------------------------------------------------------
# PRD §9 Scenario 1: 3-fas, target = 14A
# ---------------------------------------------------------------------------


def test_scenario1_3phase_target_14a():
    """PRD §9 Scenario 1: 3-fas → target_current = 14A.

    Konfiguration:
        L1 max=25A, L2 max=25A, L3 max=25A
        safety_margin=2A, min=6A, max=16A
        map=[1,2,3]

    Sensorvärden:
        L1=18.3A, L2=12.1A, L3=15.7A (inkl. laddaren)
        nrg_4=10A, nrg_5=10A, nrg_6=10A

    Beräkning:
        available_l1 = 25 - (18.3 - 10) - 2 = 14.7A
        available_l2 = 25 - (12.1 - 10) - 2 = 20.9A
        available_l3 = 25 - (15.7 - 10) - 2 = 17.3A
        available_min = min(14.7, 20.9, 17.3) = 14.7A
        target = floor(14.7) = 14A
    """
    result = calculate(
        phases=PHASES_3,
        phase_values=[18.3, 12.1, 15.7],
        device_values=[10.0, 10.0, 10.0],
        active_phase_numbers=[1, 2, 3],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert result.target_current == 14
    assert abs(result.available_l1 - 14.7) < 0.001
    assert abs(result.available_l2 - 20.9) < 0.001
    assert abs(result.available_l3 - 17.3) < 0.001
    assert abs(result.available_min - 14.7) < 0.001
    assert result.active_phases == [1, 2, 3]
    assert result.charging_mode == "3-phase"
    assert isinstance(result, CalculationResult)


# ---------------------------------------------------------------------------
# PRD §9 Scenario 2: 1-fas L2, target = 8A
# ---------------------------------------------------------------------------


def test_scenario2_1phase_l2_target_8a():
    """Spec SC-003 / Scenario 2 (modifierat från PRD §9 med L2 max=16A).

    1-fas L2 → target_current = 8A.

    Konfiguration:
        L1 max=25A, L2 max=16A, L3 max=25A
        safety_margin=2A, min=6A, max=16A
        map=[2] (bara L2 aktiv)

    Sensorvärden:
        L2=6A (inkl. laddaren)
        nrg_5=0A (1-fas, rapporterar 0A)

    Beräkning:
        available_l2 = 16 - (6 - 0) - 2 = 8A
        available_min = min(8) = 8A
        target = floor(8) = 8A
    """
    result = calculate(
        phases=PHASES_3_S2,
        phase_values=[0.0, 6.0, 0.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[2],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert result.target_current == 8
    assert abs(result.available_l2 - 8.0) < 0.001
    assert abs(result.available_min - 8.0) < 0.001
    assert result.active_phases == [2]
    assert result.charging_mode == "1-phase"


# ---------------------------------------------------------------------------
# Map unavailable → fallback till alla konfigurerade faser
# ---------------------------------------------------------------------------


def test_map_unavailable_fallback_all_phases():
    """Om active_phase_numbers är tom lista används alla konfigurerade faser som fallback.

    Testar calculators inbyggda fallback-logik: när active_phase_numbers=[] används
    alla konfigurerade faser. Koordinatorn skickar denna lista om map-sensorn saknas.
    """
    # L1: 25 - (10 - 0) - 2 = 13
    # L2: 25 - (8 - 0) - 2 = 15
    # L3: 25 - (12 - 0) - 2 = 11
    # available_min = 11A → target = 11A
    result = calculate(
        phases=PHASES_3,
        phase_values=[10.0, 8.0, 12.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[],  # fallback: alla faser
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    # Alla 3 faser används som fallback
    assert result.target_current == 11
    assert result.active_phases == []


# ---------------------------------------------------------------------------
# Klämning: resultat < min_current
# ---------------------------------------------------------------------------


def test_result_clamped_to_min_current():
    """Om beräknat värde < min_current kläms target till min_current."""
    # available_l1 = 25 - (24 - 0) - 2 = -1A → floor(-1) = -1 → kläm till 6
    result = calculate(
        phases=PHASES_3,
        phase_values=[24.0, 24.0, 24.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[1, 2, 3],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert result.target_current == 6
    assert result.available_l1 < 0


# ---------------------------------------------------------------------------
# Klämning: resultat > max_current
# ---------------------------------------------------------------------------


def test_result_clamped_to_max_current():
    """Om beräknat värde > max_current kläms target till max_current."""
    # available = 25 - (0 - 0) - 0 = 25A → kläm till 16
    result = calculate(
        phases=PHASES_3,
        phase_values=[0.0, 0.0, 0.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[1, 2, 3],
        safety_margin=0.0,
        min_current=6,
        max_current=16,
    )

    assert result.target_current == 16


# ---------------------------------------------------------------------------
# Negativt fassensorvärde (ovanligt men möjligt)
# ---------------------------------------------------------------------------


def test_negative_phase_sensor_value():
    """Negativt fassensorvärde hanteras korrekt (ger extra kapacitet)."""
    # available_l1 = 25 - (-2 - 0) - 2 = 25 + 2 - 2 = 25A → kläm till 16
    result = calculate(
        phases=[{"sensor": "s.l1", "max_ampere": 25, "label": "L1"}],
        phase_values=[-2.0],
        device_values=[0.0],
        active_phase_numbers=[1],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert result.target_current == 16  # kläms till max


# ---------------------------------------------------------------------------
# Alla faser = 0A (ingen last)
# ---------------------------------------------------------------------------


def test_all_phases_zero_load():
    """Alla fasser rapporterar 0A last — beräkning med full kapacitet."""
    # available = 25 - (0 - 0) - 2 = 23A → kläm till 16
    result = calculate(
        phases=PHASES_3,
        phase_values=[0.0, 0.0, 0.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[1, 2, 3],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert result.target_current == 16
    assert result.charging_mode == "3-phase"


# ---------------------------------------------------------------------------
# active_phases i resultatet
# ---------------------------------------------------------------------------


def test_active_phases_correctly_set_for_1phase():
    """active_phases ska spegla active_phase_numbers för 1-fas."""
    result = calculate(
        phases=PHASES_3,
        phase_values=[5.0, 5.0, 5.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[2],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert result.active_phases == [2]
    assert result.charging_mode == "1-phase"


def test_active_phases_correctly_set_for_3phase():
    """active_phases ska spegla active_phase_numbers för 3-fas."""
    result = calculate(
        phases=PHASES_3,
        phase_values=[5.0, 5.0, 5.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[1, 2, 3],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert result.active_phases == [1, 2, 3]
    assert result.charging_mode == "3-phase"


# ---------------------------------------------------------------------------
# US2: map=[2] 1-fas med explicit fasval
# ---------------------------------------------------------------------------


def test_map_phase2_only_uses_l2():
    """map=[2]: beräkning baseras bara på L2 — L1/L3 påverkar inte target."""
    # L1 har väldigt hög last men är ej aktiv
    # L2: 16 - (6 - 0) - 2 = 8A
    result = calculate(
        phases=PHASES_3_S2,
        phase_values=[24.0, 6.0, 24.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[2],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    # Bara L2 är aktiv — L1/L3 ignoreras för target
    assert result.target_current == 8
    assert result.active_phases == [2]
    assert result.charging_mode == "1-phase"


# ---------------------------------------------------------------------------
# Beräkningstid
# ---------------------------------------------------------------------------


def test_calculation_time_is_set():
    """calculation_time ska sättas automatiskt."""
    from datetime import datetime

    result = calculate(
        phases=PHASES_3,
        phase_values=[5.0, 5.0, 5.0],
        device_values=[0.0, 0.0, 0.0],
        active_phase_numbers=[1, 2, 3],
        safety_margin=2.0,
        min_current=6,
        max_current=16,
    )

    assert isinstance(result.calculation_time, datetime)
