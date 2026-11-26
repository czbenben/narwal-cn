"""Map renderer for Narwal vacuum — converts raw map data to PNG bytes.

Map data format (confirmed from live robot data):
  - Field 17 of the map protobuf is compressed with zlib
  - Decompressed data is a protobuf wrapper: field 1 = packed repeated varints
  - Skip the protobuf header (0x0a + varint length), then decode varints
  - Each varint encodes: room_id = value >> 8, pixel_type = value & 0xFF
  - Value 0 = unknown/outside, 0x20 = unassigned floor, 0x28 = unassigned obstacle
  - pixel_type & 0x18 = wall/obstacle (rendered as a deeper shade + hatch,
    like the app); other room pixels are floor
"""

from __future__ import annotations

import colorsys
import io
import logging
import math
import zlib
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Pastel room palette on a near-white background, matching the app's light theme.
ROOM_COLORS: list[tuple[int, int, int]] = [
    (167, 201, 245),  # soft blue
    (169, 224, 197),  # mint green
    (215, 190, 240),  # lavender
    (245, 190, 200),  # soft pink
    (250, 224, 170),  # soft peach
    (170, 220, 230),  # powder blue
    (230, 210, 180),  # sand
    (195, 230, 160),  # light green
]

COLOR_BACKGROUND = (238, 240, 244)
COLOR_UNASSIGNED_FLOOR = (222, 226, 233)
COLOR_OBSTACLE = (190, 196, 208)
COLOR_FALLBACK = (222, 226, 233)
COLOR_LABEL = (60, 64, 72)
COLOR_ROBOT = (255, 255, 255)
COLOR_ROBOT_OUTLINE = (32, 120, 255)
COLOR_STATION = (255, 176, 32)

_FONT_PATH = Path(__file__).parent / "assets" / "NotoSansSC-Regular-Subset.otf"
_SYSTEM_CJK_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def _load_font(size: int):
    from PIL import ImageFont

    for path in (_FONT_PATH, *_SYSTEM_CJK_FONTS):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _solid_shade(color: tuple[int, int, int], lightness: float = 0.87, saturation: float = 1.1) -> tuple[int, int, int]:
    """Deeper shade of a room color that keeps its hue (the app's wall band)."""
    r, g, b = (x / 255 for x in color)
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    return tuple(
        int(x * 255)
        for x in colorsys.hls_to_rgb(h, min(1.0, light * lightness), min(1.0, sat * saturation))
    )


def _decode_packed_varints(data: bytes) -> list[int]:
    """Decode protobuf packed repeated varint field.

    The decompressed grid starts with a protobuf header:
      byte 0: 0x0a (field 1, wire type 2 = length-delimited)
      bytes 1+: varint length
    After the header, remaining bytes are packed varint pixel values.
    """
    if len(data) < 4:
        return []

    pos = 0
    if data[0] == 0x0A:
        pos = 1
        while pos < len(data) and data[pos] & 0x80:
            pos += 1
        pos += 1

    pixels: list[int] = []
    while pos < len(data):
        val = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        pixels.append(val)
    return pixels


def render_snapshot(
    snapshot,
    room_names: dict[int, str] | None = None,
    robot_pose=None,
    active_room_id: int | None = None,
    scale: int = 4,
) -> bytes:
    """Render a MapSnapshot with dock and robot markers, rotated like the app."""
    markers = []
    if snapshot.station:
        markers.append(("station", snapshot.to_pixel(snapshot.station), snapshot.station.theta))
    pose = robot_pose or snapshot.robot
    if pose:
        markers.append(("robot", snapshot.to_pixel(pose), pose.theta))
    return render_map(
        snapshot.grid, snapshot.width, snapshot.height, room_names,
        scale=scale, markers=markers, rotate=snapshot.rotate_angle,
        highlight_room=active_room_id,
    )


