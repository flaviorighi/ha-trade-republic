"""Fixtures for the Trade Republic tests."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trade_republic.api import Portfolio, Position, TradeRepublicClient
from custom_components.trade_republic.const import CONF_PHONE, DOMAIN

PHONE = "+393331234567"
ACCOUNT_ID = "0123456789abcdef"

ASML = Position("NL0010273215", "ASML", 10.0, 80.0, 100.0)
SAP = Position("DE0007164600", "SAP", 2.0, None, 200.0)
PORTFOLIO = Portfolio(currency="EUR", cash=1000.0, positions={ASML.isin: ASML, SAP.isin: SAP})


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """A client that logs in and reads PORTFOLIO without reaching Trade Republic."""
    client = MagicMock(spec=TradeRepublicClient)
    client.phone = PHONE
    client.start_login.return_value = False
    client.account_id.return_value = ACCOUNT_ID
    client.fetch_portfolio.return_value = PORTFOLIO
    client.fetch_history.return_value = []
    with (
        patch("custom_components.trade_republic.create_client", return_value=client),
        patch("custom_components.trade_republic.config_flow.create_client", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A configured account."""
    return MockConfigEntry(domain=DOMAIN, title="Trade Republic", data={CONF_PHONE: PHONE}, unique_id=ACCOUNT_ID)
