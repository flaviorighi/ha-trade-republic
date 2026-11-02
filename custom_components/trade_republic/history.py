"""Import the curve of the Trade Republic app into the statistics of the sensors.

Home Assistant keeps statistics only from the moment the integration is set up, while the app
shows the portfolio since the first purchase. On the first start the curve is read once and
written as hourly statistics, before the first statistic Home Assistant recorded itself.

Which sensor a series belongs to is not assumed: the last point of the curve has to agree with
today's value of that sensor, otherwise the series is left out.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .api import HistoryPoint, Portfolio, TradeRepublicAuthError, TradeRepublicConnectionError
from .const import CONF_HISTORY_IMPORTED, DOMAIN
from .coordinator import TradeRepublicConfigEntry

_LOGGER = logging.getLogger(__name__)

# How far the last point of the curve may be from today's value, as a share of the securities.
TOLERANCE = 0.01

type Series = dict[str, tuple[float, list[tuple[datetime, float]]]]


def _series(portfolio: Portfolio, points: list[HistoryPoint]) -> Series:
    """The series that match today's values: sensor key -> (today's value, hourly values)."""
    last = points[-1]
    # The curve's value is either the securities alone or with cash: the closer one decides.
    if abs(last.net_value - portfolio.securities_value) <= abs(last.net_value - portfolio.total_value):
        net_key, net_today = "securities_value", portfolio.securities_value
    else:
        net_key, net_today = "total_value", portfolio.total_value
    candidates: dict[str, tuple[float, Callable[[HistoryPoint], float | None]]] = {
        net_key: (net_today, lambda p: p.net_value),
        "gain": (portfolio.gain, lambda p: p.gain),
        "invested": (portfolio.invested, lambda p: None if p.gain is None else p.net_value - p.gain),
    }
    scale = max(portfolio.securities_value, 1.0)
    series: Series = {}
    for key, (today, value_of) in candidates.items():
        if (last_value := value_of(last)) is None or abs(last_value - today) > TOLERANCE * scale:
            _LOGGER.debug("Portfolio history does not match %s: %s against %s", key, last_value, today)
            continue
        series[key] = (
            today,
            [
                (p.time.replace(minute=0, second=0, microsecond=0), value)
                for p in points
                if (value := value_of(p)) is not None
            ],
        )
    return series


async def _async_import(
    hass: HomeAssistant, entity_id: str, currency: str, today: float, values: list[tuple[datetime, float]]
) -> int:
    """Write the values older than the sensor's first statistic; returns how many."""
    existing = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, values[0][0], None, {entity_id}, "hour", None, {"state", "sum"}
    )
    if rows := existing.get(entity_id):
        # Sums continue into the statistics Home Assistant already has.
        until = dt_util.utc_from_timestamp(rows[0]["start"])
        anchor_state, anchor_sum = rows[0].get("state") or 0.0, rows[0].get("sum") or 0.0
    else:
        until = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        anchor_state, anchor_sum = today, 0.0
    statistics = [
        StatisticData(start=time, state=value, sum=anchor_sum + value - anchor_state)
        for time, value in values
        if time < until
    ]
    if statistics:
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=None,
            unit_of_measurement=currency,
        )
        async_import_statistics(hass, metadata, statistics)
    return len(statistics)


async def async_import_history(hass: HomeAssistant, entry: TradeRepublicConfigEntry) -> None:
    """Import the curve once per account; without a usable curve, try again at the next start."""
    coordinator = entry.runtime_data
    currency = coordinator.data.currency
    try:
        points = await hass.async_add_executor_job(coordinator.client.fetch_history, currency)
    except (TradeRepublicAuthError, TradeRepublicConnectionError) as err:
        _LOGGER.warning("Could not read the portfolio history, trying again at the next start: %s", err)
        return
    if not points or not (series := _series(coordinator.data, points)):
        _LOGGER.warning(
            "The portfolio history of Trade Republic does not match today's values, trying again at the next start"
        )
        return

    imported: dict[str, int] = {}
    registry = er.async_get(hass)
    for key, (today, values) in series.items():
        if entity_id := registry.async_get_entity_id(Platform.SENSOR, DOMAIN, f"{entry.unique_id}_{key}"):
            imported[entity_id] = await _async_import(hass, entity_id, currency, today, values)
    _LOGGER.info("Portfolio history since %s imported: %s", points[0].time.date(), imported)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HISTORY_IMPORTED: True})
