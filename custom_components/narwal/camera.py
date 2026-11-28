"""Camera entity showing the Narwal vacuum map and live video."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
import struct
import threading
import time

from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity, remove_stale_entity
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
LIVE_TS_IDLE_SECONDS = 20.0
LIVE_STARTUP_GRACE = 45.0
LIVE_FRAME_TIMEOUT = 15.0
LIVE_HEARTBEAT_SECONDS = 3.5


def hvcc_to_annexb(ex: bytes) -> bytes:
    """Convert decrypted ISO/IEC 14496-15 hvcC extradata into Annex-B VPS/SPS/PPS."""
    if not ex:
        return b""
    if ex.startswith(b"\x00\x00\x00\x01") or ex.startswith(b"\x00\x00\x01"):
        return ex
    if len(ex) < 23 or ex[0] != 1:
        return b""
    num_arrays = ex[22]
    pos = 23
    out: list[bytes] = []
    for _ in range(num_arrays):
        if pos + 3 > len(ex):
            break
        num_nalus = struct.unpack_from(">H", ex, pos + 1)[0]
        pos += 3
        for _ in range(num_nalus):
            if pos + 2 > len(ex):
                return b"".join(out)
            nal_len = struct.unpack_from(">H", ex, pos)[0]
            pos += 2
            if pos + nal_len > len(ex):
                return b"".join(out)
            out.append(b"\x00\x00\x00\x01" + ex[pos : pos + nal_len])
            pos += nal_len
    return b"".join(out)


def _mpeg2_crc32(data: bytes) -> int:
    """Standard MPEG-2 CRC32 (poly 0x04C11DB7, init 0xFFFFFFFF, no final XOR)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


class _HevcMpegTsMuxer:
    """Minimal pure-Python 188-byte MPEG-TS muxer for raw Annex-B HEVC streams."""

    PAT_PID = 0x0000
    PMT_PID = 0x1000
    VIDEO_PID = 0x0100
    STREAM_TYPE_HEVC = 0x24
    HEVC_AUD = b"\x00\x00\x00\x01\x46\x01\x50"

    def __init__(self) -> None:
        self.pat_cc = 0
        self.pmt_cc = 0
        self.vid_cc = 0
        self._sent_tables = False

    def build_pat(self) -> bytes:
        section = b"\x00\xB0\x0D\x00\x01\xC1\x00\x00\x00\x01\xF0\x00"
        crc = struct.pack(">I", _mpeg2_crc32(section))
        psi = b"\x00" + section + crc
        hdr = bytes([0x47, 0x40, 0x00, 0x10 | self.pat_cc])
        self.pat_cc = (self.pat_cc + 1) & 0x0F
        return hdr + psi + (b"\xFF" * (184 - len(psi)))

    def build_pmt(self) -> bytes:
        es = struct.pack(">BHH", self.STREAM_TYPE_HEVC, 0xE000 | self.VIDEO_PID, 0xF000)
        body = struct.pack(">HBBBHH", 1, 0xC1, 0, 0, 0xE000 | self.VIDEO_PID, 0xF000) + es
        sec_len = len(body) + 4
        section = struct.pack(">BH", 0x02, 0xB000 | sec_len) + body
        crc = struct.pack(">I", _mpeg2_crc32(section))
        psi = b"\x00" + section + crc
        hdr = bytes([0x47, 0x40 | (self.PMT_PID >> 8), self.PMT_PID & 0xFF, 0x10 | self.pmt_cc])
        self.pmt_cc = (self.pmt_cc + 1) & 0x0F
        return hdr + psi + (b"\xFF" * (184 - len(psi)))

    def build_init_tables(self) -> bytes:
        return self.build_pat() + self.build_pmt()

    @staticmethod
    def _encode_pts(pts_90k: int) -> bytes:
        pts = pts_90k & 0x1FFFFFFFF
        return bytes([
            0x21 | (((pts >> 30) & 0x07) << 1),
            (pts >> 22) & 0xFF,
            0x01 | (((pts >> 15) & 0x7F) << 1),
            (pts >> 7) & 0xFF,
            0x01 | ((pts & 0x7F) << 1),
        ])

    @staticmethod
    def _encode_pcr(pcr_90k: int) -> bytes:
        base = pcr_90k & 0x1FFFFFFFF
        return bytes([
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E,
            0x00,
        ])

    def mux_access_unit(self, annexb_payload: bytes, pts_90k: int, is_keyframe: bool) -> bytes:
        out: list[bytes] = []
        if is_keyframe or not self._sent_tables:
            out.append(self.build_pat())
            out.append(self.build_pmt())
            self._sent_tables = True

        pes = (
            b"\x00\x00\x01\xE0\x00\x00\x80\x80\x05"
            + self._encode_pts(pts_90k)
            + self.HEVC_AUD
            + annexb_payload
        )

        pid_hi = (self.VIDEO_PID >> 8) & 0x1F
        pid_lo = self.VIDEO_PID & 0xFF
        offset = 0
        total = len(pes)
        first = True

        while offset < total:
            remaining = total - offset
            pusi = 0x40 if first else 0x00
            if first and is_keyframe:
                # RAI (0x40) | PCR (0x10) + 6-byte PCR => 7 bytes af body + 1 byte af_len = 8 bytes
                take = min(remaining, 176)
                stuff = 176 - take
                af_len = 7 + stuff
                af = bytes([af_len, 0x50]) + self._encode_pcr(pts_90k) + (b"\xFF" * stuff)
                hdr = bytes([0x47, pusi | pid_hi, pid_lo, 0x30 | self.vid_cc])
                chunk = pes[offset : offset + take]
                out.append(hdr + af + chunk)
                offset += take
            elif remaining < 184:
                pad = 184 - remaining
                if pad == 1:
                    af = b"\x00"
                    take = 183
                else:
                    af = bytes([pad - 1, 0x00]) + (b"\xFF" * (pad - 2))
                    take = remaining
                hdr = bytes([0x47, pusi | pid_hi, pid_lo, 0x30 | self.vid_cc])
                chunk = pes[offset : offset + take]
                out.append(hdr + af + chunk)
                offset += take
            else:
                hdr = bytes([0x47, pusi | pid_hi, pid_lo, 0x10 | self.vid_cc])
                out.append(hdr + pes[offset : offset + 184])
                offset += 184

            self.vid_cc = (self.vid_cc + 1) & 0x0F
            first = False

        return b"".join(out)


