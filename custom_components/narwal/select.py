"""Select entities for Narwal robot vacuum."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import CLEAN_MODE_LIST, CLEAN_MODE_MAP, CLEAN_MODE_REVERSE, MOP_HUMIDITY_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity, remove_stale_entity
from .narwal_client import smart_clean
from .narwal_client.schema import MESSAGES


@dataclass(frozen=True, kw_only=True)
class NarwalConfigSelectDescription(SelectEntityDescription):
    config_key: str
    # HA option -> enum label sent to / read from the robot.
    values: dict[str, str]
    # Robot feature flag required for this entity (None = always create).
    feature: str | None = None
    # Label assumed when the robot omits the field (proto3 drops defaults).
    default: str | None = None


CONFIG_SELECTS: tuple[NarwalConfigSelectDescription, ...] = (
    NarwalConfigSelectDescription(
        key="mop_dry_strength",
        translation_key="mop_dry_strength",
        icon="mdi:fan",
        config_key="dryMopStrength",
        values={
            "quiet": "DRY_MOP_STRENGTH_QUIET",
            "strong": "DRY_MOP_STRENGTH_STRONG",
            "smart": "DRY_MOP_STRENGTH_SMART",
        },
    ),
    NarwalConfigSelectDescription(
        key="carpet_strategy",
        translation_key="carpet_strategy",
        icon="mdi:rug",
        config_key="carpetCleanOption",
        feature="supportCarpetCleanSetting",
        values={
            "cross": "CARPET_CLEAN_OPTION_ACROSS",
            "boost": "CARPET_CLEAN_OPTION_PRESSURE",
            "ignore": "CARPET_CLEAN_OPTION_IGNORE",
            "avoid": "CARPET_CLEAN_OPTION_AVOID",
        },
    ),
    NarwalConfigSelectDescription(
        key="corner_clean_mode",
        translation_key="corner_clean_mode",
        icon="mdi:square-rounded-outline",
        config_key="cornerCleanMode",
        feature="supportFlexibleMop",
        values={
            "flexible_mop": "CORNER_CLEAN_MODE_FLEXIBLE_MOP",
            "rotate_robot": "CORNER_CLEAN_MODE_ROTATE_ROBOT",
            "off": "CORNER_CLEAN_MODE_DISABLED",
        },
    ),
    NarwalConfigSelectDescription(
        key="station_clean_mode",
        translation_key="station_clean_mode",
        icon="mdi:home-import-outline",
        config_key="stationCleanMode",
        feature="supportStationCleanMode",
        # J5 omits the field; the app shows 扫拖同时 for it.
        default="CLEAN_MODE_SWEEP_MOP_SYNC",
        values={
            "vacuum_and_mop": "CLEAN_MODE_SWEEP_MOP_SYNC",
            "vacuum_then_mop": "CLEAN_MODE_SWEEP_THEN_MOP",
            "vacuum_only": "CLEAN_MODE_SWEEP",
            "mop_only": "CLEAN_MODE_MOP",
        },
    ),
    NarwalConfigSelectDescription(
        key="avoid_mode",
        translation_key="avoid_mode",
        icon="mdi:radar",
        config_key="avoidMode",
        feature="supportAvoidModeConfig",
        values={
            "smart": "AVOID_MODE_SMART",
            "safer": "AVOID_MODE_SAFER",
            "off": "AVOID_MODE_OFF",
        },
    ),
)


@dataclass(frozen=True, kw_only=True)
class NarwalSmartCleanDescription(SelectEntityDescription):
    # smartCleanParam submessage field number.
    param_field: int
    # HA option -> submsg value (0 = "smart") or stain-remover mode.
    choices: dict[str, int | str]
    stain: bool = False


# 鲸灵托管 (smart clean) parameters; field numbers from smart_clean.py.
SMART_CLEAN_SELECTS: tuple[NarwalSmartCleanDescription, ...] = (
    NarwalSmartCleanDescription(
        key="smart_mop_repeat_limit",
        translation_key="smart_mop_repeat_limit",
        icon="mdi:repeat",
        param_field=smart_clean.FIELD_MOP_REPEAT_LIMIT,
        choices={"smart": 0, "1": 1, "2": 2, "3": 3},
    ),
    NarwalSmartCleanDescription(
        key="smart_fineness",
        translation_key="smart_fineness",
        icon="mdi:blur",
        param_field=smart_clean.FIELD_FINENESS,
        choices={"smart": 0, "standard": 1, "fine": 2},
    ),
    NarwalSmartCleanDescription(
        key="smart_suction",
        translation_key="smart_suction",
        icon="mdi:vacuum",
        param_field=smart_clean.FIELD_SUCTION,
        choices={"smart": 0, "quiet": 1, "standard": 2, "strong": 3, "max": 4},
    ),
    NarwalSmartCleanDescription(
        key="smart_mop_humidity",
        translation_key="smart_mop_humidity",
        icon="mdi:water-percent",
        param_field=smart_clean.FIELD_MOP_HUMIDITY,
        choices={"smart": 0, "slightly_dry": 1, "standard": 2, "slightly_wet": 3},
    ),
    NarwalSmartCleanDescription(
        key="smart_stain_remover",
        translation_key="smart_stain_remover",
        icon="mdi:spray",
        param_field=smart_clean.FIELD_STAIN_REMOVER,
        choices={
            "every_time": "every_time",
            "every_7_days": "every_7_days",
            "every_14_days": "every_14_days",
            "off": "off",
        },
        stain=True,
    ),
)

# cleanMopFreq label -> HA option for the rewash-strategy select.
_MOP_WASH_AREA = {
    "every_8m2": "CLEAN_MOP_FREQ_DEEP",
    "every_10m2": "CLEAN_MOP_FREQ_NORMAL",
    "every_12m2": "CLEAN_MOP_FREQ_EFFICIENT",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [
        NarwalCleanModeSelect(coordinator),
        NarwalMopHumiditySelect(coordinator),
        NarwalMopWashFrequencySelect(coordinator),
        NarwalDustGatheringFrequencySelect(coordinator),
    ]
    for d in CONFIG_SELECTS:
        if coordinator.supports(d.feature):
            entities.append(NarwalConfigSelect(coordinator, d))
        else:
            remove_stale_entity(hass, entry, "select", d.key)
    # XY-style corner cleaning only where the mop can't reach out.
    if coordinator.supports("!supportFlexibleMop"):
        entities.append(NarwalDeepCornerCleanSelect(coordinator))
    else:
        remove_stale_entity(hass, entry, "select", "deep_corner_clean")
    for d in SMART_CLEAN_SELECTS:
        if coordinator.supports("supportCustomizeSmartCleanParam"):
            entities.append(NarwalSmartCleanSelect(coordinator, d))
        else:
            remove_stale_entity(hass, entry, "select", d.key)
    async_add_entities(entities)


class NarwalCleanModeSelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Clean mode used by vacuum.start and room cleaning (kept in HA)."""

    _attr_translation_key = "clean_mode"
    _attr_icon = "mdi:spray-bottle"
    _attr_options = CLEAN_MODE_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "clean_mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in CLEAN_MODE_MAP:
            self.coordinator.selected_clean_mode = CLEAN_MODE_MAP[last.state]

    @property
    def available(self) -> bool:
        return True  # local preference; usable even while the robot sleeps

    @property
    def current_option(self) -> str:
        return CLEAN_MODE_REVERSE[self.coordinator.selected_clean_mode]

    async def async_select_option(self, option: str) -> None:
        self.coordinator.selected_clean_mode = CLEAN_MODE_MAP[option]
        self.async_write_ha_state()


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity):
    """Mop wetness, as reported by the robot."""

    _attr_translation_key = "mop_humidity"
    _attr_icon = "mdi:water-percent"
    _attr_options = list(MOP_HUMIDITY_MAP)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "mop_humidity")

    @property
    def current_option(self) -> str | None:
        level = self.state_data.mop_humidity
        return level.name.lower() if level else None

    async def async_select_option(self, option: str) -> None:
        await self._run(self.coordinator.client.set_mop_humidity(MOP_HUMIDITY_MAP[option]))
        self.async_write_ha_state()


