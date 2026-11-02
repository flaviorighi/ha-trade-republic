"""Sensors for the Trade Republic integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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

from .api import Portfolio, Position
from .const import DOMAIN
from .coordinator import TradeRepublicConfigEntry, TradeRepublicCoordinator

# Every entity reads the coordinator's data, nothing is fetched per entity.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TradeRepublicSensorDescription(SensorEntityDescription):
    """An account sensor and how to read its value."""

    value_fn: Callable[[Portfolio], float | None]


def _money(key: str, value_fn: Callable[[Portfolio], float | None]) -> TradeRepublicSensorDescription:
    return TradeRepublicSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=value_fn,
    )


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
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.device_class == SensorDeviceClass.MONETARY:
            return self.coordinator.data.currency
        return super().native_unit_of_measurement


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
        return position.value if (position := self._position) else None

    @property
    def native_unit_of_measurement(self) -> str:
        return self.coordinator.data.currency

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (position := self._position) is None:
            return None
        return {
            "isin": position.isin,
            "quantity": round(position.quantity, 6),
            "average_price": _round(position.average_price, 4),
            "price": round(position.price, 4),
            "invested": _round(position.invested, 2),
            "gain": _round(position.gain, 2),
            "gain_percent": _round(position.gain_percent, 2),
        }


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
