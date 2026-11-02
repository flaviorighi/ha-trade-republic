"""The Trade Republic integration."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pytr.tickers import SUPPORTED_LANGUAGES

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import TradeRepublicClient
from .const import CONF_HISTORY_IMPORTED, CONF_PHONE, DOMAIN
from .coordinator import TradeRepublicConfigEntry, TradeRepublicCoordinator
from .history import async_import_history

PLATFORMS = [Platform.SENSOR]


def create_client(hass: HomeAssistant, phone: str) -> TradeRepublicClient:
    """Client for one account, with its session stored under .storage."""
    # The file is named after a hash, so the phone number does not appear on disk.
    name = hashlib.sha256(phone.encode()).hexdigest()[:16]
    cookies_file = Path(hass.config.path(".storage", DOMAIN, f"{name}.cookies"))
    language = hass.config.language.split("-")[0]
    return TradeRepublicClient(phone, cookies_file, language if language in SUPPORTED_LANGUAGES else "en")


async def async_setup_entry(hass: HomeAssistant, entry: TradeRepublicConfigEntry) -> bool:
    """Set up Trade Republic from a config entry."""
    coordinator = TradeRepublicCoordinator(hass, entry, create_client(hass, entry.data[CONF_PHONE]))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if not entry.data.get(CONF_HISTORY_IMPORTED) and "recorder" in hass.config.components:
        entry.async_create_background_task(hass, async_import_history(hass, entry), "trade_republic_history")
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: TradeRepublicConfigEntry) -> bool:
    """Bring older config entries up to date."""
    if entry.version == 1 and entry.minor_version < 2:
        # Before 1.2 the history was marked as imported even when Trade Republic sent none.
        data = {key: value for key, value in entry.data.items() if key != CONF_HISTORY_IMPORTED}
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TradeRepublicConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TradeRepublicConfigEntry) -> None:
    """Delete the saved session when the account is removed."""
    await hass.async_add_executor_job(create_client(hass, entry.data[CONF_PHONE]).forget_session)
