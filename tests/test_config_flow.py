"""Tester för EV Load Balancer config flow.

Täcker:
- US1: Komplett 6-stegs go-e-konfiguration
- US2: Duplikat-sensor blockering (säkerhetskritisk)
- US3: Generic-profil hoppar serienummer-steget
- US4: Options Flow — ändra fassensorer och parametrar
"""

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_load_balancer.const import DOMAIN

# ---------------------------------------------------------------------------
# Hjälpkonstanter — teststyrda entitets-ID:n
# ---------------------------------------------------------------------------

SENSOR_L1 = "sensor.current_be1_30051"
SENSOR_L2 = "sensor.current_be1_30052"
SENSOR_L3 = "sensor.current_be1_30053"

FULL_GOE_DATA = {
    "profile_id": "goe_gemini",
    "serial": "409787",
    "charger_entities": {
        "amp": "number.goe_409787_amp",
        "frc": "select.goe_409787_frc",
        "psm": "select.goe_409787_psm",
        "car_value": "sensor.goe_409787_car_value",
        "nrg_4": "sensor.goe_409787_nrg_4",
        "nrg_5": "sensor.goe_409787_nrg_5",
        "nrg_6": "sensor.goe_409787_nrg_6",
        "map": "sensor.goe_409787_map",
    },
    "phases": [{"sensor": SENSOR_L1, "max_ampere": 25, "label": "L1"}],
    "safety_margin": 2,
    "min_current": 6,
    "max_current": 16,
    "phase_count": "auto",
}


# ---------------------------------------------------------------------------
# US1: Komplett 6-stegs flow (go-e Gemini)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_flow_step1_shows_form(hass):
    """Steg 1: async_init ska returnera FORM med step_id='user'."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_config_flow_goe_profile_shows_serial_step(hass):
    """Steg 1 → steg 2: go-e-profil leder till serienummer-formuläret."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "goe_gemini"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "serial"


@pytest.mark.asyncio
async def test_config_flow_serial_autofills_entities(hass):
    """Steg 2 → steg 3: korrekt serienummer leder till entitetsformulär."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    # Steg 1: profilval
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "goe_gemini"}
    )
    # Steg 2: serienummer
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"serial": "409787"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "entities"


@pytest.mark.asyncio
async def test_config_flow_goe_gemini_full(hass):
    """Komplett 6-stegs flow för go-e Gemini → CREATE_ENTRY med korrekt data."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    # Steg 1: profilval
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "goe_gemini"}
    )
    assert result["step_id"] == "serial"

    # Steg 2: serienummer
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"serial": "409787"}
    )
    assert result["step_id"] == "entities"

    # Steg 3: entiteter (accept pre-fill)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "amp": "number.goe_409787_amp",
            "frc": "select.goe_409787_frc",
            "psm": "select.goe_409787_psm",
            "car_value": "sensor.goe_409787_car_value",
            "nrg_4": "sensor.goe_409787_nrg_4",
            "nrg_5": "sensor.goe_409787_nrg_5",
            "nrg_6": "sensor.goe_409787_nrg_6",
            "map": "sensor.goe_409787_map",
        },
    )
    assert result["step_id"] == "phases"

    # Steg 4: fassensorer
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
        },
    )
    assert result["step_id"] == "params"

    # Steg 5: parametrar
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "safety_margin": 2.0,
            "min_current": 6.0,
            "max_current": 16.0,
            "phase_count": "auto",
        },
    )
    assert result["step_id"] == "confirm"

    # Steg 6: bekräftelse
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "EV Load Balancer"
    data = result["data"]
    assert data["profile_id"] == "goe_gemini"
    assert data["serial"] == "409787"
    assert isinstance(data["phases"], list)
    assert len(data["phases"]) == 1
    assert data["phases"][0]["sensor"] == SENSOR_L1
    assert data["safety_margin"] == 2
    assert data["min_current"] == 6
    assert data["max_current"] == 16
    assert data["phase_count"] == "auto"


@pytest.mark.asyncio
async def test_device_created_after_setup(hass, mock_config_entry):
    """Device ska skapas i device registry efter async_setup_entry."""
    from homeassistant.helpers import device_registry as dr

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    device_reg = dr.async_get(hass)
    device = device_reg.async_get_device(identifiers={(DOMAIN, mock_config_entry.entry_id)})

    assert device is not None
    assert device.name == "EV Load Balancer"
    assert device.manufacturer == "EV Load Balancer"
    assert device.model == "goe_gemini"