class NarwalConfigSelect(NarwalEntity, SelectEntity):
    """An enum-valued robot setting."""

    entity_description: NarwalConfigSelectDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalConfigSelectDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.values)

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        d = self.entity_description
        label = self.state_data.config.get(d.config_key, d.default)
        return next((k for k, v in d.values.items() if v == label), None)

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        await self._run(self.coordinator.client.set_config(**{d.config_key: d.values[option]}))
        self.async_write_ha_state()


class NarwalMopWashFrequencySelect(NarwalEntity, SelectEntity):
    """Mop rewash strategy: per-room or every N m².

    Write path mirrors the app: ``customCleanStrategy`` is written back with
    the user's room order (``cleanOrders``) untouched.
    """

    _attr_translation_key = "mop_wash_frequency"
    _attr_icon = "mdi:washing-machine"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "mop_wash_frequency")

    def _strategy(self) -> dict | None:
        strategy = self.state_data.config.get("customCleanStrategy")
        return strategy if isinstance(strategy, dict) else None

    def _writable_strategy(self, updates: dict) -> dict:
        valid = {f[0] for f in MESSAGES["CustomCleanStrategy"].values()}
        strategy = {k: v for k, v in (self._strategy() or {}).items() if k in valid}
        return {**strategy, **updates}

    @property
    def options(self) -> list[str]:
        opts = list(_MOP_WASH_AREA)
        if self._strategy() is not None:
            opts.insert(0, "by_room")
        return opts

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        strategy = self._strategy()
        if strategy and strategy.get("strategy") == "CUSTOM_CLEAN_ROOM":
            return "by_room"
        freq = (strategy or {}).get("areaOption") or self.state_data.config.get("cleanMopFreq")
        return next((k for k, v in _MOP_WASH_AREA.items() if v == freq), None)

    async def async_select_option(self, option: str) -> None:
        client = self.coordinator.client
        if option == "by_room":
            await self._run(client.set_config(customCleanStrategy=self._writable_strategy(
                {"strategy": "CUSTOM_CLEAN_ROOM"})))
        elif self._strategy() is not None:
            label = _MOP_WASH_AREA[option]
            await self._run(client.set_config(
                cleanMopFreq=label,
                customCleanStrategy=self._writable_strategy(
                    {"strategy": "CUSTOM_CLEAN_AREA", "areaOption": label}),
            ))
        else:
            await self._run(client.set_config(cleanMopFreq=_MOP_WASH_AREA[option]))
        self.async_write_ha_state()


