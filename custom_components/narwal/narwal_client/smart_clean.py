"""Byte-level patching of the ``smartCleanParam`` config blob.

``smartCleanParam`` is an embedded protobuf the app schema doesn't define.
Each setting is a length-delimited top-level field whose value is a small
submessage: an empty submessage means "smart" (auto), ``{1: 1, 2: v}``
pins value ``v``. Field 7 (stain remover) uses ``{1: 1, 2: mode,
3: minutes}`` where field 1's presence is the on/off switch. Every other
field is preserved byte-for-byte.
"""

from __future__ import annotations

from .proto import ProtoError, encode_varint, read_varint

# smartCleanParam top-level fields.
FIELD_MOP_REPEAT_LIMIT = 1
FIELD_FINENESS = 2
FIELD_SUCTION = 4
FIELD_MOP_HUMIDITY = 5
FIELD_STAIN_REMOVER = 7

STAIN_REMOVER_MODES = ("every_time", "every_7_days", "every_14_days", "off")
_STAIN_MINUTES = {"every_7_days": 10080, "every_14_days": 20160}


def _fields(data: bytes):
    """Yield (field_number, wire_type, decoded_value, raw_field_bytes)."""
    pos, end = 0, len(data)
    while pos < end:
        start = pos
        tag, pos = read_varint(data, pos)
        num, wire = tag >> 3, tag & 7
        if num == 0:
            raise ProtoError("field number 0")
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 2:
            length, pos = read_varint(data, pos)
            if pos + length > end:
                raise ProtoError("truncated length-delimited field")
            value, pos = data[pos : pos + length], pos + length
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ProtoError(f"unsupported wire type {wire}")
        if pos > end:
            raise ProtoError("truncated field")
        yield num, wire, value, data[start:pos]


def _varint_field(num: int, value: int) -> bytes:
    return encode_varint(num << 3) + encode_varint(value)


def _find_sub(raw: bytes, field: int) -> bytes | None:
    """Return the body of the first length-delimited ``field``, or None."""
    for num, wire, value, _ in _fields(raw):
        if num == field and wire == 2:
            return value
    return None


def _patch(raw: bytes, field: int, body: bytes | None) -> bytes:
    """Replace the first ``field`` body in place; ``body=None`` removes it.

    The field keeps its position; if absent it is appended at the end.
    """
    out = bytearray()
    done = False
    for num, _wire, _value, raw_field in _fields(raw):
        if num == field and not done:
            if body is not None:
                out += encode_varint((field << 3) | 2)
                out += encode_varint(len(body)) + body
            done = True
        else:
            out += raw_field
    if not done and body is not None:
        out += encode_varint((field << 3) | 2)
        out += encode_varint(len(body)) + body
    return bytes(out)


def get_option(raw: bytes, field: int) -> int | None:
    """Return the pinned value of a smartCleanParam setting.

    ``0`` means "smart" (empty submessage or field absent); ``None`` when
    the submessage is present but malformed.
    """
    sub = _find_sub(raw, field)
    if sub is None:
        return 0
    values = {num: v for num, wire, v, _ in _fields(sub) if wire == 0}
    if not values:
        return 0
    return values.get(2)


def set_option(raw: bytes, field: int, value: int | None) -> bytes:
    """Pin ``value`` for a setting; ``None`` resets it to "smart"."""
    body = b"" if value is None else _varint_field(1, 1) + _varint_field(2, value)
    return _patch(raw, field, body)


def get_stain_remover(raw: bytes) -> str | None:
    """Return the stain-remover mode, or None if the value is unrecognized."""
    sub = _find_sub(raw, FIELD_STAIN_REMOVER)
    if sub is None:
        return "off"
    values = {num: v for num, wire, v, _ in _fields(sub) if wire == 0}
    if 1 not in values:
        return "off"
    if values.get(2) == 1:
        return "every_time"
    if values.get(2) == 2:
        for mode, minutes in _STAIN_MINUTES.items():
            if values.get(3) == minutes:
                return mode
    return None


def set_stain_remover(raw: bytes, mode: str) -> bytes:
    """Set the stain-remover mode; "off" keeps f2/f3 but drops the enable flag."""
    if mode == "every_time":
        sub = _varint_field(1, 1) + _varint_field(2, 1)
    elif mode in _STAIN_MINUTES:
        sub = (
            _varint_field(1, 1)
            + _varint_field(2, 2)
            + _varint_field(3, _STAIN_MINUTES[mode])
        )
    elif mode == "off":
        old = _find_sub(raw, FIELD_STAIN_REMOVER)
        sub = (
            _patch(old, 1, None)
            if old is not None
            else _varint_field(2, 2) + _varint_field(3, 10080)
        )
    else:
        raise ValueError(f"unknown stain remover mode {mode!r}")
    return _patch(raw, FIELD_STAIN_REMOVER, sub)
