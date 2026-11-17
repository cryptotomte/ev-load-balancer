"""Every form the integration shows must survive HA's frontend serialization.

HA's flow HTTP views convert each form's data_schema to a field list for the
frontend and JSON-encode the response. A validator the converter cannot handle
(e.g. a plain function) makes the view raise and the UI shows "500 Internal
Server Error". Tests that call the flow manager directly never reach that
conversion, so these tests drive every form through the HTTP views and follow
whatever converter the installed HA uses.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.ev_load_balancer.const import DOMAIN

_FLOW = "/api/config/config_entries/flow"
_OPTIONS_FLOW = "/api/config/config_entries/options/flow"

SENSOR_L1 = "sensor.current_be1_30051"
SENSOR_L2 = "sensor.current_be1_30052"

CHARGER_ENTITIES = {
    "amp": "number.goe_409787_amp",
    "frc": "select.goe_409787_frc",
    "psm": "select.goe_409787_psm",
    "car_value": "sensor.goe_409787_car_value",
    "nrg_4": "sensor.goe_409787_nrg_4",
    "nrg_5": "sensor.goe_409787_nrg_5",
    "nrg_6": "sensor.goe_409787_nrg_6",
    "map": "sensor.goe_409787_map",
}
DUPLICATE_PHASES = {
    "phase_1_sensor": SENSOR_L1,
    "phase_1_max_ampere": 25,
    "phase_2_sensor": SENSOR_L1,
    "phase_2_max_ampere": 25,
}
VALID_PHASES = {
    "phase_1_sensor": SENSOR_L1,
    "phase_1_max_ampere": 25,
    "phase_2_sensor": SENSOR_L2,
    "phase_2_max_ampere": 25,
}
MAX_LESS_THAN_MIN = {
    "safety_margin": 2,
    "min_current": 16,
    "max_current": 6,
    "phase_count": "auto",
}


@pytest.fixture
async def client(hass: HomeAssistant, hass_client: ClientSessionGenerator):
    assert await async_setup_component(hass, "config", {})
    return await hass_client()


async def _post(client, path: str, payload: dict, step_id: str) -> dict:
    """POST to a flow view and expect the form ``step_id`` back with HTTP 200."""
    resp = await client.post(path, json=payload)
    assert resp.status == HTTPStatus.OK, await resp.text()
    result = await resp.json()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == step_id
    return result


async def test_config_flow_forms_serializable(hass: HomeAssistant, client) -> None:
    """user, serial, entities, phases, params and confirm, plus re-shown error forms."""
    result = await _post(client, _FLOW, {"handler": DOMAIN}, "user")
    step = f"{_FLOW}/{result['flow_id']}"

    await _post(client, step, {"profile_id": "goe_gemini"}, "serial")
    result = await _post(client, step, {"serial": " "}, "serial")
    assert result["errors"] == {"serial": "empty_serial"}

    await _post(client, step, {"serial": "409787"}, "entities")
    await _post(client, step, CHARGER_ENTITIES, "phases")

    result = await _post(client, step, DUPLICATE_PHASES, "phases")
    assert result["errors"] == {"base": "duplicate_phase_sensors"}

    await _post(client, step, VALID_PHASES, "params")
    result = await _post(client, step, MAX_LESS_THAN_MIN, "params")
    assert result["errors"] == {"max_current": "max_less_than_min"}

    await _post(
        client,
        step,
        {"safety_margin": 2, "min_current": 6, "max_current": 16, "phase_count": "auto"},
        "confirm",
    )


async def test_config_flow_generic_entities_form_serializable(hass: HomeAssistant, client) -> None:
    """The generic profile builds the entities form without pre-filled defaults."""
    result = await _post(client, _FLOW, {"handler": DOMAIN}, "user")
    await _post(client, f"{_FLOW}/{result['flow_id']}", {"profile_id": "generic"}, "entities")


async def test_options_flow_forms_serializable(
    hass: HomeAssistant, client, mock_config_entry
) -> None:
    """phases and params, each also re-shown with an error."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await _post(client, _OPTIONS_FLOW, {"handler": mock_config_entry.entry_id}, "phases")
    step = f"{_OPTIONS_FLOW}/{result['flow_id']}"

    result = await _post(client, step, DUPLICATE_PHASES, "phases")
    assert result["errors"] == {"base": "duplicate_phase_sensors"}

    await _post(client, step, VALID_PHASES, "params")
    result = await _post(
        client,
        step,
        {
            **MAX_LESS_THAN_MIN,
            "action_on_sensor_loss": "reduce",
            "safe_default_current": 6,
            "capacity_warning_threshold": 3,
        },
        "params",
    )
    assert result["errors"] == {"max_current": "max_less_than_min"}
