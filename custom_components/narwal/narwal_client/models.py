"""Data models for Narwal vacuum state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .error_codes import ERROR_MESSAGES
from .const import ROOM_SUB_TYPE_NAMES, SUCCESS_RESULTS, FanLevel, MopHumidity
from .proto import ProtoError, decode

_LOGGER = logging.getLogger(__name__)

# Activity values (mirror Home Assistant's VacuumActivity strings).
ACTIVITY_CLEANING = "cleaning"
ACTIVITY_DOCKED = "docked"
ACTIVITY_IDLE = "idle"
ACTIVITY_PAUSED = "paused"
ACTIVITY_RETURNING = "returning"
ACTIVITY_ERROR = "error"

# Top-level task types that mean "the robot is out cleaning".
_CLEANING_TASKS = frozenset({
    "TASK_TYPE_SWEEP",
    "TASK_TYPE_MOP",
    "TASK_TYPE_SWEEP_THEN_MOP",
    "TASK_TYPE_SWEEP_AND_MOP",
    "TASK_TYPE_SMART_CLEAN",
    "TASK_TYPE_COMPOSITE_CLEAN",
    "TASK_TYPE_MAINTIAN_FLOOR",
    "TASK_TYPE_BUILD_MAP",
    "TASK_TYPE_UPDATE_MAP",
    "TASK_TYPE_DEPARTURE",
    "TASK_TYPE_RELOCALIZATION",
})
# Station-side tasks: the robot is on the dock doing maintenance.
_DOCK_TASKS = frozenset({
    "TASK_TYPE_WASH_AND_DRY_MOP",
    "TASK_TYPE_DUST_GATHERING",
    "TASK_TYPE_DRY_STATION_BAG",
    "TASK_TYPE_PUMP_TROUBLESHOOTING",
    "TASK_TYPE_ROBOT_SEWAGE_BOX_PUMP_TROUBLESHOOTING",
})
_RETURN_TASKS = frozenset({"TASK_TYPE_RECALL", "TASK_TYPE_PARKING"})

_FRIENDLY_IDLE = {
    "IDLE_STATUS_IN_STATION": "Docked",
    "IDLE_STATUS_LOW_POWER_IN_STATION": "Sleeping on dock",
    "IDLE_STATUS_OUTSIDE_STATION": "Idle",
    "IDLE_STATUS_LOW_POWER_OUTSIDE_STATION": "Sleeping off dock",
    "IDLE_STATUS_UNINSTALLED_CLEAN_MODULE": "Cleaning module missing",
    "IDLE_STATUS_UNINSTALLED_SWEEP_MODULE": "Vacuum module missing",
    "IDLE_STATUS_UNINSTALLED_MOP_MODULE": "Mop module missing",
}
_TASK_NAMES = {
    "TASK_TYPE_SWEEP": "Vacuuming",
    "TASK_TYPE_MOP": "Mopping",
    "TASK_TYPE_SWEEP_THEN_MOP": "Vacuum then mop",
    "TASK_TYPE_SWEEP_AND_MOP": "Vacuum and mop",
    "TASK_TYPE_SMART_CLEAN": "Smart clean",
    "TASK_TYPE_RECALL": "Returning to base",
    "TASK_TYPE_WASH_AND_DRY_MOP": "Mop care",
    "TASK_TYPE_DUST_GATHERING": "Emptying dust bin",
    "TASK_TYPE_DRY_STATION_BAG": "Drying dust bag",
    "TASK_TYPE_BUILD_MAP": "Mapping",
    "TASK_TYPE_UPDATE_MAP": "Updating map",
    "TASK_TYPE_UPGRADE": "Updating firmware",
    "TASK_TYPE_RELOCALIZATION": "Relocating",
}

# Fixed keys for the translatable status sensor. Keep in sync with the
# entity.sensor.status.state translations.
STATUS_KEYS: tuple[str, ...] = (
    "offline",
    "unknown",
    "error",
    "paused",
    "charging",
    "sleeping",
    "docked",
    "idle",
    "returning",
    "vacuuming",
    "mopping",
    "vacuum_then_mop",
    "vacuum_and_mop",
    "smart_clean",
    "mapping",
    "mop_care",
    "emptying_dustbin",
    "drying_dust_bag",
    "updating_firmware",
    "relocating",
    "departing",
    "maintaining_floor",
    "manual_control",
    "video_cruise",
    "busy",
)
_TASK_STATUS_KEYS = {
    "TASK_TYPE_SWEEP": "vacuuming",
    "TASK_TYPE_MOP": "mopping",
    "TASK_TYPE_SWEEP_THEN_MOP": "vacuum_then_mop",
    "TASK_TYPE_SWEEP_AND_MOP": "vacuum_and_mop",
    "TASK_TYPE_SMART_CLEAN": "smart_clean",
    "TASK_TYPE_COMPOSITE_CLEAN": "smart_clean",
    "TASK_TYPE_BUILD_MAP": "mapping",
    "TASK_TYPE_UPDATE_MAP": "mapping",
    "TASK_TYPE_WASH_AND_DRY_MOP": "mop_care",
    "TASK_TYPE_DUST_GATHERING": "emptying_dustbin",
    "TASK_TYPE_DRY_STATION_BAG": "drying_dust_bag",
    "TASK_TYPE_UPGRADE": "updating_firmware",
    "TASK_TYPE_RECALL": "returning",
    "TASK_TYPE_PARKING": "returning",
    "TASK_TYPE_RELOCALIZATION": "relocating",
    "TASK_TYPE_DEPARTURE": "departing",
    "TASK_TYPE_MAINTIAN_FLOOR": "maintaining_floor",
    "TASK_TYPE_TELECONTROL": "manual_control",
    "TASK_TYPE_VIDEO_CRUISE": "video_cruise",
}
_IN_STATION_IDLE = frozenset({"IDLE_STATUS_IN_STATION", "IDLE_STATUS_LOW_POWER_IN_STATION"})
_CONTACTED = frozenset({"STATION_CONTACT_TYPE_CONTACTED", "STATION_CONTACT_TYPE_CHARGING"})

# The station reports tank problems as error codes while cleanWaterTankState /
# sewageTankState stay NORMAL; these codes win over the enum fields.
_CLEAN_WATER_TANK_BY_ERROR = {
    0x01010137: "CLEAN_WATER_TANK_STATE_EMPTY",
    0x01010037: "CLEAN_WATER_TANK_STATE_EMPTY",
    0x01010034: "CLEAN_WATER_TANK_STATE_NOT_INSTALLED",
    0x01010035: "CLEAN_WATER_TANK_STATE_ABNORMAL",
}
_SEWAGE_TANK_BY_ERROR = {
    0x01010036: "SEWAGE_TANK_STATE_FILLED_OR_NOT_INSTALLED",
}


def _pretty(label: Any, prefix: str = "") -> str:
    """'SWEEP_AND_MOP_STATUS_ROOM_CLEAN' -> 'Room clean' (prefix stripped)."""
    if not isinstance(label, str):
        return str(label) if label is not None else ""
    if prefix and label.startswith(prefix):
        label = label[len(prefix):]
    return label.replace("_", " ").capitalize()


def strip_narwal_frame(raw: bytes) -> bytes:
    """Drop the 0x01 + varint(len) + auth-protobuf header; return the message."""
    if len(raw) < 2 or raw[0] != 0x01:
        return raw
    length = shift = 0
    pos = 1
    while pos < len(raw):
        byte = raw[pos]
        pos += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return raw[pos + length:]


@dataclass
class CommandResponse:
    """Parsed command response."""

    command: str
    raw: bytes = b""
    message: dict[str, Any] = field(default_factory=dict)
    result: str | int | None = None

    @property
    def success(self) -> bool:
        # Read-only commands (get_feature_list, get_device_info) carry no
        # result field at all; treat a missing result as success.
        return self.result is None or self.result in SUCCESS_RESULTS or self.result == 1

    @classmethod
    def parse(cls, command: str, raw: bytes, message_type: str | None) -> CommandResponse:
        body = strip_narwal_frame(raw)
        resp = cls(command=command, raw=body)
        if message_type:
            try:
                resp.message = decode(message_type, body)
            except ProtoError:
                _LOGGER.debug("Could not decode %s as %s: %s", command, message_type, body.hex())
        resp.result = resp.message.get("result")
        return resp


@dataclass
class RoomInfo:
    """A room from the vacuum's map (``StaticMapPayload.roomInfos``)."""

    room_id: int
    room_sub_type: int = 0
    name: str = ""
    category: int = 0
    instance_index: int = 0

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        base = ROOM_SUB_TYPE_NAMES.get(self.room_sub_type, f"Room {self.room_id}")
        if self.instance_index > 1:
            return f"{base} {self.instance_index}"
        return base


