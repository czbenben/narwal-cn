"""Sensor entities for Narwal robot vacuum."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity, remove_stale_entity
from .narwal_client import NarwalState
from .narwal_client.error_codes import error_message_zh
from .narwal_client.models import STATUS_KEYS
from .narwal_client.proto import enum_labels

CONSUMABLE_NAMES = {
    "CONSUMABLE_MAINTAIN_ITEM_DUST_BOX": "Clean the dust bin",
    "CONSUMABLE_MAINTAIN_ITEM_DUST_FILTER": "Clean the dust bin filter",
    "CONSUMABLE_MAINTAIN_ITEM_DIRTY_WATER": "Empty the dirty water tank",
    "CONSUMABLE_MAINTAIN_ITEM_WASH_RIBS": "Clean the base station tray",
    "CONSUMABLE_MAINTAIN_ITEM_UNIVERSAL_WHEEL": "Clean the caster wheel",
    "CONSUMABLE_MAINTAIN_ITEM_CLIFF_SENSOR": "Clean the sensors",
    "CONSUMABLE_MAINTAIN_ITEM_SIDE_DISTANCE_SENSOR": "Clean the sensors",
    "CONSUMABLE_MAINTAIN_ITEM_ANTI_WINDING_BRUSH": "Clean the roller brush",
    "CONSUMABLE_MAINTAIN_ITEM_WATER_TANK_SPONGE": "Clean the water tank filter sponge",
    "CONSUMABLE_MAINTAIN_ITEM_SMART_MODULE_SPONGE": "Clean the filter sponge",
    "CONSUMABLE_MAINTAIN_ITEM_DUST_CONTAINER": "Empty the dust bin / dust bag",
    "CONSUMABLE_REPLACE_ITEM_DUST_FILTER": "Replace the dust bin filter",
    "CONSUMABLE_REPLACE_ITEM_MOP": "Replace the mops",
    "CONSUMABLE_REPLACE_ITEM_SIDE_BRUSH": "Replace the side brush",
    "CONSUMABLE_REPLACE_ITEM_ROLLER_BRUSH": "Replace the roller brush",
    "CONSUMABLE_REPLACE_ITEM_DETERGENT": "Refill the detergent",
    "CONSUMABLE_REPLACE_ITEM_DUST_BAG": "Replace the dust bag",
}


def _slug(label: Any, prefix: str) -> str | None:
    if not isinstance(label, str):
        return None
    if label.startswith(prefix):
        label = label[len(prefix):]
    return label.lower()


def _enum_options(enum: str, prefix: str) -> list[str]:
    return [s for v, label in sorted(enum_labels(enum).items()) if v and (s := _slug(label, prefix))]


@dataclass(frozen=True, kw_only=True)
class NarwalSensorDescription(SensorEntityDescription):
    value_fn: Callable[[NarwalState], Any]
    attrs_fn: Callable[[NarwalState], dict[str, Any]] | None = None
    exists_fn: Callable[[NarwalState], bool] = lambda _: True
    # Robot feature flag required for this entity (None = always create).
    feature: str | None = None
    # Keep the last non-zero reading when the task ends or across HA restarts.
    persist_last: bool = False


def _base_enum(
    key: str,
    field_name: str,
    enum: str,
    prefix: str,
    icon: str,
    value: Callable[[NarwalState], Any] | None = None,
) -> NarwalSensorDescription:
    if value is None:
        value = lambda s, f=field_name: s.base.get(f)
    return NarwalSensorDescription(
        key=key,
        translation_key=key,
        icon=icon,
        device_class=SensorDeviceClass.ENUM,
        options=_enum_options(enum, prefix),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s, v=value, p=prefix: _slug(v(s), p),
    )


def _error_attrs(state: NarwalState) -> dict[str, Any]:
    return {
        "codes": [f"0x{c:08X}" for c in state.error_codes],
        "messages": [error_message_zh(c) for c in state.error_codes],
        # Reported by the robot but not shown by the Narwal app (internal notices).
        "hidden_codes": [f"0x{c:08X}" for c in state.hidden_error_codes],
        "last_task_result": _slug(state.base.get("terminateReason"), "TASK_RESULT_"),
    }


def _consumable_attrs(state: NarwalState) -> dict[str, Any]:
    due = state.consumables_due
    items = due.get("maintain", []) + due.get("replace", [])
    return {"items": sorted({CONSUMABLE_NAMES.get(i, i) for i in items}), "raw": items}


def _progress_value(state: NarwalState) -> float | None:
    val = state.progress
    if state.is_cleaning:
        return val
    if val is not None and val > 0:
        return val
    if _slug(state.base.get("terminateReason"), "TASK_RESULT_") == "normal_end" and (
        (state.cleaned_area or 0) > 0 or (state.elapsed_time or 0) > 0
    ):
        return 100.0
    return None


SENSORS: tuple[NarwalSensorDescription, ...] = (
    NarwalSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.battery_level,
    ),
    NarwalSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:robot-vacuum",
        device_class=SensorDeviceClass.ENUM,
        options=list(STATUS_KEYS),
        value_fn=lambda s: s.status_key,
        attrs_fn=lambda s: {
            "task": s.task,
            "sub_status": s.sub_status,
            "activity": s.activity,
            "detail": s.status_text,
        },
    ),
    NarwalSensorDescription(
        key="error",
        translation_key="error",
        icon="mdi:alert-circle-outline",
        value_fn=lambda s: error_message_zh(s.error_codes[0]) if s.error_codes else "无故障",
        attrs_fn=_error_attrs,
    ),
    NarwalSensorDescription(
        key="elapsed_time",
        translation_key="elapsed_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda s: s.elapsed_time,
        persist_last=True,
    ),
    NarwalSensorDescription(
        key="remaining_time",
        translation_key="remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda s: s.remaining_time if s.is_cleaning else None,
    ),
    NarwalSensorDescription(
        key="cleaned_area",
        translation_key="cleaned_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        value_fn=lambda s: s.cleaned_area,
        persist_last=True,
    ),
    NarwalSensorDescription(
        key="progress",
        translation_key="progress",
        icon="mdi:progress-check",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_progress_value,
        persist_last=True,
    ),
    NarwalSensorDescription(
        key="current_room",
        translation_key="current_room",
        icon="mdi:floor-plan",
        value_fn=lambda s: next(
            (r.display_name for r in s.rooms if r.room_id == s.current_room_id), None
        ) if s.is_cleaning and s.current_room_id else None,
    ),
    NarwalSensorDescription(
        key="consumables_due",
        translation_key="consumables_due",
        icon="mdi:wrench-clock",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: len(_consumable_attrs(s)["items"]),
        attrs_fn=_consumable_attrs,
    ),
    _base_enum("clean_water_tank", "cleanWaterTankState", "RobotBaseStatus_CleanWaterTankState",
               "CLEAN_WATER_TANK_STATE_", "mdi:water",
               value=lambda s: s.clean_water_tank_state),
    _base_enum("dirty_water_tank", "sewageTankState", "RobotBaseStatus_SewageTankState",
               "SEWAGE_TANK_STATE_", "mdi:water-opacity",
               value=lambda s: s.sewage_tank_state),
    _base_enum("dust_bin", "dustBoxState", "RobotBaseStatus_DustBoxState",
               "DUST_BOX_STATE_", "mdi:delete-variant"),
    _base_enum("detergent", "detergentState", "RobotBaseStatus_DetergentState",
               "DETERGENT_STATE_", "mdi:bottle-tonic-outline"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for d in SENSORS:
        if coordinator.supports(d.feature):
            entities.append(NarwalSensor(coordinator, d))
        else:
            remove_stale_entity(hass, entry, "sensor", d.key)
    if coordinator.cloud is not None:
        entities += [
            NarwalConsumableSensor(coordinator, item)
            for item in coordinator.consumables
            if item.get("item_type") == 2 and item.get("progress_bar_switch") == 1
        ]
    async_add_entities(entities)


class NarwalSensor(NarwalEntity, RestoreSensor):
    """A sensor computed from the vacuum state."""

    entity_description: NarwalSensorDescription

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._last_value: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (
            self.entity_description.persist_last
            and self.entity_description.value_fn(self.state_data) is None
        ):
            last = await self.async_get_last_sensor_data()
            if (
                last is not None
                and last.native_value is not None
                and last.native_unit_of_measurement in (None, self.native_unit_of_measurement)
            ):
                try:
                    num = float(last.native_value)
                except (TypeError, ValueError):
                    num = None
                if num is not None and num > 0:
                    self._last_value = (
                        int(round(num))
                        if self.native_unit_of_measurement == UnitOfTime.SECONDS
                        else round(num, 1)
                    )

    @property
    def native_value(self) -> Any:
        val = self.entity_description.value_fn(self.state_data)
        if self.entity_description.persist_last:
            if self.state_data.is_cleaning and val is not None:
                self._last_value = val
                return val
            if (
                self.entity_description.key == "progress"
                and not self.state_data.is_cleaning
                and _slug(self.state_data.base.get("terminateReason"), "TASK_RESULT_") == "normal_end"
                and (val is None or val <= 0)
                and self._last_value is not None
            ):
                self._last_value = 100.0
                return 100.0
            if val is not None and val > 0:
                self._last_value = val
                return val
            if self._last_value is not None:
                return self._last_value
        return val

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.state_data) if fn else None


class NarwalConsumableSensor(NarwalEntity, SensorEntity):
    """Countdown for one cloud consumable, named as in the Narwal app."""

    _attr_icon = "mdi:progress-wrench"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: NarwalCoordinator, item: dict[str, Any]) -> None:
        super().__init__(coordinator, f"consumable_{item['type']}")
        self._consumable_type = item["type"]
        self._attr_name = item["name"]

    def _item(self) -> dict[str, Any] | None:
        return next(
            (i for i in self.coordinator.consumables if i.get("type") == self._consumable_type),
            None,
        )

    def _remaining(self) -> int | None:
        item = self._item()
        if not item or not item.get("total_duration"):
            return None
        return max(0, int(item["total_duration"]) - int(item.get("usage_duration") or 0))

    @property
    def native_value(self) -> float | None:
        item = self._item()
        remaining = self._remaining()
        if item is None or remaining is None:
            return None
        return max(0, min(100, round(100 * remaining / item["total_duration"])))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item() or {}
        remaining = self._remaining()
        total = int(item.get("total_duration") or 0)
        used = int(item.get("usage_duration") or 0)
        return {
            "remaining_hours": math.ceil(remaining / 3600) if remaining is not None else None,
            "remaining_days": math.ceil(remaining / 86400) if remaining is not None else None,
            "used_hours": round(used / 3600),
            "total_hours": round(total / 3600),
            "description": item.get("subtitle"),
        }