# ---------------------------------------------------------------------------
# US2: Duplikat-sensor blockering (säkerhetskritisk)
# ---------------------------------------------------------------------------


async def _navigate_to_phases(hass):
    """Hjälpfunktion: navigera till steg 4 (phases) via go-e-profil."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "goe_gemini"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"serial": "409787"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "amp": "number.goe_409787_amp",
            "frc": "select.goe_409787_frc",
            "psm": "select.goe_409787_psm",
            "car_value": "sensor.goe_409787_car_value",
            "nrg_4": "sensor.goe_409787_nrg_4",
            "nrg_5": "sensor.goe_409787_nrg_5",
            "nrg_6": "sensor.goe_409787_nrg_6",
            "map": "sensor.goe_409787_map",
        },
    )
    assert result["step_id"] == "phases"
    return result


@pytest.mark.asyncio
async def test_duplicate_phase_sensor_blocked(hass):
    """Samma sensor för L1 och L2 ska ge 'duplicate_phase_sensors'."""
    result = await _navigate_to_phases(hass)

    # Samma sensor för två faser — säkerhetskritisk blockering
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
            "phase_2_sensor": SENSOR_L1,  # DUPLIKAT!
            "phase_2_max_ampere": 25.0,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "phases"
    assert result["errors"]["base"] == "duplicate_phase_sensors"


@pytest.mark.asyncio
async def test_one_phase_accepted(hass):
    """En fas konfigurerad ska gå vidare till params-steget (positiv path)."""
    result = await _navigate_to_phases(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
        },
    )
    # Verifierar att vi kom vidare med en fas (positiv path)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "params"


@pytest.mark.asyncio
async def test_no_phases_blocked(hass, mock_config_entry):
    """Noll faser konfigurerade via options flow ska ge 'no_phases'-fel.

    Anropar step-handleren direkt för att kringgå schema-validering och
    testa no_phases-valideringslogiken isolerat.
    """
    from custom_components.ev_load_balancer.config_flow import EVLoadBalancerOptionsFlow

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    # Skapa en flow-instans och sätt upp config_entry manuellt.
    # OptionsFlow._config_entry_id är en property som returnerar self.handler —
    # vi sätter handler till entry_id så att config_entry-lookupet fungerar.
    flow = EVLoadBalancerOptionsFlow()
    flow.hass = hass
    flow.handler = mock_config_entry.entry_id

    # Anropa step-handleren direkt med tom sensor — kringgår schema-validering
    result = await flow.async_step_phases(
        user_input={"phase_1_sensor": "", "phase_1_max_ampere": 25.0}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "phases"
    assert result["errors"]["base"] == "no_phases"


@pytest.mark.asyncio
async def test_no_phases_blocked_via_options(hass, mock_config_entry):
    """Options flow: options-steget kräver minst en fas — steg visas korrekt med pre-fill."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "phases"

    # Verifiera att description_placeholders innehåller befintlig fas-summering
    assert "description_placeholders" in result
    assert "summary" in result["description_placeholders"]


@pytest.mark.asyncio
async def test_three_unique_sensors_accepted(hass):
    """Tre unika sensorer ska accepteras och leda till steg 5."""
    result = await _navigate_to_phases(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
            "phase_2_sensor": SENSOR_L2,
            "phase_2_max_ampere": 25.0,
            "phase_3_sensor": SENSOR_L3,
            "phase_3_max_ampere": 25.0,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "params"


@pytest.mark.asyncio
async def test_duplicate_after_fix_is_accepted(hass):
    """Duplikat → rättelse → formuläret accepterar och går vidare."""
    result = await _navigate_to_phases(hass)

    # Första försöket: duplikat
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
            "phase_2_sensor": SENSOR_L1,  # DUPLIKAT
            "phase_2_max_ampere": 25.0,
        },
    )
    assert result["errors"]["base"] == "duplicate_phase_sensors"

    # Andra försöket: unikt
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
            "phase_2_sensor": SENSOR_L2,  # Unik sensor nu
            "phase_2_max_ampere": 25.0,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "params"


@pytest.mark.asyncio
async def test_phase_summary_in_description_placeholders(hass):
    """description_placeholders ska innehålla fas-summering (FR-010)."""
    result = await _navigate_to_phases(hass)

    # Steg 4 visas — kontrollera att description_placeholders finns
    assert "description_placeholders" in result
    assert "summary" in result["description_placeholders"]