@dataclass
class Pose:
    """Position in map grid-cell units and heading in radians."""

    x: float
    y: float
    theta: float = 0.0

    @classmethod
    def from_msg(cls, msg: dict[str, Any] | None) -> Pose | None:
        if not isinstance(msg, dict):
            return None
        loc = msg.get("location") or {}
        if "x" not in loc and "y" not in loc:
            return None
        return cls(float(loc.get("x", 0.0)), float(loc.get("y", 0.0)), float(msg.get("theta", 0.0)))


@dataclass
class MapSnapshot:
    """The static map plus enough geometry to place the robot on it."""

    map_id: int
    width: int
    height: int
    resolution_mm: int
    left: int
    bottom: int
    grid: bytes
    rotate_angle: int = 0
    station: Pose | None = None
    robot: Pose | None = None
    fetched_at: float = field(default_factory=time.time)

    def to_pixel(self, pose: Pose) -> tuple[float, float]:
        """Map coords -> grid pixel (origin bottom-left, before any flip).

        Poses are in grid-cell units already (verified: the docked robot lands
        ~4 cells from a wall); only the border offset is applied.
        """
        return pose.x - self.left, pose.y - self.bottom


@dataclass
class NarwalState:
    """Current vacuum state assembled from pushes and command responses."""

    device_reachable: bool = False
    base: dict[str, Any] = field(default_factory=dict)  # RobotBaseStatus
    working: dict[str, Any] = field(default_factory=dict)  # WorkingStatus
    config: dict[str, Any] = field(default_factory=dict)  # GetConfig_Response.config
    features: dict[str, Any] = field(default_factory=dict)
    consumables_due: dict[str, list[str]] = field(default_factory=dict)
    firmware_version: str = ""
    rooms: list[RoomInfo] = field(default_factory=list)
    map: MapSnapshot | None = None
    robot_pose: Pose | None = None
    current_room_id: int | None = None
    no_go_room_ids: set[int] = field(default_factory=set)
    preferred_fan_level: FanLevel | None = None
    preferred_mop_humidity: MopHumidity | None = None
    last_base_update: float = 0.0
    _latched_clean_water_error: int | None = None
    _latched_clean_water_task: str = ""
    _latched_clean_water_saw_pause: bool = False
    _latched_clean_water_ts: float = 0.0
    _latched_sewage_error: int | None = None
    _latched_sewage_task: str = ""
    _latched_sewage_saw_pause: bool = False
    _latched_sewage_ts: float = 0.0

    # ---- update paths -------------------------------------------------

    @staticmethod
    def _next_tank_latch(
        raw_codes: list[int],
        error_map: dict[int, str],
        cur_err: int | None,
        cur_task: str,
        saw_pause: bool,
        cur_ts: float,
        task_name: str,
        raw_paused_or_error: bool,
        now: float,
    ) -> tuple[int | None, str, bool, float]:
        live_code = next((c for c in raw_codes if c in error_map), None)
        if live_code is not None:
            return live_code, task_name, raw_paused_or_error, now
        if cur_err is None:
            return None, "", False, 0.0
        if raw_paused_or_error:
            return cur_err, cur_task, True, cur_ts
        if not saw_pause and (now - cur_ts) <= 15.0 and task_name == cur_task:
            return cur_err, cur_task, False, cur_ts
        return None, "", False, 0.0

    def _update_tank_latches(self, now: float) -> None:
        raw_codes = self.reported_error_codes
        live_shown_errors = [c for c in raw_codes if c in ERROR_MESSAGES]
        raw_paused = bool(
            self.task_status.get("pauseStatus")
            or self.sub_status.endswith("_WAITING_COMMAND")
            or self.task == "TASK_TYPE_WAITING_COMMAND"
        )
        raw_error = bool(
            live_shown_errors and self.task in ("TASK_TYPE_IDLE", "TASK_TYPE_UNSPECIFIED")
        )
        paused_or_err = raw_paused or raw_error
        (
            self._latched_clean_water_error,
            self._latched_clean_water_task,
            self._latched_clean_water_saw_pause,
            self._latched_clean_water_ts,
        ) = self._next_tank_latch(
            raw_codes,
            _CLEAN_WATER_TANK_BY_ERROR,
            self._latched_clean_water_error,
            self._latched_clean_water_task,
            self._latched_clean_water_saw_pause,
            self._latched_clean_water_ts,
            self.task,
            paused_or_err,
            now,
        )
        (
            self._latched_sewage_error,
            self._latched_sewage_task,
            self._latched_sewage_saw_pause,
            self._latched_sewage_ts,
        ) = self._next_tank_latch(
            raw_codes,
            _SEWAGE_TANK_BY_ERROR,
            self._latched_sewage_error,
            self._latched_sewage_task,
            self._latched_sewage_saw_pause,
            self._latched_sewage_ts,
            self.task,
            paused_or_err,
            now,
        )

    def update_base_status(self, base: dict[str, Any]) -> None:
        prev = self.activity
        now = time.time()
        self.base = base
        self.last_base_update = now
        self._update_tank_latches(now)
        if self.activity != prev:
            _LOGGER.info(
                "Narwal activity %s -> %s (task=%s, sub=%s)",
                prev, self.activity, self.task, self.sub_status,
            )

    def update_working_status(self, working: dict[str, Any]) -> None:
        self.working = working
        zone = working.get("cleaningZoneId")
        if isinstance(zone, int) and zone:
            self.current_room_id = zone

    def update_display_map(self, display: dict[str, Any]) -> None:
        pose = Pose.from_msg(display.get("robot"))
        if pose:
            self.robot_pose = pose
        zone = display.get("currentCleanZoneId")
        if isinstance(zone, int):
            self.current_room_id = zone or None

    def update_rooms_from_map(self, static_map: dict[str, Any]) -> None:
        rooms: list[RoomInfo] = []
        for info in static_map.get("roomInfos", []):
            if not isinstance(info, dict) or "roomId" not in info:
                continue
            rooms.append(RoomInfo(
                room_id=int(info["roomId"]),
                room_sub_type=int(info.get("type", 0) or 0),
                name=str(info.get("customName", "") or ""),
                instance_index=int(info.get("roomTypeId", 0) or 0),
            ))
        if rooms:
            self.rooms = sorted(rooms, key=lambda r: r.room_id)
        # Whole rooms marked no-go for both vacuum and mop; the robot refuses
        # any task that includes them, so a full-house clean must skip them.
        self.no_go_room_ids = {
            int(f["roomId"])
            for f in static_map.get("forbiddens", [])
            if isinstance(f, dict)
            and f.get("mode") == "FORBIDDEN_MODE_ROOM"
            and f.get("type") in ("FORBIDDEN_TYPE_SWEEP_MOP", "FORBIDDEN_TYPE_UNSPECIFIED", None)
            and f.get("roomId")
        }

    # ---- derived properties -------------------------------------------

    @property
    def task_status(self) -> dict[str, Any]:
        status = self.base.get("robotTaskStatus")
        return status if isinstance(status, dict) else {}

    @property
    def task(self) -> str:
        task = self.task_status.get("task")
        return task if isinstance(task, str) else "TASK_TYPE_UNSPECIFIED"

    @property
    def sub_status(self) -> str:
        """The task-specific sub-status label (e.g. SWEEP_STATUS_ROOM_CLEAN)."""
        for key, value in self.task_status.items():
            if key != "task" and isinstance(value, str):
                return value
        return ""

    @property
    def current_map_id(self) -> int | None:
        """The active map's id: from the loaded map, else from robot status."""
        if self.map and self.map.map_id:
            return self.map.map_id
        value = self.base.get("mapId")
        return int(value) if isinstance(value, int) and value else None

    @property
    def battery_level(self) -> float | None:
        value = self.base.get("batteryPercentage")
        if isinstance(value, (int, float)):
            return max(0.0, min(100.0, float(value)))
        return None

    @property
    def reported_error_codes(self) -> list[int]:
        """Every code the robot reports, including ones the app never shows."""
        codes = []
        for err in self.base.get("errorCode", []) or []:
            if isinstance(err, dict) and err.get("identityCode"):
                codes.append(int(err["identityCode"]))
        return codes

    @property
    def error_codes(self) -> list[int]:
        """Errors the Narwal app would show: codes in its table for this model.

        The robot also reports internal notices (seen live: 0x01010072, level
        1, mid-clean with everything normal) that the app has no message for
        and doesn't display; those must not raise errors in HA.
        """
        codes = [c for c in self.reported_error_codes if c in ERROR_MESSAGES]
        for latched in (self._latched_clean_water_error, self._latched_sewage_error):
            if latched is not None and latched in ERROR_MESSAGES and latched not in codes:
                codes.append(latched)
        return codes

    @property
    def hidden_error_codes(self) -> list[int]:
        return [c for c in self.reported_error_codes if c not in ERROR_MESSAGES]

    @property
    def clean_water_tank_state(self) -> str | None:
        for code in self.reported_error_codes:
            if code in _CLEAN_WATER_TANK_BY_ERROR:
                return _CLEAN_WATER_TANK_BY_ERROR[code]
        if self._latched_clean_water_error is not None:
            return _CLEAN_WATER_TANK_BY_ERROR[self._latched_clean_water_error]
        value = self.base.get("cleanWaterTankState")
        return value if isinstance(value, str) else None

    @property
    def sewage_tank_state(self) -> str | None:
        for code in self.reported_error_codes:
            if code in _SEWAGE_TANK_BY_ERROR:
                return _SEWAGE_TANK_BY_ERROR[code]
        if self._latched_sewage_error is not None:
            return _SEWAGE_TANK_BY_ERROR[self._latched_sewage_error]
        value = self.base.get("sewageTankState")
        return value if isinstance(value, str) else None

    @property
    def on_station(self) -> bool:
        return (
            self.base.get("stationContactType") in _CONTACTED
            or self.sub_status in _IN_STATION_IDLE
        )

    @property
    def is_charging(self) -> bool:
        if self.base.get("stationContactType") == "STATION_CONTACT_TYPE_CHARGING":
            return True
        return self.on_station and self.base.get("chargingStatus") == "CHG_STATUS_CHARGING"

    @property
    def activity(self) -> str:
        if not self.base:
            return ACTIVITY_IDLE
        if self.error_codes and self.task in ("TASK_TYPE_IDLE", "TASK_TYPE_UNSPECIFIED"):
            return ACTIVITY_ERROR
        task, sub = self.task, self.sub_status
        if (
            self.task_status.get("pauseStatus")
            or sub.endswith("_WAITING_COMMAND")
            or task == "TASK_TYPE_WAITING_COMMAND"
        ):
            return ACTIVITY_PAUSED
        if task in _RETURN_TASKS:
            return ACTIVITY_RETURNING
        if task in _CLEANING_TASKS:
            if "RECALL" in sub or "BACK_TO_STATION" in sub or sub.endswith("_PARKING"):
                return ACTIVITY_RETURNING
            return ACTIVITY_CLEANING
        if task in _DOCK_TASKS:
            return ACTIVITY_DOCKED
        if task == "TASK_TYPE_IDLE":
            return ACTIVITY_DOCKED if self.on_station else ACTIVITY_IDLE
        return ACTIVITY_DOCKED if self.on_station else ACTIVITY_IDLE

    @property
    def is_paused(self) -> bool:
        return self.activity == ACTIVITY_PAUSED

    @property
    def is_cleaning(self) -> bool:
        return self.task in _CLEANING_TASKS

    @property
    def status_key(self) -> str:
        """One fixed machine-readable key describing the current status."""
        if not self.base:
            return "unknown"
        if not self.device_reachable:
            return "offline"
        if self.activity == ACTIVITY_ERROR:
            return "error"
        if self.is_paused:
            return "paused"
        if self.task == "TASK_TYPE_IDLE":
            if self.is_charging:
                return "charging"
            if self.sub_status.startswith("IDLE_STATUS_LOW_POWER"):
                return "sleeping"
            return "docked" if self.on_station else "idle"
        if self.activity == ACTIVITY_RETURNING:
            return "returning"
        return _TASK_STATUS_KEYS.get(self.task, "busy")

    @property
    def status_text(self) -> str:
        """Readable status similar to the app, e.g. 'Sweep and mop: room clean'."""
        if not self.device_reachable:
            return "Offline"
        if not self.base:
            return "Unknown"
        sub = self.sub_status
        if self.task == "TASK_TYPE_IDLE":
            if self.is_charging:
                return "Charging"
            if sub in _FRIENDLY_IDLE:
                return _FRIENDLY_IDLE[sub]
        task = _TASK_NAMES.get(self.task) or _pretty(self.task, "TASK_TYPE_")
        if sub:
            prefix = sub.rsplit("_STATUS_", 1)[0] + "_STATUS_" if "_STATUS_" in sub else ""
            return f"{task}: {_pretty(sub, prefix).lower()}"
        return task

    @property
    def fan_level(self) -> FanLevel | None:
        """Suction: the robot's live value while cleaning, else the level the
        next clean will use (a choice made in HA while docked wins)."""
        reported = _enum_from_label(FanLevel, self.base.get("fanLevel"), "FAN_LEVEL_", {"MUTE": "QUIET", "SUPER": "MAX"})
        if self.is_cleaning:
            return reported
        return self.preferred_fan_level or reported

    @property
    def mop_humidity(self) -> MopHumidity | None:
        reported = _enum_from_label(MopHumidity, self.base.get("mopHumidity"), "MOP_HUMIDITY_")
        if self.is_cleaning:
            return reported
        return self.preferred_mop_humidity or reported

    @property
    def cleaned_area(self) -> float | None:
        """Area covered in the current/last task, m²."""
        value = self.working.get("coveredArea")
        return round(float(value), 1) if isinstance(value, (int, float)) else None

    @property
    def elapsed_time(self) -> int | None:
        value = self.working.get("timeConsuming")
        return int(value) if isinstance(value, int) else None

    @property
    def remaining_time(self) -> int | None:
        value = self.working.get("remainedTime")
        return int(value) if isinstance(value, int) else None

    @property
    def progress(self) -> float | None:
        value = self.working.get("workingProgress")
        if not isinstance(value, (int, float)):
            return None
        # The robot reports 0..1 on some firmware and 0..100 on others.
        return round(value * 100 if value <= 1.0 else value, 1)


def _enum_from_label(enum_cls, label: Any, prefix: str, aliases: dict[str, str] | None = None):
    if not isinstance(label, str) or not label.startswith(prefix):
        return None
    name = label[len(prefix):]
    name = (aliases or {}).get(name, name)
    try:
        member = enum_cls[name]
    except KeyError:
        return None
    return None if member.value == 0 else member
