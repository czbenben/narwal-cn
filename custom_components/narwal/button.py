"""Button entities for Narwal robot vacuum."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import NarwalClient, NarwalCloudError
from .narwal_client.proto import enum_labels


@dataclass(frozen=True, kw_only=True)
class NarwalButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[NarwalClient], Coroutine[Any, Any, Any]]


BUTTONS: tuple[NarwalButtonDescription, ...] = (
    NarwalButtonDescription(key="locate", translation_key="locate",
                            icon="mdi:map-marker-question", press_fn=lambda c: c.locate()),
    NarwalButtonDescription(key="wake", translation_key="wake",
                            icon="mdi:power", press_fn=lambda c: c.notify_active()),
    NarwalButtonDescription(key="wash_mop", translation_key="wash_mop",
                            icon="mdi:washing-machine", press_fn=lambda c: c.wash_mop()),
    NarwalButtonDescription(key="dry_mop", translation_key="dry_mop",
                            icon="mdi:fan", press_fn=lambda c: c.dry_mop()),
    NarwalButtonDescription(key="wash_and_dry_mop", translation_key="wash_and_dry_mop",
                            icon="mdi:autorenew", press_fn=lambda c: c.wash_and_dry_mop()),
    NarwalButtonDescription(key="empty_dustbin", translation_key="empty_dustbin",
                            icon="mdi:delete-empty", press_fn=lambda c: c.empty_dustbin()),
    NarwalButtonDescription(key="reboot", translation_key="reboot",
                            icon="mdi:restart", entity_category=EntityCategory.CONFIG,
                            press_fn=lambda c: c.reboot()),
)

# Cloud consumable `type` -> ConsumableReplaceItem label for the MQTT reset.
_CONSUMABLE_LABELS = enum_labels("ConsumableReplaceItem")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = [NarwalButton(coordinator, d) for d in BUTTONS]
    if coordinator.cloud is not None:
        entities += [
            NarwalConsumableResetButton(coordinator, item)
            for item in coordinator.consumables
            if item.get("reset_btn_switch") == 1
        ]
    async_add_entities(entities)


class NarwalButton(NarwalEntity, ButtonEntity):
    """A one-shot robot action."""

    entity_description: NarwalButtonDescription

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self._run(self.entity_description.press_fn(self.coordinator.client))
        await self.coordinator.async_request_refresh()


class NarwalConsumableResetButton(NarwalEntity, ButtonEntity):
    """Mark a cloud consumable as replaced (the app's 已更换)."""

    _attr_translation_key = "reset_consumable"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:archive-refresh"

    def __init__(self, coordinator: NarwalCoordinator, item: dict[str, Any]) -> None:
        super().__init__(coordinator, f"reset_consumable_{item['type']}")
        self._consumable_type = item["type"]
        self._consumables_code = item["consumables_code"]
        self._attr_translation_placeholders = {"name": item["name"]}

    async def async_press(self) -> None:
        # The app sends both the MQTT reset (robot-side counter) and the
        # cloud reset (app-side countdown).
        label = _CONSUMABLE_LABELS.get(self._consumable_type)
        if label:
            await self._run(self.coordinator.client.reset_consumable(label))
        cloud = self.coordinator.cloud
        client = self.coordinator.client
        try:
            await self.hass.async_add_executor_job(
                cloud.reset_consumable,
                client.device_name,
                client.product_key,
                self._consumables_code,
            )
        except NarwalCloudError as err:
            raise HomeAssistantError(f"Could not reset consumable: {err}") from err
        await self.coordinator.fetch_cloud_consumables()
        self.coordinator.async_set_updated_data(self.coordinator.client.state)
