"""Diagnostics for the Trade Republic integration.

Diagnostics end up in public bug reports, so they say how the integration is doing without
amounts, securities or anything that identifies the account.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import TradeRepublicConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TradeRepublicConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    portfolio = coordinator.data
    return {
        "options": dict(entry.options),
        "update_interval": str(coordinator.update_interval),
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception) if coordinator.last_exception else None,
        "portfolio": None
        if portfolio is None
        else {
            "currency": portfolio.currency,
            "positions": len(portfolio.positions),
            "positions_without_average_price": sum(1 for p in portfolio.positions.values() if p.average_price is None),
        },
    }
