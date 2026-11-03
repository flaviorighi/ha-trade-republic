"""Tests for the dividend and interest sensors."""

from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.trade_republic.api import DividendOutlook, ExpectedDividend, Payment
from homeassistant.core import HomeAssistant

from .conftest import ASML, PORTFOLIO, SAP

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _payment(year: int, month: int, amount: float, isin: str | None = None, currency: str = "EUR") -> Payment:
    return Payment(datetime(year, month, 15, 10, tzinfo=UTC), isin, "ASML" if isin else "Interest", amount, currency)


WITH_DIVIDENDS = replace(
    PORTFOLIO,
    dividends=(
        _payment(2025, 6, 5.0, ASML.isin),
        _payment(2026, 3, 2.0, ASML.isin),
        _payment(2026, 9, 3.0, SAP.isin),
        # Other currencies stay out of the sums.
        _payment(2026, 10, 9.0, SAP.isin, currency="USD"),
    ),
    interest=(_payment(2025, 10, 1.5), _payment(2025, 12, 1.0), _payment(2026, 10, 2.25)),
    outlook={
        ASML.isin: DividendOutlook("EUR", 2.0, ExpectedDividend(date(2026, 11, 10), date(2026, 11, 20), 0.5)),
        SAP.isin: DividendOutlook("EUR", 2.5, ExpectedDividend(date(2026, 5, 4), date(2026, 5, 8), 2.5)),
    },
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _value(hass: HomeAssistant, entity_id: str) -> float | str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    try:
        return float(state.state)
    except ValueError:
        return state.state


async def test_received(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    freezer.move_to(NOW)
    mock_client.fetch_portfolio.return_value = WITH_DIVIDENDS
    await _setup(hass, mock_config_entry)

    assert _value(hass, "sensor.trade_republic_dividends_this_year") == 5.0
    assert _value(hass, "sensor.trade_republic_dividends_last_12_months") == 5.0
    assert _value(hass, "sensor.trade_republic_dividends_received") == 10.0
    assert _value(hass, "sensor.trade_republic_interest_this_year") == 2.25
    assert _value(hass, "sensor.trade_republic_interest_last_12_months") == 3.25
    assert _value(hass, "sensor.trade_republic_interest_received") == 4.75

    total = hass.states.get("sensor.trade_republic_dividends_received").attributes
    assert total["by_year"] == {"2025": 5.0, "2026": 5.0}
    assert total["payments"] == 3
    assert (total["last_date"], total["last_name"], total["last_amount"]) == ("2026-09-15", "ASML", 3.0)
    this_year = hass.states.get("sensor.trade_republic_dividends_this_year").attributes
    assert this_year["state_class"] == "total"
    assert this_year["last_reset"].startswith("2026-01-01T00:00:00")

    # Per security: what it paid, what it is expected to pay in a year and its next dividend.
    asml = hass.states.get("sensor.trade_republic_asml").attributes
    assert asml["dividends_received"] == 7.0
    assert asml["expected_annual_dividend"] == 20.0
    assert (asml["next_dividend_date"], asml["next_dividend"]) == ("2026-11-20", 5.0)
    sap = hass.states.get("sensor.trade_republic_sap").attributes
    assert sap["dividends_received"] == 3.0
    # Its last dividend is paid already.
    assert (sap["next_dividend_date"], sap["next_dividend"]) == (None, None)


async def test_expected(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    freezer.move_to(NOW)
    mock_client.fetch_portfolio.return_value = WITH_DIVIDENDS
    await _setup(hass, mock_config_entry)

    # 10 shares of ASML at 0.50 each.
    assert _value(hass, "sensor.trade_republic_next_dividend") == 5.0
    next_dividend = hass.states.get("sensor.trade_republic_next_dividend").attributes
    assert next_dividend["name"] == "ASML"
    assert (next_dividend["ex_date"], next_dividend["payment_date"]) == ("2026-11-10", "2026-11-20")
    assert len(next_dividend["upcoming"]) == 1

    # 10 ASML at 2.00 a year and 2 SAP at 2.50.
    assert _value(hass, "sensor.trade_republic_expected_dividends_per_year") == 25.0
    expected = hass.states.get("sensor.trade_republic_expected_dividends_per_year").attributes
    assert expected["by_security"] == {"ASML": 20.0, "SAP": 5.0}


async def test_unknown_until_the_timeline_is_read(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    for entity_id in (
        "sensor.trade_republic_dividends_this_year",
        "sensor.trade_republic_dividends_received",
        "sensor.trade_republic_interest_received",
        "sensor.trade_republic_next_dividend",
        "sensor.trade_republic_expected_dividends_per_year",
    ):
        assert _value(hass, entity_id) == "unknown", entity_id
    assert hass.states.get("sensor.trade_republic_asml").attributes["dividends_received"] is None


async def test_diagnostics_count_payments_only(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_client.fetch_portfolio.return_value = WITH_DIVIDENDS
    await _setup(hass, mock_config_entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)
    portfolio = diagnostics["portfolio"]
    assert (portfolio["dividend_payments"], portfolio["interest_payments"]) == (4, 3)
    assert portfolio["positions_with_dividend_outlook"] == 2
    assert ASML.isin not in str(diagnostics)