class NarwalDeepCornerCleanSelect(NarwalEntity, SelectEntity):
    """Deep corner cleaning frequency (XY-style cleanCorner setting)."""

    _attr_translation_key = "deep_corner_clean"
    _attr_icon = "mdi:square-outline"
    _attr_options = ["off", "smart", "every_time"]
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "deep_corner_clean")

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        corner = self.state_data.config.get("cleanCorner")
        if not isinstance(corner, dict):
            return None
        freq = corner.get("cleanCornerFreq")
        if freq == "CLEAN_CORNER_FREQ_OFF":
            return "off"
        if freq == "CLEAN_CORNER_FREQ_EVERY_N_DAYS":
            return "smart" if corner.get("interval") else "every_time"
        return None

    async def async_select_option(self, option: str) -> None:
        setting = {
            "smart": {"cleanCornerFreq": "CLEAN_CORNER_FREQ_EVERY_N_DAYS", "interval": 7},
            "every_time": {"cleanCornerFreq": "CLEAN_CORNER_FREQ_EVERY_N_DAYS"},
            "off": {"cleanCornerFreq": "CLEAN_CORNER_FREQ_OFF"},
        }[option]
        await self._run(self.coordinator.client.set_config(cleanCornerSetting=setting))
        self.async_write_ha_state()


class NarwalDustGatheringFrequencySelect(NarwalEntity, SelectEntity):
    """Auto-empty frequency (dustGatheringEnabled + smartDustGatheringEnabled)."""

    _attr_translation_key = "dust_gathering_frequency"
    _attr_icon = "mdi:delete-clock"
    _attr_options = ["off", "high", "smart"]
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "dust_gathering_frequency")

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        config = self.state_data.config
        if not config.get("dustGatheringEnabled"):
            return "off"
        if config.get("smartDustGatheringEnabled"):
            return "smart"
        return "high"

    async def async_select_option(self, option: str) -> None:
        values = {
            "off": {"dustGatheringEnabled": False},
            "high": {"dustGatheringEnabled": True, "smartDustGatheringEnabled": False},
            "smart": {"dustGatheringEnabled": True, "smartDustGatheringEnabled": True},
        }[option]
        await self._run(self.coordinator.client.set_config(**values))
        self.async_write_ha_state()


class NarwalSmartCleanSelect(NarwalEntity, SelectEntity):
    """One 鲸灵托管 setting inside the smartCleanParam blob."""

    entity_description: NarwalSmartCleanDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: NarwalCoordinator, description: NarwalSmartCleanDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.choices)

    def _raw(self) -> bytes | None:
        raw = self.state_data.config.get("smartCleanParam")
        return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None

    @property
    def available(self) -> bool:
        return super().available and self._raw() is not None

    @property
    def current_option(self) -> str | None:
        raw = self._raw()
        if raw is None:
            return None
        d = self.entity_description
        value = (
            smart_clean.get_stain_remover(raw)
            if d.stain
            else smart_clean.get_option(raw, d.param_field)
        )
        return next((k for k, v in d.choices.items() if v == value), None)

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        raw = self._raw() or b""
        choice = d.choices[option]
        if d.stain:
            patched = smart_clean.set_stain_remover(raw, str(choice))
        else:
            patched = smart_clean.set_option(raw, d.param_field, int(choice) or None)
        await self._run(self.coordinator.client.set_config(smartCleanParam=patched))
        self.async_write_ha_state()