def _enqueue_drop_oldest(q: asyncio.Queue[bytes | None], item: bytes | None) -> None:
    """Push an item into a bounded asyncio.Queue without backpressure lag."""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([NarwalMapCamera(coordinator)])
    # Strict gate: no live camera while the feature list is unknown; the
    # coordinator reloads the entry once a sleeping robot answers.
    if coordinator.cloud is not None and coordinator.client.state.features.get(
        "supportVideoStreaming"
    ):
        async_add_entities([NarwalLiveCamera(coordinator)])
    elif coordinator.client.state.features:
        remove_stale_entity(hass, entry, "camera", "live_view")


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
        self._last_token_key: tuple | None = None
        self._last_token_bump = 0.0
        self._last_retry = 0.0
        self._disk_loaded = False

    def _cache_path(self) -> Path:
        return Path(self.hass.config.path(".storage")) / (
            f"narwal_map_{self.coordinator.client.device_name}.png"
        )

    def _read_disk_cache(self) -> bytes | None:
        try:
            data = self._cache_path().read_bytes()
            return data if data else None
        except OSError:
            return None

    def _write_disk_cache(self, data: bytes) -> None:
        try:
            path = self._cache_path()
            tmp = path.with_suffix(".png.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            _LOGGER.debug("Could not persist map cache", exc_info=True)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._image is None and not self._disk_loaded:
            self._disk_loaded = True
            cached = await self.hass.async_add_executor_job(self._read_disk_cache)
            if cached:
                self._image = cached

    @property
    def available(self) -> bool:
        # The last map is still useful while the robot sleeps.
        return self._image is not None or super().available

    @property
    def frame_interval(self) -> float:
        return 2.0 if self.state_data.is_cleaning else 30.0

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.state_data.map is not None:
            key = self._render_key()
            now = time.monotonic()
            if (
                key != self._last_token_key
                and now - self._last_token_bump >= self.frame_interval
            ):
                self._last_token_key = key
                self._last_token_bump = now
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
            self._last_token_key = key
            await self.hass.async_add_executor_job(self._write_disk_cache, image)
        return self._image


class _LiveView:
    """Background pull of the Aliyun LinkVisual relay.

    Primary path emits zero-decode decrypted HEVC inside 188-byte MPEG-TS packets
    for go2rtc. Legacy JPEG decoding only activates on-demand when an MJPEG or
    snapshot caller explicitly touches ``touch_jpeg()``.
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
        self._jpeg_last_wanted = 0.0
        self._ts_lock = threading.Lock()
        self._ts_queues: set[asyncio.Queue[bytes | None]] = set()
        self._last_keyframe_ts: bytes | None = None
        self._ts_ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats: dict[str, float | int | str | None] = {
            "started_at": 0.0,
            "relay_opened_at": 0.0,
            "frames": 0,
            "keyframes": 0,
            "reconnects": 0,
            "last_frame_at": 0.0,
            "last_error": None,
            "annexb_headers_len": 0,
            "annexb_headers_hex": "",
            "decrypt_diag": {"hits": {}, "dropped": 0, "sample_dropped": []},
        }

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="narwal-liveview"
        )
        self._thread.start()

    def touch(self) -> None:
        self._last_consumer = time.monotonic()

    def touch_jpeg(self) -> None:
        now = time.monotonic()
        self._last_consumer = now
        self._jpeg_last_wanted = now

    def subscribe_ts(self) -> asyncio.Queue[bytes | None]:
        """Register an asyncio subscriber queue for MPEG-TS chunks."""
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=120)
        with self._ts_lock:
            self._last_consumer = time.monotonic()
            if self._last_keyframe_ts is not None:
                _enqueue_drop_oldest(q, self._last_keyframe_ts)
            if self._dead:
                _enqueue_drop_oldest(q, None)
            self._ts_queues.add(q)
        return q

    def unsubscribe_ts(self, q: asyncio.Queue[bytes | None]) -> None:
        with self._ts_lock:
            self._ts_queues.discard(q)
            if not self._ts_queues:
                self._last_consumer = time.monotonic()

    @property
    def dead(self) -> bool:
        return self._dead

    def wait_next(self, seq: int, timeout: float) -> tuple[int, bytes | None]:
        """Wait for a JPEG frame newer than ``seq``; returns (new_seq, jpeg|None)."""
        self.touch_jpeg()
        with self._cond:
            self._cond.wait_for(
                lambda: self._seq != seq or self._dead, timeout=timeout
            )
            now = time.monotonic()
            self._last_consumer = now
            self._jpeg_last_wanted = now
            return self._seq, self._frame

    def stop(self) -> None:
        self._stop = True
        with self._cond:
            self._cond.notify_all()

    def _has_active_consumers(self) -> bool:
        now = time.monotonic()
        with self._ts_lock:
            has_ts = bool(self._ts_queues)
        if has_ts:
            self._last_consumer = now
            return True
        return (now - self._jpeg_last_wanted) < LIVE_IDLE_SECONDS

    def _run(self) -> None:
        cont = None
        produced_any = False
        hb_stop = threading.Event()
        self.stats["started_at"] = round(time.time(), 3)
        try:
            cloud = self._cloud
            if self._client.probably_asleep():
                _LOGGER.info("Live view: robot appears asleep, sending wake()")
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._client.wake(), self._hass.loop
                    ).result(timeout=25)
                except Exception as wake_err:  # noqa: BLE001
                    _LOGGER.warning("Live view: wake attempt failed: %s", wake_err)

            token = cloud.ensure_valid_token()
            ident = fetch_media_identity(
                cloud._api_host,
                token,
                self._client.product_key,
                self._client.device_name,
            )
            _LOGGER.info(
                "Live view: media identity %s/%s",
                ident.product_key,
                ident.device_name,
            )
            asyncio.run_coroutine_threadsafe(
                self._client.start_video_viewer(), self._hass.loop
            ).result(timeout=10)
            _LOGGER.info("Live view: streaming_command sent")

            def _hb_loop() -> None:
                while not hb_stop.is_set() and not self._stop:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._client.video_heartbeat(), self._hass.loop
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    if hb_stop.wait(LIVE_HEARTBEAT_SECONDS):
                        break

            hb_thread = threading.Thread(
                target=_hb_loop, daemon=True, name="narwal-liveview-hb"
            )
            hb_thread.start()

            muxer = _HevcMpegTsMuxer()
            annexb_headers = b""
            inline_params: dict[int, bytes] = {}
            last_pts_90k: int | None = None
            zero_frame_attempts = 0
            attempt_idx = 0

            while not self._stop:
                attempt_idx += 1
                if attempt_idx > 1 and not self._has_active_consumers():
                    _LOGGER.info("Live view: no consumers left before reconnect, stopping")
                    break

                session = LinkVisualSession(
                    cloud.session.user_uuid, ident.device_name
                ).connect()
                key, iv = session.key_iv
                _LOGGER.info(
                    "Live view: opening relay (attempt %d) %s",
                    attempt_idx,
                    session.relay_url[:60],
                )

                # Fail fast (20s instead of 120s) so go2rtc never stalls behind a zombie _LiveView;
                # mid-wait, re-send start=True once and refresh LinkVisualSession in case RTMP rolled.
                open_deadline = time.monotonic() + 20.0
                open_tries = 0
                resent_start = False
                cont = None
                vs = None
                while True:
                    with self._ts_lock:
                        has_ts_subs = bool(self._ts_queues)
                    if self._stop or (
                        not has_ts_subs
                        and time.monotonic() - self._last_consumer > LIVE_STARTUP_GRACE
                    ):
                        raise RuntimeError("live view aborted during startup (no consumers)")
                    try:
                        cont, vs = open_video_stream(session.relay_url)
                        break
                    except Exception as open_err:
                        cont = None
                        open_tries += 1
                        if self._stop or time.monotonic() > open_deadline:
                            raise RuntimeError(
                                f"relay never started carrying data within 20s ({open_err})"
                            ) from open_err
                        if open_tries >= 3 and not resent_start:
                            resent_start = True
                            _LOGGER.info(
                                "Live view: relay open pending (try %d), re-sending start_video_viewer and refreshing session",
                                open_tries,
                            )
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    self._client.start_video_viewer(start=True),
                                    self._hass.loop,
                                ).result(timeout=8)
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                session = LinkVisualSession(
                                    cloud.session.user_uuid, ident.device_name
                                ).connect()
                                key, iv = session.key_iv
                            except Exception:  # noqa: BLE001
                                pass
                        time.sleep(2.0)

                self.stats["relay_opened_at"] = round(time.time(), 3)
                ex_dec = decrypt_extradata(
                    bytes(vs.codec_context.extradata or b""), key, iv
                )
                new_headers = hvcc_to_annexb(ex_dec)
                if new_headers:
                    _bad = False
                    for _part in new_headers.split(b"\x00\x00\x00\x01"):
                        if not _part:
                            continue
                        if (
                            len(_part) < 2
                            or (_part[0] & 0x80)
                            or (_part[0] & 0x01)
                            or _part[1] != 0x01
                            or len(_part) > 256
                        ):
                            _bad = True
                            break
                    if _bad:
                        _LOGGER.warning(
                            "Live view: hvcC headers look corrupt (%s), keeping previous/inline params",
                            new_headers[:24].hex(),
                        )
                    else:
                        annexb_headers = new_headers
                self.stats["annexb_headers_len"] = len(annexb_headers)
                self.stats["annexb_headers_hex"] = annexb_headers[:24].hex()
                _LOGGER.info(
                    "Live view: relay open, extradata=%dB annexb_headers=%dB",
                    len(ex_dec),
                    len(annexb_headers),
                )

                codec = None
                attempt_frames = 0
                base_pts_90k: int | None = None
                pts_offset = (last_pts_90k + 4500) if last_pts_90k is not None else 0
                stream_t0 = time.monotonic()
                idle_exit = False
                stream_err: Exception | None = None

                try:
                    for payload, pts, dts in iter_nal_payloads(
                        cont, vs, key, iv, diag=self.stats["decrypt_diag"]
                    ):
                        now = time.monotonic()
                        with self._ts_lock:
                            ts_subs = list(self._ts_queues)
                        if ts_subs:
                            self._last_consumer = now

                        if self._stop:
                            idle_exit = True
                            break
                        if not ts_subs:
                            idle_limit = (
                                LIVE_IDLE_SECONDS
                                if (now - self._jpeg_last_wanted < LIVE_IDLE_SECONDS)
                                else LIVE_TS_IDLE_SECONDS
                            )
                            if now - self._last_consumer > idle_limit:
                                idle_exit = True
                                break

                        nal = payload[4:]
                        if not nal:
                            continue

                        nal_type = (nal[0] >> 1) & 0x3F
                        if nal_type in (32, 33, 34):
                            if (
                                not annexb_headers
                                and len(nal) <= 256
                                and (nal[0] & 0x81) == 0
                                and nal[1] == 0x01
                            ):
                                inline_params[nal_type] = b"\x00\x00\x00\x01" + nal
                            continue
                        if nal_type in (35, 39, 40):
                            continue

                        is_keyframe = 16 <= nal_type <= 21
                        inline_hdr = b"".join(
                            inline_params[k] for k in (32, 33, 34) if k in inline_params
                        )
                        kf_hdr = annexb_headers or inline_hdr
                        if is_keyframe and kf_hdr:
                            au = kf_hdr + b"\x00\x00\x00\x01" + nal
                        else:
                            au = b"\x00\x00\x00\x01" + nal

                        if pts is not None and vs.time_base is not None:
                            raw_pts = int(pts * vs.time_base * 90000)
                        else:
                            raw_pts = int((now - stream_t0) * 90000)
                        if base_pts_90k is None:
                            base_pts_90k = raw_pts
                        pts_90k = pts_offset + max(0, raw_pts - base_pts_90k)
                        if last_pts_90k is not None and pts_90k <= last_pts_90k:
                            pts_90k = last_pts_90k + 1
                        last_pts_90k = pts_90k

                        ts_chunk = muxer.mux_access_unit(au, pts_90k, is_keyframe=is_keyframe)
                        attempt_frames += 1
                        zero_frame_attempts = 0
                        self.stats["frames"] = int(self.stats["frames"] or 0) + 1
                        if is_keyframe:
                            self.stats["keyframes"] = int(self.stats["keyframes"] or 0) + 1
                        self.stats["last_frame_at"] = round(time.time(), 3)
                        if not produced_any:
                            produced_any = True
                            _LOGGER.info(
                                "Live view: first MPEG-TS unit emitted (%dB, keyframe=%s)",
                                len(ts_chunk),
                                is_keyframe,
                            )

                        if is_keyframe or self._last_keyframe_ts is None:
                            with self._ts_lock:
                                self._last_keyframe_ts = ts_chunk
                        self._ts_ready.set()

                        if ts_subs:
                            for q in ts_subs:
                                self._hass.loop.call_soon_threadsafe(
                                    _enqueue_drop_oldest, q, ts_chunk
                                )

                        # Legacy JPEG path only if an MJPEG/snapshot consumer asked recently
                        if _HAS_AV and (now - self._jpeg_last_wanted < 20.0):
                            if codec is None:
                                codec = av.CodecContext.create("hevc", "r")
                                codec.extradata = ex_dec
                                codec.open()
                            packet = av.Packet(payload)
                            packet.pts, packet.dts = pts, dts
                            try:
                                frames = codec.decode(packet)
                            except Exception:  # noqa: BLE001
                                continue
                            for frame in frames:
                                buf = io.BytesIO()
                                frame.to_image().save(buf, format="JPEG", quality=85)
                                with self._cond:
                                    self._frame = buf.getvalue()
                                    self._seq += 1
                                    self._cond.notify_all()
                except Exception as iter_err:  # noqa: BLE001
                    stream_err = iter_err
                finally:
                    if cont is not None:
                        try:
                            cont.close()
                        except Exception:  # noqa: BLE001
                            pass
                        cont = None

                if self._stop or idle_exit:
                    break

                if not self._has_active_consumers():
                    _LOGGER.info("Live view: relay ended and no consumers active, exiting")
                    break

                if attempt_frames == 0:
                    zero_frame_attempts += 1
                else:
                    zero_frame_attempts = 0

                if zero_frame_attempts >= 5:
                    self.stats["last_error"] = (
                        f"relay ended 5 times with 0 frames ({stream_err or 'eof'})"
                    )
                    _LOGGER.warning("Live view: %s", self.stats["last_error"])
                    break

                self.stats["reconnects"] = int(self.stats["reconnects"] or 0) + 1
                self.stats["last_error"] = (
                    f"relay reconnect #{self.stats['reconnects']}: {stream_err or 'eof'}"
                )
                _LOGGER.warning(
                    "Live view: %s (attempt_frames=%d, keeping muxer & subscribers alive)",
                    self.stats["last_error"],
                    attempt_frames,
                )
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._client.start_video_viewer(start=True), self._hass.loop
                    ).result(timeout=8)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1.0)
        except Exception as err:  # noqa: BLE001 - fatal failure ends the view
            self.stats["last_error"] = str(err)
            _LOGGER.warning("Live view ended with error: %s", err, exc_info=True)
        finally:
            hb_stop.set()
            self._dead = True
            with self._ts_lock:
                end_subs = list(self._ts_queues)
            for q in end_subs:
                try:
                    self._hass.loop.call_soon_threadsafe(_enqueue_drop_oldest, q, None)
                except Exception:  # noqa: BLE001
                    pass
            if not produced_any and self._seq == 0:
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
            self._ts_ready.set()
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

    def _registry_keys(self) -> tuple[str, str, str]:
        return (
            self.coordinator.client.product_key.lower(),
            self.coordinator.client.device_name.lower(),
            self.entity_id,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        cams = self.hass.data.setdefault("narwal_live_cameras", {})
        for k in self._registry_keys():
            if k:
                cams[k] = self

    async def async_will_remove_from_hass(self) -> None:
        cams = self.hass.data.get("narwal_live_cameras", {})
        for k in self._registry_keys():
            if cams.get(k) is self:
                cams.pop(k, None)
        if self._live is not None:
            self._live.stop()

    async def _ensure_live(self, want_jpeg: bool = True) -> _LiveView | None:
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
            if want_jpeg:
                self._live.touch_jpeg()
            else:
                self._live.touch()
            return self._live

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        live = await self._ensure_live(want_jpeg=True)
        if live is None:
            return None
        _, frame = await self.hass.async_add_executor_job(
            live.wait_next, 0, LIVE_FRAME_TIMEOUT
        )
        return frame

    async def handle_async_ts_stream(
        self, request: web.Request
    ) -> web.StreamResponse:
        """Serve raw zero-decode decrypted HEVC wrapped in MPEG-TS for go2rtc."""
        live = await self._ensure_live(want_jpeg=False)
        if live is None:
            return web.Response(status=503, text="live view unavailable")
        queue = live.subscribe_ts()
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "video/mp2t",
                "Cache-Control": "no-store, no-cache",
            },
        )
        await response.prepare(request)
        try:
            if queue.empty() and not live.dead:
                try:
                    first_chunk = await asyncio.wait_for(queue.get(), timeout=5.0)
                    if first_chunk is not None:
                        await response.write(first_chunk)
                except asyncio.TimeoutError:
                    await response.write(_HevcMpegTsMuxer().build_init_tables())
            elif queue.empty():
                await response.write(_HevcMpegTsMuxer().build_init_tables())

            while not live.dead:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    if live.dead:
                        break
                    continue
                if chunk is None:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError, ConnectionError):
            pass
        finally:
            live.unsubscribe_ts(queue)
        return response

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse:
        live = await self._ensure_live(want_jpeg=True)
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


class NarwalLiveTsView(HomeAssistantView):
    """Loopback-only MPEG-TS endpoint consumed locally by go2rtc."""

    url = "/api/narwal/live_ts/{device}"
    name = "api:narwal:live_ts"
    requires_auth = False

    async def get(self, request: web.Request, device: str) -> web.StreamResponse:
        if "X-Forwarded-For" in request.headers or request.remote not in (
            "127.0.0.1",
            "::1",
        ):
            return web.Response(status=403, text="loopback only")
        peername = request.transport.get_extra_info("peername") if request.transport else None
        peer_ip = peername[0] if isinstance(peername, tuple) and peername else None
        if peer_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return web.Response(status=403, text="loopback only")

        hass: HomeAssistant = request.app["hass"]
        cam = hass.data.get("narwal_live_cameras", {}).get(device.lower())
        if cam is None:
            return web.Response(status=404, text="camera not found")
        return await cam.handle_async_ts_stream(request)


class NarwalLiveStatsView(HomeAssistantView):
    """Loopback-only JSON telemetry for diagnosing live_ts frame delivery."""

    url = "/api/narwal/live_stats/{device}"
    name = "api:narwal:live_stats"
    requires_auth = False

    async def get(self, request: web.Request, device: str) -> web.Response:
        if "X-Forwarded-For" in request.headers or request.remote not in (
            "127.0.0.1",
            "::1",
        ):
            return web.Response(status=403, text="loopback only")
        peername = request.transport.get_extra_info("peername") if request.transport else None
        peer_ip = peername[0] if isinstance(peername, tuple) and peername else None
        if peer_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return web.Response(status=403, text="loopback only")

        hass: HomeAssistant = request.app["hass"]
        cam = hass.data.get("narwal_live_cameras", {}).get(device.lower())
        if cam is None:
            return web.json_response({"error": "camera not found"}, status=404)
        live = cam._live
        if live is None:
            return web.json_response({"active": False, "dead": True, "subscribers": 0, "stats": None})
        with live._ts_lock:
            subs = len(live._ts_queues)
        return web.json_response({
            "active": True,
            "dead": live.dead,
            "subscribers": subs,
            "stats": live.stats,
        })
