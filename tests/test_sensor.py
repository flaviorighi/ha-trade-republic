"""Tests for setup, sensors and diagnostics."""

from dataclasses import replace
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.trade_republic.api import (
    Position,
    TradeRepublicAuthError,
    TradeRepublicConnectionError,
)
from custom_components.trade_republic.const import DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import ASML, PORTFOLIO, SAP


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_sensors(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    expected = {
        "sensor.trade_republic_total_value": 2400.0,
        "sensor.trade_republic_securities_value": 1400.0,
        "sensor.trade_republic_cash": 1000.0,
        # SAP has no purchase price and stays out of invested and gain.
        "sensor.trade_republic_invested": 800.0,
        "sensor.trade_republic_unrealized_gain": 200.0,
        "sensor.trade_republic_unrealized_return": 25.0,
    }
    for entity_id, value in expected.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert float(state.state) == value, entity_id
    assert hass.states.get("sensor.trade_republic_total_value").attributes["unit_of_measurement"] == "EUR"

    asml = hass.states.get("sensor.trade_republic_asml")
    assert float(asml.state) == 1000.0
    assert asml.attributes["isin"] == ASML.isin
    assert asml.attributes["quantity"] == 10.0
    assert asml.attributes["gain_percent"] == 25.0
    sap = hass.states.get("sensor.trade_republic_sap")
    assert sap.attributes["gain"] is None


async def test_positions_come_and_go(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    tesla = Position("US88160R1014", "Tesla", 1.0, 300.0, 400.0)
    mock_client.fetch_portfolio.return_value = replace(PORTFOLIO, positions={ASML.isin: ASML, tesla.isin: tesla})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert float(hass.states.get("sensor.trade_republic_tesla").state) == 400.0
    assert hass.states.get("sensor.trade_republic_sap").state == STATE_UNAVAILABLE

    # A sold position is removed at the next start.
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("sensor.trade_republic_sap") is None
    assert registry.async_get("sensor.trade_republic_tesla") is not None


async def test_expired_session_starts_reauth(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    mock_client.fetch_portfolio.side_effect = TradeRepublicAuthError
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert hass.states.get("sensor.trade_republic_total_value").state == STATE_UNAVAILABLE


async def test_setup_without_session(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_client.fetch_portfolio.side_effect = TradeRepublicAuthError
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in hass.config_entries.flow.async_progress())


async def test_setup_retries_when_offline(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_client.fetch_portfolio.side_effect = TradeRepublicConnectionError("down")
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_remove_deletes_session(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.forget_session.assert_called_once()


async def test_diagnostics_reveal_nothing_personal(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, mock_config_entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)
    assert diagnostics["portfolio"] == {"currency": "EUR", "positions": 2, "positions_without_average_price": 1}
    text = str(diagnostics)
    for secret in (ASML.isin, SAP.name, "2400", "+39"):
        assert secret not in text
