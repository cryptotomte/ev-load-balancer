"""Tester för command_dispatcher.py.

Täcker:
- send_amp anropar number.set_value med rätt entity_id och value
- send_frc anropar select.select_option med rätt entity_id och option
- pause() skickar frc='1'
- resume(amp) skickar frc='0' följt av amp
- Felhantering loggar ERROR men kastar inte undantag
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ev_load_balancer.command_dispatcher import CommandDispatcher

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CHARGER_ENTITIES = {
    "amp": "number.goe_409787_amp",
    "frc": "select.goe_409787_frc",
    "psm": "select.goe_409787_psm",
}


@pytest.fixture
def mock_hass():
    """Mockad HomeAssistant-instans med async_call."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    return hass


@pytest.fixture
def dispatcher(mock_hass) -> CommandDispatcher:
    """CommandDispatcher med mockad hass och standardentiteter."""
    return CommandDispatcher(mock_hass, CHARGER_ENTITIES)


# ---------------------------------------------------------------------------
# send_amp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_amp_calls_number_set_value(dispatcher, mock_hass):
    """send_amp ska anropa number.set_value med rätt entity_id och value."""
    await dispatcher.send_amp(12)

    mock_hass.services.async_call.assert_called_once_with(
        "number",
        "set_value",
        {"entity_id": "number.goe_409787_amp", "value": 12},
        blocking=False,
    )


@pytest.mark.asyncio
async def test_send_amp_with_different_values(dispatcher, mock_hass):
    """send_amp ska hantera olika amp-värden korrekt."""
    await dispatcher.send_amp(6)
    mock_hass.services.async_call.assert_called_with(
        "number",
        "set_value",
        {"entity_id": "number.goe_409787_amp", "value": 6},
        blocking=False,
    )

    await dispatcher.send_amp(16)
    mock_hass.services.async_call.assert_called_with(
        "number",
        "set_value",
        {"entity_id": "number.goe_409787_amp", "value": 16},
        blocking=False,
    )


# ---------------------------------------------------------------------------
# send_frc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_frc_calls_select_option(dispatcher, mock_hass):
    """send_frc ska anropa select.select_option med rätt entity_id och option."""
    await dispatcher.send_frc("0")

    mock_hass.services.async_call.assert_called_once_with(
        "select",
        "select_option",
        {"entity_id": "select.goe_409787_frc", "option": "0"},
        blocking=False,
    )


@pytest.mark.asyncio
async def test_send_frc_pause_value(dispatcher, mock_hass):
    """send_frc('1') ska skicka paus-kommando."""
    await dispatcher.send_frc("1")

    mock_hass.services.async_call.assert_called_once_with(
        "select",
        "select_option",
        {"entity_id": "select.goe_409787_frc", "option": "1"},
        blocking=False,
    )


# ---------------------------------------------------------------------------
# pause()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_sends_frc_one(dispatcher, mock_hass):
    """pause() ska skicka frc='1' till laddarens frc-entitet."""
    await dispatcher.pause()

    mock_hass.services.async_call.assert_called_once_with(
        "select",
        "select_option",
        {"entity_id": "select.goe_409787_frc", "option": "1"},
        blocking=False,
    )


# ---------------------------------------------------------------------------
# resume()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_sends_frc_zero_then_amp(dispatcher, mock_hass):
    """resume(amp) ska skicka frc='0' följt av amp-kommando."""
    await dispatcher.resume(10)

    # Kontrollera att async_call anropades 2 gånger (frc + amp)
    assert mock_hass.services.async_call.call_count == 2

    # Första anropet: frc='0' (blocking=True för att säkra ordning)
    first_call = mock_hass.services.async_call.call_args_list[0]
    assert first_call.args[0] == "select"
    assert first_call.args[1] == "select_option"
    assert first_call.args[2] == {"entity_id": "select.goe_409787_frc", "option": "0"}
    assert first_call.kwargs["blocking"] is True

    # Andra anropet: amp
    second_call = mock_hass.services.async_call.call_args_list[1]
    assert second_call.args[0] == "number"
    assert second_call.args[1] == "set_value"
    assert second_call.args[2] == {"entity_id": "number.goe_409787_amp", "value": 10}
    assert second_call.kwargs["blocking"] is False


@pytest.mark.asyncio
async def test_resume_correct_order(dispatcher, mock_hass):
    """resume() ska alltid skicka frc='0' INNAN amp."""
    calls = []

    async def track_call(domain, service, data=None, **kwargs):
        calls.append((domain, service, data))

    mock_hass.services.async_call = track_call

    await dispatcher.resume(12)

    assert len(calls) == 2
    # Första: frc='0'
    assert calls[0][0] == "select"
    assert calls[0][1] == "select_option"
    assert calls[0][2]["option"] == "0"
    # Andra: amp
    assert calls[1][0] == "number"
    assert calls[1][1] == "set_value"
    assert calls[1][2]["value"] == 12


