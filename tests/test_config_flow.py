"""Tests for the Trade Republic config flow."""

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trade_republic.api import (
    TooManyAttemptsError,
    TradeRepublicAuthError,
    TradeRepublicConnectionError,
)
from custom_components.trade_republic.const import CONF_PHONE, DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_CODE, CONF_PIN, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import ACCOUNT_ID, PHONE


async def _start(hass: HomeAssistant, phone: str = "+39 333 123 4567", pin: str = "1234"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PHONE: phone, CONF_PIN: pin})


async def _through_progress(hass: HomeAssistant, result):
    """Let the confirmation in the app finish, then continue the flow."""
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_login_confirmed_in_app(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "confirm_in_app"

    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Trade Republic"
    # The PIN is used for the login only and never stored.
    assert result["data"] == {CONF_PHONE: PHONE}
    assert result["result"].unique_id == ACCOUNT_ID
    mock_client.start_login.assert_called_once_with("1234")
    mock_client.wait_for_confirmation.assert_called_once()


async def test_login_with_authenticator_code(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.start_login.return_value = True
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authenticator"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_client.complete_with_code.assert_called_once_with("123456")


async def test_wrong_authenticator_code_starts_again(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.start_login.return_value = True
    mock_client.complete_with_code.side_effect = TradeRepublicAuthError
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "000000"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_code"}


async def test_invalid_phone(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass, phone="333 1234567")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PHONE: "invalid_phone"}
    mock_client.start_login.assert_not_called()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (TradeRepublicAuthError, "invalid_auth"),
        (TooManyAttemptsError, "too_many_attempts"),
        (TradeRepublicConnectionError, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_login_errors(hass: HomeAssistant, mock_client: MagicMock, error: type[Exception], key: str) -> None:
    mock_client.start_login.side_effect = error
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    mock_client.start_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PHONE: PHONE, CONF_PIN: "1234"})
    result = await _through_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_login_not_confirmed(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.wait_for_confirmation.side_effect = TradeRepublicAuthError
    result = await _through_progress(hass, await _start(hass))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "not_confirmed"}


async def test_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_client.start_login.assert_not_called()


async def test_reauth(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: "1234"})
    result = await _through_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {CONF_PHONE: PHONE}


async def test_reauth_other_account(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_client.account_id.return_value = "another"
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: "1234"})
    result = await _through_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_options(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 60})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_SCAN_INTERVAL: 60}
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.update_interval.total_seconds() == 3600
