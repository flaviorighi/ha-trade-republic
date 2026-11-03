"""Sensors for the Trade Republic integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import Payment, Portfolio, Position
from .const import DOMAIN
from .coordinator import TradeRepublicConfigEntry, TradeRepublicCoordinator

# Every entity reads the coordinator's data, nothing is fetched per entity.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TradeRepublicSensorDescription(SensorEntityDescription):
    """An account sensor and how to read its value."""

    value_fn: Callable[[Portfolio], float | None]
    attributes_fn: Callable[[Portfolio], dict[str, Any] | None] | None = None
    last_reset_fn: Callable[[], datetime] | None = None


def _money(
    key: str,
    value_fn: Callable[[Portfolio], float | None],
    state_class: SensorStateClass | None = SensorStateClass.TOTAL,
    **kwargs: Any,
) -> TradeRepublicSensorDescription:
    return TradeRepublicSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.MONETARY,
        state_class=state_class,
        suggested_display_precision=2,
        value_fn=value_fn,
        **kwargs,
    )


def _start_of_year() -> datetime:
    return dt_util.start_of_local_day(dt_util.now().date().replace(month=1, day=1))


def _year_ago() -> datetime:
    return dt_util.now() - timedelta(days=365)


def _total_attributes(portfolio: Portfolio, payments: tuple[Payment, ...] | None) -> dict[str, Any] | None:
    if payments is None:
        return None
    mine = sorted((p for p in payments if p.currency == portfolio.currency), key=lambda p: p.time)
    by_year: dict[str, float] = {}
    for payment in mine:
        year = str(dt_util.as_local(payment.time).year)
        by_year[year] = round(by_year.get(year, 0) + payment.amount, 2)
    last = mine[-1] if mine else None
    return {
        "payments": len(mine),
        "by_year": by_year,
        "last_date": dt_util.as_local(last.time).date().isoformat() if last else None,
        "last_name": last.name if last else None,
        "last_amount": round(last.amount, 2) if last else None,
    }


def _received_sensors(
    kind: str, payments_fn: Callable[[Portfolio], tuple[Payment, ...] | None]
) -> tuple[TradeRepublicSensorDescription, ...]:
    """Net amounts credited this year, in the last 12 months and since the account was opened."""
    return (
        _money(
            f"{kind}_year",
            lambda p: p.received(payments_fn(p), _start_of_year()),
            last_reset_fn=_start_of_year,
        ),
        _money(f"{kind}_12_months", lambda p: p.received(payments_fn(p), _year_ago()), state_class=None),
        _money(
            f"{kind}_total",
            lambda p: p.received(payments_fn(p)),
            attributes_fn=lambda p: _total_attributes(p, payments_fn(p)),
        ),
    )


def _upcoming(portfolio: Portfolio) -> list[dict[str, Any]]:
    return [
        {
            "name": position.name,
            "isin": position.isin,
            "ex_date": dividend.ex_date.isoformat(),
            "payment_date": dividend.payment_date.isoformat(),
            "per_share": round(dividend.per_share, 4),
            "quantity": round(position.quantity, 6),
            "amount": round(position.quantity * dividend.per_share, 2),
        }
        for position, dividend in portfolio.upcoming_dividends(dt_util.now().date())
    ]


def _next_dividend(portfolio: Portfolio) -> float | None:
    upcoming = _upcoming(portfolio)
    return upcoming[0]["amount"] if upcoming else None


def _next_dividend_attributes(portfolio: Portfolio) -> dict[str, Any]:
    upcoming = _upcoming(portfolio)
    first = upcoming[0] if upcoming else {}
    return {
        "name": first.get("name"),
        "ex_date": first.get("ex_date"),
        "payment_date": first.get("payment_date"),
        "per_share": first.get("per_share"),
        "quantity": first.get("quantity"),
        "upcoming": upcoming,
    }


def _expected_attributes(portfolio: Portfolio) -> dict[str, Any]:
    by_security = {
        position.name: round(value, 2)
        for isin, position in portfolio.positions.items()
        if (value := portfolio.expected_annual_dividend(isin))
    }
    return {"by_security": dict(sorted(by_security.items(), key=lambda item: -item[1]))}


SENSORS: tuple[TradeRepublicSensorDescription, ...] = (
    _money("total_value", lambda p: p.total_value),
    _money("securities_value", lambda p: p.securities_value),
    _money("cash", lambda p: p.cash),
    _money("invested", lambda p: p.invested),
    _money("gain", lambda p: p.gain),
    TradeRepublicSensorDescription(
        key="gain_percent",
        translation_key="gain_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p: p.gain_percent,
    ),
    *_received_sensors("dividends", lambda p: p.dividends),
    *_received_sensors("interest", lambda p: p.interest),
    # Estimates from the dividends announced or expected, before taxes.
    _money("next_dividend", _next_dividend, state_class=None, attributes_fn=_next_dividend_attributes),
    _money(
        "expected_dividends",
        lambda p: p.expected_annual_dividends,
        state_class=None,
        attributes_fn=_expected_attributes,
    ),
)


def _position_unique_id(entry: TradeRepublicConfigEntry, isin: str) -> str:
    return f"{entry.unique_id}_position_{isin}"


async def async_setup_entry(
    hass: HomeAssistant, entry: TradeRepublicConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add account sensors, and a sensor for every position as it appears."""
    coordinator = entry.runtime_data
    async_add_entities(TradeRepublicSensor(coordinator, description) for description in SENSORS)

    # Positions sold since the last start are gone for good.
    registry = er.async_get(hass)
    current = {_position_unique_id(entry, isin) for isin in coordinator.data.positions}
    prefix = _position_unique_id(entry, "")
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id.startswith(prefix) and registry_entry.unique_id not in current:
            registry.async_remove(registry_entry.entity_id)

    known: set[str] = set()

    @callback
    def add_new_positions() -> None:
        new = [isin for isin in coordinator.data.positions if isin not in known]
        known.update(new)
        if new:
            async_add_entities(PositionSensor(coordinator, isin) for isin in new)

    add_new_positions()
    entry.async_on_unload(coordinator.async_add_listener(add_new_positions))