# ---------------------------------------------------------------------------
# Felhantering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_rollback_when_amp_fails_after_frc_succeeds(mock_hass):
    """resume() ska återpausa (frc='1') och returnera False om amp misslyckas efter frc='0'.

    Scenario: send_frc('0') lyckas, send_amp() kastar undantag →
    rollback-anrop send_frc('1') ska göras och resume() ska returnera False.
    """
    call_log: list[tuple] = []

    async def fake_async_call(domain, service, data=None, **kwargs):
        call_log.append((domain, service, data))
        # frc-anrop lyckas alltid, amp-anrop misslyckas
        if domain == "number":
            raise Exception("Nätverksfel på amp")

    mock_hass.services.async_call = fake_async_call
    dispatcher = CommandDispatcher(mock_hass, CHARGER_ENTITIES)

    result = await dispatcher.resume(10)

    # resume() ska returnera False
    assert result is False

    # Tre anrop ska ha gjorts: frc='0', amp (misslyckas), frc='1' (rollback)
    assert len(call_log) == 3

    # Första: frc='0'
    assert call_log[0][0] == "select"
    assert call_log[0][2]["option"] == "0"

    # Andra: amp (misslyckat — men async_call anropades)
    assert call_log[1][0] == "number"
    assert call_log[1][2]["value"] == 10

    # Tredje: rollback frc='1'
    assert call_log[2][0] == "select"
    assert call_log[2][2]["option"] == "1"


@pytest.mark.asyncio
async def test_send_amp_logs_error_but_does_not_raise(mock_hass):
    """send_amp ska logga ERROR men inte kasta undantag vid fel."""
    mock_hass.services.async_call = AsyncMock(side_effect=Exception("Nätverksfel"))
    dispatcher = CommandDispatcher(mock_hass, CHARGER_ENTITIES)

    # Ska inte kasta undantag
    with patch("custom_components.ev_load_balancer.command_dispatcher._LOGGER") as mock_logger:
        await dispatcher.send_amp(10)  # Ska inte kasta
        mock_logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_send_frc_logs_error_but_does_not_raise(mock_hass):
    """send_frc ska logga ERROR men inte kasta undantag vid fel."""
    mock_hass.services.async_call = AsyncMock(side_effect=RuntimeError("HA-tjänst ej tillgänglig"))
    dispatcher = CommandDispatcher(mock_hass, CHARGER_ENTITIES)

    with patch("custom_components.ev_load_balancer.command_dispatcher._LOGGER") as mock_logger:
        await dispatcher.send_frc("1")  # Ska inte kasta
        mock_logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_send_amp_without_entity_logs_warning(mock_hass):
    """send_amp ska logga WARNING och returnera direkt om ingen amp-entitet finns."""
    dispatcher = CommandDispatcher(mock_hass, {})  # Inga entiteter

    with patch("custom_components.ev_load_balancer.command_dispatcher._LOGGER") as mock_logger:
        await dispatcher.send_amp(10)
        mock_logger.warning.assert_called_once()

    # async_call ska inte anropas
    mock_hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_send_frc_without_entity_logs_warning(mock_hass):
    """send_frc ska logga WARNING och returnera direkt om ingen frc-entitet finns."""
    dispatcher = CommandDispatcher(mock_hass, {})  # Inga entiteter

    with patch("custom_components.ev_load_balancer.command_dispatcher._LOGGER") as mock_logger:
        await dispatcher.send_frc("0")
        mock_logger.warning.assert_called_once()

    mock_hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# send_psm (PR-06)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_psm_1phase_calls_select_option(dispatcher, mock_hass):
    """send_psm('1') ska anropa select.select_option med psm-entitet och option='1'."""
    result = await dispatcher.send_psm("1")

    assert result is True
    mock_hass.services.async_call.assert_called_once_with(
        "select",
        "select_option",
        {"entity_id": "select.goe_409787_psm", "option": "1"},
        blocking=False,
    )


@pytest.mark.asyncio
async def test_send_psm_3phase_calls_select_option(dispatcher, mock_hass):
    """send_psm('2') ska anropa select.select_option med psm-entitet och option='2'."""
    result = await dispatcher.send_psm("2")

    assert result is True
    mock_hass.services.async_call.assert_called_once_with(
        "select",
        "select_option",
        {"entity_id": "select.goe_409787_psm", "option": "2"},
        blocking=False,
    )


@pytest.mark.asyncio
async def test_send_psm_returns_false_without_psm_entity(mock_hass):
    """send_psm ska returnera False och logga WARNING om ingen psm-entitet finns."""
    dispatcher = CommandDispatcher(mock_hass, {"amp": "number.goe_409787_amp"})

    with patch("custom_components.ev_load_balancer.command_dispatcher._LOGGER") as mock_logger:
        result = await dispatcher.send_psm("1")
        assert result is False
        mock_logger.warning.assert_called_once()

    mock_hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_send_psm_logs_error_but_does_not_raise(mock_hass):
    """send_psm ska logga ERROR men inte kasta undantag vid fel."""
    mock_hass.services.async_call = AsyncMock(side_effect=Exception("Nätverksfel"))
    dispatcher = CommandDispatcher(mock_hass, CHARGER_ENTITIES)

    with patch("custom_components.ev_load_balancer.command_dispatcher._LOGGER") as mock_logger:
        result = await dispatcher.send_psm("1")
        assert result is False
        mock_logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_send_psm_returns_true_on_success(dispatcher, mock_hass):
    """send_psm ska returnera True vid framgångsrikt anrop."""
    result = await dispatcher.send_psm("2")
    assert result is True
