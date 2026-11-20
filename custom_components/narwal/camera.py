"""Camera entity showing the Narwal vacuum map."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time

from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client.map_renderer import render_snapshot
from .narwal_client.lv_stream import (
    LinkVisualSession,
    decrypt_extradata,
    fetch_media_identity,
    iter_nal_payloads,
    open_video_stream,
)

try:
    import av

    _HAS_AV = True
except ImportError:
    av = None  # type: ignore[assignment]
    _HAS_AV = False

_LOGGER = logging.getLogger(__name__)

# If the map hasn't loaded yet, ask for it at most this often.
MAP_RETRY_SECONDS = 300

# Live video: stop pushing to the relay when nobody watches for this long.
LIVE_IDLE_SECONDS = 90.0
LIVE_FRAME_TIMEOUT = 15.0
LIVE_HEARTBEAT_SECONDS = 5.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NarwalMapCamera(entry.runtime_data)])
    if entry.runtime_data.cloud is not None:
        async_add_entities([NarwalLiveCamera(entry.runtime_data)])


class NarwalMapCamera(NarwalEntity, Camera):
    """Renders the coordinator's map with live robot position.

    The coordinator owns map fetching (on setup, after each task, and every
    30 min); this entity only renders, so dashboards can't flood the robot
    with map requests.
    """

    _attr_translation_key = "map"
    _attr_icon = "mdi:floor-plan"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        NarwalEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)
        self.content_type = "image/png"
        self._image: bytes | None = None
        self._rendered_key: tuple | None = None
        self._last_retry = 0.0

    @property
    def available(self) -> bool:
        # The last map is still useful while the robot sleeps.
        return self._image is not None or super().available

    @property
    def frame_interval(self) -> float:
        return 2.0 if self.state_data.is_cleaning else 30.0

    @callback
    def _handle_coordinator_update(self) -> None:
        # Bump the access token so the frontend re-fetches when the map/pose changes.
        if self._render_key() != self._rendered_key:
            self.async_update_token()
        super()._handle_coordinator_update()

    def _render_key(self) -> tuple:
        state = self.state_data
        pose = state.robot_pose
        return (
            state.map.fetched_at if state.map else None,
            round(pose.x, 1) if pose else None,
            round(pose.y, 1) if pose else None,
            state.current_room_id if state.is_cleaning else None,
            tuple((r.room_id, r.display_name) for r in state.rooms),
        )

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        state = self.state_data
        if state.map is None:
            now = time.monotonic()
            if self.coordinator.client.connected and now - self._last_retry > MAP_RETRY_SECONDS:
                self._last_retry = now
                await self.coordinator._refresh_map()
            if state.map is None:
                return self._image

        key = self._render_key()
        if key == self._rendered_key and self._image:
            return self._image
        try:
            image = await self.hass.async_add_executor_job(
                render_snapshot,
                state.map,
                {r.room_id: r.display_name for r in state.rooms},
                state.robot_pose,
                state.current_room_id if state.is_cleaning else None,
            )
        except Exception:  # noqa: BLE001 - never break the dashboard
            _LOGGER.debug("Map render failed", exc_info=True)
            return self._image
        if image:
            self._image = image
            self._rendered_key = key
        return self._image


class _LiveView:
    """Background pull of the Aliyun LinkVisual relay, decoding to JPEG.

    The robot only pushes video while it believes a viewer is watching, so
    this sends ``streaming_command`` once, heartbeats every 5 s, and simply
    stops heartbeating to end the session (the robot times out itself).
    """

    def __init__(self, hass: HomeAssistant, coordinator: NarwalCoordinator) -> None:
        self._hass = hass
        self._client = coordinator.client
        self._cloud = coordinator.cloud
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._seq = 0
        self._dead = False
        self._stop = False
        self._last_consumer = time.monotonic()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="narwal-liveview")
        self._thread.start()

    def touch(self) -> None:
        self._last_consumer = time.monotonic()

    @property
    def dead(self) -> bool:
        return self._dead

    def wait_next(self, seq: int, timeout: float) -> tuple[int, bytes | None]:
        """Wait for a frame newer than ``seq``; returns (new_seq, jpeg|None)."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._seq != seq or self._dead, timeout=timeout
            )
            self._last_consumer = time.monotonic()
            return self._seq, self._frame

    def stop(self) -> None:
        self._stop = True
        with self._cond:
            self._cond.notify_all()

    def _run(self) -> None:
        cont = None
        try:
            cloud = self._cloud
            token = cloud.ensure_valid_token()
            ident = fetch_media_identity(
                cloud._api_host, token,
                self._client.product_key, self._client.device_name,
            )
            _LOGGER.info("Live view: media identity %s/%s",
                         ident.product_key, ident.device_name)
            asyncio.run_coroutine_threadsafe(
                self._client.start_video_viewer(), self._hass.loop
            ).result(timeout=10)
            _LOGGER.info("Live view: streaming_command sent")

            session = LinkVisualSession(
                cloud.session.user_uuid, ident.device_name
            ).connect()
            key, iv = session.key_iv
            _LOGGER.info("Live view: opening relay %s", session.relay_url[:60])
            # The robot can take up to ~90s to register and start pushing;
            # opening the relay before any data flows fails outright.
            open_deadline = time.monotonic() + 120.0
            while True:
                try:
                    cont, vs = open_video_stream(session.relay_url)
                    break
                except Exception:
                    cont = None
                    if self._stop or time.monotonic() > open_deadline:
                        raise RuntimeError(
                            "relay never started carrying data "
                            "(robot did not begin pushing)"
                        )
                    time.sleep(2.0)
                    asyncio.run_coroutine_threadsafe(
                        self._client.video_heartbeat(), self._hass.loop
                    )
            codec = av.CodecContext.create("hevc", "r")
            codec.extradata = decrypt_extradata(
                bytes(vs.codec_context.extradata or b""), key, iv)
            codec.open()
            _LOGGER.info("Live view: relay open, decoding")

            last_hb = 0.0
            for payload, pts, dts in iter_nal_payloads(cont, vs, key, iv):
                if self._stop or (
                    time.monotonic() - self._last_consumer > LIVE_IDLE_SECONDS
                ):
                    break
                if time.monotonic() - last_hb > LIVE_HEARTBEAT_SECONDS:
                    asyncio.run_coroutine_threadsafe(
                        self._client.video_heartbeat(), self._hass.loop
                    )
                    last_hb = time.monotonic()
                packet = av.Packet(payload)
                packet.pts, packet.dts = pts, dts
                try:
                    frames = codec.decode(packet)
                except Exception:  # noqa: BLE001 - occasional corrupt tag
                    continue
                for frame in frames:
                    buf = io.BytesIO()
                    frame.to_image().save(buf, format="JPEG", quality=85)
                    with self._cond:
                        self._frame = buf.getvalue()
                        self._seq += 1
                        self._cond.notify_all()
                    if self._seq == 1:
                        _LOGGER.info("Live view: first frame decoded")
        except Exception as err:  # noqa: BLE001 - any failure ends the view
            _LOGGER.warning("Live view ended with error: %s", err, exc_info=True)
        finally:
            self._dead = True
            if self._seq == 0:
                _LOGGER.warning(
                    "Live view produced no frames - the robot may be asleep "
                    "or refused the streaming_command"
                )
            try:  # tell the robot to stop pushing
                asyncio.run_coroutine_threadsafe(
                    self._client.start_video_viewer(start=False), self._hass.loop
                ).result(timeout=5)
            except Exception:  # noqa: BLE001 - best effort
                pass
            with self._cond:
                self._cond.notify_all()
            if cont is not None:
                try:
                    cont.close()
                except Exception:  # noqa: BLE001
                    pass