class TradeRepublicEntity(CoordinatorEntity[TradeRepublicCoordinator]):
    """An entity of the account's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TradeRepublicCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.config_entry.unique_id))},
            name="Trade Republic",
            manufacturer="Trade Republic Bank GmbH",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://app.traderepublic.com",
        )


class TradeRepublicSensor(TradeRepublicEntity, SensorEntity):
    """A value of the whole account."""

    entity_description: TradeRepublicSensorDescription

    def __init__(self, coordinator: TradeRepublicCoordinator, description: TradeRepublicSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{description.key}"

    @property
    def native_value(self) -> float | None:
        return _round(self.entity_description.value_fn(self.coordinator.data), 2)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.device_class == SensorDeviceClass.MONETARY:
            return self.coordinator.data.currency
        return super().native_unit_of_measurement

    @property
    def last_reset(self) -> datetime | None:
        if (last_reset_fn := self.entity_description.last_reset_fn) is None:
            return None
        return last_reset_fn()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (attributes_fn := self.entity_description.attributes_fn) is None:
            return None
        return attributes_fn(self.coordinator.data)


class PositionSensor(TradeRepublicEntity, SensorEntity):
    """The market value of one security, with its details as attributes."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_translation_key = "position"

    def __init__(self, coordinator: TradeRepublicCoordinator, isin: str) -> None:
        super().__init__(coordinator)
        self._isin = isin
        self._attr_unique_id = _position_unique_id(coordinator.config_entry, isin)
        self._attr_name = coordinator.data.positions[isin].name

    @property
    def _position(self) -> Position | None:
        return self.coordinator.data.positions.get(self._isin)

    @property
    def available(self) -> bool:
        return super().available and self._position is not None

    @property
    def native_value(self) -> float | None:
        return round(position.value, 2) if (position := self._position) else None

    @property
    def native_unit_of_measurement(self) -> str:
        return self.coordinator.data.currency

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (position := self._position) is None:
            return None
        portfolio = self.coordinator.data
        received = None
        if portfolio.dividends is not None:
            received = portfolio.received(tuple(p for p in portfolio.dividends if p.isin == position.isin))
        outlook = portfolio.outlook_of(position.isin)
        upcoming = (
            outlook.next if outlook and outlook.next and outlook.next.payment_date >= dt_util.now().date() else None
        )
        return {
            "isin": position.isin,
            "quantity": round(position.quantity, 6),
            "average_price": _round(position.average_price, 4),
            "price": round(position.price, 4),
            "invested": _round(position.invested, 2),
            "gain": _round(position.gain, 2),
            "gain_percent": _round(position.gain_percent, 2),
            "dividends_received": _round(received, 2),
            "expected_annual_dividend": _round(portfolio.expected_annual_dividend(position.isin), 2),
            "next_dividend_date": upcoming.payment_date.isoformat() if upcoming else None,
            "next_dividend": round(position.quantity * upcoming.per_share, 2) if upcoming else None,
        }


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
