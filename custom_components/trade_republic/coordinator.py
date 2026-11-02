"""Coordinator for the Trade Republic integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Portfolio, TradeRepublicAuthError, TradeRepublicClient, TradeRepublicConnectionError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type TradeRepublicConfigEntry = ConfigEntry[TradeRepublicCoordinator]


class TradeRepublicCoordinator(DataUpdateCoordinator[Portfolio]):
    """Reads the portfolio of one account."""

    config_entry: TradeRepublicConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TradeRepublicConfigEntry, client: TradeRepublicClient) -> None:
        minutes = entry.options.get(CONF_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes) if minutes else DEFAULT_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> Portfolio:
        try:
            return await self.hass.async_add_executor_job(self.client.fetch_portfolio, self.data)
        except TradeRepublicAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="session_expired") from err
        except TradeRepublicConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="update_failed", translation_placeholders={"error": str(err)}
            ) from err