class NarwalLiveCamera(NarwalEntity, Camera):
    """Live video from the robot camera via Aliyun LinkVisual (CN models)."""

    _attr_translation_key = "live_view"
    _attr_icon = "mdi:video"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        NarwalEntity.__init__(self, coordinator, "live_view")
        Camera.__init__(self)
        self.content_type = "image/jpeg"
        self._live: _LiveView | None = None
        self._live_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return _HAS_AV and self.coordinator.cloud is not None and super().available

    @property
    def frame_interval(self) -> float:
        return 1.0

    async def async_will_remove_from_hass(self) -> None:
        if self._live is not None:
            self._live.stop()

    async def _ensure_live(self) -> _LiveView | None:
        if not _HAS_AV:
            _LOGGER.warning("Live view needs the 'av' package; it did not install")
            return None
        if self.coordinator.cloud is None:
            return None
        async with self._live_lock:
            if self._live is None or self._live.dead:
                live = _LiveView(self.hass, self.coordinator)
                live.start()
                self._live = live
            self._live.touch()
            return self._live

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        live = await self._ensure_live()
        if live is None:
            return None
        _, frame = await self.hass.async_add_executor_job(
            live.wait_next, 0, LIVE_FRAME_TIMEOUT
        )
        return frame

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse:
        live = await self._ensure_live()
        if live is None:
            return web.Response(status=503, text="live view unavailable")
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace;boundary=frame"
            },
        )
        await response.prepare(request)
        seq = 0
        try:
            while not live.dead:
                seq, frame = await self.hass.async_add_executor_job(
                    live.wait_next, seq, LIVE_FRAME_TIMEOUT
                )
                if frame is None:
                    if live.dead:
                        break
                    continue
                await response.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode()
                    + b"\r\n\r\n" + frame + b"\r\n"
                )
        except (ConnectionResetError, asyncio.CancelledError, ConnectionError):
            pass
        return response
