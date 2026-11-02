"""Tests for importing the portfolio curve into the statistics."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.trade_republic.api import HistoryPoint, TradeRepublicConnectionError
from custom_components.trade_republic.const import CONF_HISTORY_IMPORTED, CONF_PHONE, DOMAIN
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .conftest import ACCOUNT_ID, PHONE

SECURITIES = "sensor.trade_republic_securities_value"
INVESTED = "sensor.trade_republic_invested"
GAIN = "sensor.trade_republic_unrealized_gain"
TOTAL = "sensor.trade_republic_total_value"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Recorder, enable_custom_integrations: None) -> None:
    """The recorder's database has to exist before Home Assistant starts."""


@pytest.fixture
def hour():
    return dt_util.utcnow().replace(minute=0, second=0, microsecond=0)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    # The import runs as a background task of the entry.
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _statistics(hass: HomeAssistant, start) -> dict:
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {SECURITIES, INVESTED, GAIN, TOTAL}, "hour", None, {"state", "sum"}
    )


def _rows(statistics: dict, entity_id: str) -> list[tuple]:
    return [
        (dt_util.utc_from_timestamp(row["start"]), row["state"], row["sum"]) for row in statistics.get(entity_id, [])
    ]


async def test_imports_matching_series(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    hour,
) -> None:
    # Today: securities 1400, invested 800, gain 200 (SAP has no purchase price and stays out of the gain).
    mock_client.fetch_history.return_value = [
        HistoryPoint(hour - timedelta(days=2), 1000.0, 100.0),
        HistoryPoint(hour - timedelta(days=1) + timedelta(minutes=20), 1200.0, 150.0),
        # This hour is left to Home Assistant.
        HistoryPoint(hour + timedelta(minutes=5), 1400.0, 600.0),
    ]
    await _setup(hass, mock_config_entry)
    mock_client.fetch_history.assert_called_once_with("EUR")

    statistics = await _statistics(hass, hour - timedelta(days=3))
    # Sums end where Home Assistant's own statistics will start, at today's value.
    assert _rows(statistics, SECURITIES) == [
        (hour - timedelta(days=2), 1000.0, -400.0),
        (hour - timedelta(days=1), 1200.0, -200.0),
    ]
    # Securities minus performance agrees with today's invested capital...
    assert _rows(statistics, INVESTED) == [
        (hour - timedelta(days=2), 900.0, 100.0),
        (hour - timedelta(days=1), 1050.0, 250.0),
    ]
    # ...while the performance itself is not today's unrealized gain, and the value is not the total.
    assert _rows(statistics, GAIN) == []
    assert _rows(statistics, TOTAL) == []
    assert mock_config_entry.data[CONF_HISTORY_IMPORTED] is True


async def test_stops_at_existing_statistics(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    hour,
) -> None:
    async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=SECURITIES,
            unit_class=None,
            unit_of_measurement="EUR",
        ),
        [{"start": hour - timedelta(days=1), "state": 1300.0, "sum": 5.0}],
    )
    await async_wait_recording_done(hass)
    mock_client.fetch_history.return_value = [
        HistoryPoint(hour - timedelta(days=2), 1000.0, None),
        HistoryPoint(hour - timedelta(days=1), 1250.0, None),
        HistoryPoint(hour, 1400.0, None),
    ]
    await _setup(hass, mock_config_entry)

    assert _rows(await _statistics(hass, hour - timedelta(days=3)), SECURITIES) == [
        (hour - timedelta(days=2), 1000.0, -295.0),
        (hour - timedelta(days=1), 1300.0, 5.0),
    ]


async def test_imports_once(recorder_mock: Recorder, hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PHONE: PHONE, CONF_HISTORY_IMPORTED: True},
        unique_id=ACCOUNT_ID,
        minor_version=2,
    )
    await _setup(hass, entry)
    mock_client.fetch_history.assert_not_called()


async def test_retries_after_a_failure(
    recorder_mock: Recorder, hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_client.fetch_history.side_effect = TradeRepublicConnectionError("down")
    await _setup(hass, mock_config_entry)
    assert CONF_HISTORY_IMPORTED not in mock_config_entry.data


async def test_retries_when_nothing_matches(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    hour,
) -> None:
    mock_client.fetch_history.return_value = [HistoryPoint(hour - timedelta(days=1), 9000.0, None)]
    await _setup(hass, mock_config_entry)
    assert CONF_HISTORY_IMPORTED not in mock_config_entry.data
    assert _rows(await _statistics(hass, hour - timedelta(days=3)), SECURITIES) == []