def _draw_markers_at(img, markers, scale: int) -> None:
    """Draw (kind, x, y, heading) markers given in final image coordinates."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    for kind, x, y, theta in markers:
        if kind == "station":
            r = 2.6 * scale
            draw.rounded_rectangle(
                [x - r, y - r, x + r, y + r], radius=r * 0.4,
                fill=(255, 255, 255), outline=(90, 100, 115), width=max(1, scale // 2),
            )
            draw.rectangle(
                [x - r * 0.45, y - r * 0.45, x + r * 0.45, y + r * 0.45], fill=COLOR_STATION,
            )
        else:
            r = 3.2 * scale
            width = max(1, int(scale * 0.7))
            draw.ellipse([x - r, y - r, x + r, y + r], fill=COLOR_ROBOT, outline=COLOR_ROBOT_OUTLINE, width=width)
            hx = x + math.cos(theta) * r * 0.85
            hy = y - math.sin(theta) * r * 0.85
            draw.line([x, y, hx, hy], fill=COLOR_ROBOT_OUTLINE, width=width)


def render_map(
    compressed_grid: bytes,
    width: int,
    height: int,
    room_names: dict[int, str] | None = None,
    scale: int = 4,
    markers: list | None = None,
    rotate: int = 0,
    highlight_room: int | None = None,
) -> bytes:
    """Render compressed map grid data as a PNG image.

    Args:
        compressed_grid: Zlib-compressed grid data from map protobuf field 17.
        width: Map width in pixels (field 4).
        height: Map height in pixels (field 5).
        room_names: Optional mapping of room_id to display name for labels.
        scale: Upscale factor (pixels per grid cell, default 4).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    if not compressed_grid or width <= 0 or height <= 0:
        return b""

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering — pip install Pillow")
        return b""

    try:
        decompressed = zlib.decompress(compressed_grid)
    except zlib.error:
        try:
            decompressed = zlib.decompress(compressed_grid, 47)
        except zlib.error:
            _LOGGER.warning("Could not decompress map grid (%d bytes)", len(compressed_grid))
            return b""

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        _LOGGER.debug("Map has %d pixels, expected %d — padding", len(pixels), expected)
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    img = Image.new("RGB", (width, height), COLOR_BACKGROUND)
    px = img.load()
    solid_mask = Image.new("L", (width, height), 0)
    mp = solid_mask.load()

    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width

        if val == 0:
            continue
        elif val == 0x20:
            px[x, y] = COLOR_UNASSIGNED_FLOOR
            continue
        elif val == 0x28:
            px[x, y] = COLOR_OBSTACLE
            mp[x, y] = 255
            continue

        room_id = (val >> 8) & 0xFFFF
        ptype = val & 0xFF

        if 1 <= room_id <= len(ROOM_COLORS):
            base = ROOM_COLORS[room_id - 1]
        elif room_id >= 1:
            base = COLOR_FALLBACK
        else:
            base = COLOR_UNASSIGNED_FLOOR

        if ptype & 0x18:
            px[x, y] = _solid_shade(base) if room_id >= 1 else COLOR_OBSTACLE
            mp[x, y] = 255
        else:
            px[x, y] = base
            if highlight_room is not None and room_id == highlight_room:
                px[x, y] = tuple(min(255, c + 45) for c in base)
            if room_names and room_id in room_names and room_id >= 1:
                room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
                room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
                room_count[room_id] = room_count.get(room_id, 0) + 1

    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    solid_mask = solid_mask.transpose(Image.FLIP_TOP_BOTTOM)
    scale = max(1, scale)

    # Anti-aliased edges: nearest-neighbor upscale 2x past the target,
    # then LANCZOS back down (supersampling).
    if scale > 1:
        img = img.resize((width * scale * 2, height * scale * 2), Image.NEAREST)
        img = img.resize((width * scale, height * scale), Image.LANCZOS)
        solid_mask = solid_mask.resize((width * scale, height * scale), Image.NEAREST)

    # Diagonal hatch over wall/obstacle zones, like the app.
    hatch = Image.new("RGBA", img.size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(hatch)
    step = 7 * scale
    for hx in range(-img.size[1], img.size[0] + step, step):
        hd.line([hx, 0, hx + img.size[1], img.size[1]], fill=(0, 0, 0, 12), width=max(1, scale // 2))
    clipped = Image.new("RGBA", img.size, (0, 0, 0, 0))
    clipped.paste(hatch, (0, 0), solid_mask)
    img = Image.alpha_composite(img.convert("RGBA"), clipped).convert("RGB")

    # Match the app's orientation. The robot's rotateAngle is clockwise
    # (verified against the app); PIL rotates counter-clockwise for positive
    # angles, so negate it. Annotation points are rotated the same way.
    rotate = (-rotate) % 360
    w_img, h_img = img.size
    if rotate in (90, 180, 270):
        img = img.rotate(rotate, expand=True)

    # Margin so a dock/robot sitting on the map edge isn't clipped.
    margin = 10 * scale
    padded = Image.new("RGB", (img.size[0] + 2 * margin, img.size[1] + 2 * margin), COLOR_BACKGROUND)
    padded.paste(img, (margin, margin))
    img = padded

    def _rot(x: float, y: float) -> tuple[float, float]:
        if rotate == 90:
            x, y = y, w_img - x
        elif rotate == 180:
            x, y = w_img - x, h_img - y
        elif rotate == 270:
            x, y = h_img - y, x
        return x + margin, y + margin

    if markers:
        rotated = []
        for kind, (gx, gy), theta in markers:
            x = (gx + 0.5) * scale
            y = (height - 1 - gy + 0.5) * scale
            rx, ry = _rot(x, y)
            rotated.append((kind, rx, ry, theta + math.radians(rotate)))
        _draw_markers_at(img, rotated, scale)

    if room_names and room_count:
        draw = ImageDraw.Draw(img)
        font = _load_font(int(4.2 * scale))
        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            cx = (room_sum_x[rid] / room_count[rid] + 0.5) * scale
            cy = (height - 1 - room_sum_y[rid] / room_count[rid] + 0.5) * scale
            tx, ty = _rot(cx, cy)
            bbox = draw.textbbox((0, 0), name, font=font)
            tx -= (bbox[2] - bbox[0]) / 2
            ty -= (bbox[3] - bbox[1]) / 2
            draw.text((tx, ty), name, fill=COLOR_LABEL, font=font,
                      stroke_width=max(1, scale // 3), stroke_fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