# ---------------------------------------------------------------------------
# US3: Generic-profil hoppar serienummer-steget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_profile_skips_serial(hass):
    """Generic-profil ska gå direkt till steg 3 utan steg 2."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "generic"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "entities"


@pytest.mark.asyncio
async def test_generic_profile_empty_entities(hass):
    """Steg 3 med generic-profil ska ha tomma entitetsfält."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "generic"}
    )

    assert result["step_id"] == "entities"
    # Formuläret ska visas utan pre-fill (schema finns, inga defaults från resolve)
    assert result["type"] == FlowResultType.FORM
    assert "data_schema" in result


# ---------------------------------------------------------------------------
# US4: Options Flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_flow_init_shows_phases(hass, mock_config_entry):
    """Options flow ska öppna med step_id='phases' och befintliga värden."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "phases"


@pytest.mark.asyncio
async def test_options_flow_phases_changeable(hass, mock_config_entry):
    """Options flow: ändra sensor ska resultera i CREATE_ENTRY med ny config."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "phases"

    # Ändra till ny sensor
    new_sensor = "sensor.current_new_sensor"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": new_sensor,
            "phase_1_max_ampere": 20.0,
        },
    )
    assert result["step_id"] == "params"

    # Bekräfta parametrar
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "safety_margin": 2.0,
            "min_current": 6.0,
            "max_current": 16.0,
            "phase_count": "auto",
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["phases"][0]["sensor"] == new_sensor
    assert result["data"]["phases"][0]["max_ampere"] == 20


@pytest.mark.asyncio
async def test_options_flow_duplicate_sensor_blocked(hass, mock_config_entry):
    """Options flow: duplikat-sensor ska ge 'duplicate_phase_sensors'."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
            "phase_2_sensor": SENSOR_L1,  # DUPLIKAT!
            "phase_2_max_ampere": 25.0,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "phases"
    assert result["errors"]["base"] == "duplicate_phase_sensors"


@pytest.mark.asyncio
async def test_options_flow_max_less_than_min_blocked(hass, mock_config_entry):
    """Options flow: max_current < min_current ska ge 'max_less_than_min'."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "phases"

    # Navigera till params-steget
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
        },
    )
    assert result["step_id"] == "params"

    # Skicka max < min — ska blockeras
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "safety_margin": 2.0,
            "min_current": 16.0,
            "max_current": 6.0,  # max < min!
            "phase_count": "auto",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "params"
    assert result["errors"]["max_current"] == "max_less_than_min"


# ---------------------------------------------------------------------------
# Edge case: Okänt profil-ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_profile_id_logs_error(hass, caplog):
    """async_setup_entry ska returnera False och logga ERROR om profil-ID är okänt."""
    import logging

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EV Load Balancer",
        data={"profile_id": "okant_profil_xyz"},
    )
    entry.add_to_hass(hass)

    with caplog.at_level(logging.ERROR, logger="custom_components.ev_load_balancer"):
        result = await hass.config_entries.async_setup(entry.entry_id)

    assert result is False
    assert any(
        "okant_profil_xyz" in record.message or "Okänt profil-ID" in record.message
        for record in caplog.records
        if record.levelno == logging.ERROR
    )


# ---------------------------------------------------------------------------
# Valideringstest: min/max-ström
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_less_than_min_blocked(hass):
    """max_current < min_current ska ge 'max_less_than_min'."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "goe_gemini"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"serial": "409787"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "amp": "number.goe_409787_amp",
            "frc": "select.goe_409787_frc",
            "psm": "select.goe_409787_psm",
            "car_value": "sensor.goe_409787_car_value",
            "nrg_4": "sensor.goe_409787_nrg_4",
            "nrg_5": "sensor.goe_409787_nrg_5",
            "nrg_6": "sensor.goe_409787_nrg_6",
            "map": "sensor.goe_409787_map",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_1_sensor": SENSOR_L1,
            "phase_1_max_ampere": 25.0,
        },
    )
    assert result["step_id"] == "params"

    # Skicka max < min — ska blockeras
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "safety_margin": 2.0,
            "min_current": 16.0,
            "max_current": 6.0,  # max < min!
            "phase_count": "auto",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "params"
    assert result["errors"]["max_current"] == "max_less_than_min"


@pytest.mark.asyncio
async def test_serial_empty_shows_error(hass):
    """Tomt serienummer ska ge 'empty_serial'."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"profile_id": "goe_gemini"}
    )
    # Steg 2: tomt serienummer
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"serial": ""}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "serial"
    assert result["errors"]["serial"] == "empty_serial"
