from __future__ import annotations

import gzip
import io
import math
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


@dataclass(slots=True)
class NbtTag:
    type_id: int
    value: Any


@dataclass(slots=True)
class NbtDocument:
    name: str
    root: NbtTag
    compression: str = "raw"


def read_nbt_file(path: Path) -> NbtDocument:
    data = path.read_bytes()
    doc = read_nbt_bytes(data)
    doc.compression = "gzip" if data[:2] == b"\x1f\x8b" else "raw"
    return doc


def write_nbt_file(path: Path, doc: NbtDocument) -> None:
    raw = write_nbt_bytes(doc.name, doc.root)
    if doc.compression == "gzip":
        raw = gzip.compress(raw)
    _write_bytes_atomic(path, raw)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    # Write to a sibling temp file first so a mid-write crash cannot destroy
    # the existing file.
    tmp = path.with_name(path.name + ".mc-hanhua-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_nbt_bytes(data: bytes) -> NbtDocument:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    stream = io.BytesIO(data)
    type_id = _read_u8(stream)
    if type_id != TAG_COMPOUND:
        raise ValueError("NBT root must be a compound")
    name = _read_string(stream)
    return NbtDocument(name=name, root=NbtTag(type_id, _read_payload(stream, type_id)))


def write_nbt_bytes(name: str, root: NbtTag) -> bytes:
    stream = io.BytesIO()
    _write_u8(stream, root.type_id)
    _write_string(stream, name)
    _write_payload(stream, root)
    return stream.getvalue()


def get_tag(root: NbtTag, path: list[str | int]) -> NbtTag:
    cur = root
    for part in path:
        if cur.type_id == TAG_COMPOUND:
            cur = cur.value[str(part)]
        elif cur.type_id == TAG_LIST:
            cur = cur.value[1][int(part)]
        else:
            raise KeyError(path)
    return cur


def set_string(root: NbtTag, path: list[str | int], value: str) -> None:
    tag = get_tag(root, path)
    if tag.type_id != TAG_STRING:
        raise TypeError("target NBT tag is not a string")
    tag.value = value


def iter_children(tag: NbtTag) -> list[tuple[str | int, NbtTag]]:
    if tag.type_id == TAG_COMPOUND:
        return list(tag.value.items())
    if tag.type_id == TAG_LIST:
        return list(enumerate(tag.value[1]))
    return []


def read_region_chunks(path: Path) -> list[dict[str, Any]]:
    data = path.read_bytes()
    if len(data) < 8192:
        raise ValueError("Region file is shorter than the 8192-byte header")
    chunks: list[dict[str, Any]] = []
    for index in range(1024):
        loc = data[index * 4 : index * 4 + 4]
        offset = int.from_bytes(loc[:3], "big")
        sectors = loc[3]
        timestamp = int.from_bytes(data[4096 + index * 4 : 4100 + index * 4], "big")
        if offset == 0 and sectors == 0:
            continue
        if offset == 0 or sectors == 0:
            raise ValueError(f"Region chunk {index} has an incomplete location entry")
        start = offset * 4096
        if start + 5 > len(data):
            raise ValueError(f"Region chunk {index} points outside file")
        length = int.from_bytes(data[start : start + 4], "big")
        compression = data[start + 4]
        if length < 1 or start + 4 + length > start + sectors * 4096 or start + 4 + length > len(data):
            raise ValueError(f"Region chunk {index} has invalid payload length")
        if start + sectors * 4096 > len(data):
            # A short raw_chunk would shift every later chunk on rewrite, so
            # declared sectors past EOF must fail closed.
            raise ValueError(f"Region chunk {index} declares sectors past the end of file")
        payload = data[start + 5 : start + 4 + length]
        raw_chunk = data[start : start + sectors * 4096]
        try:
            if compression == 1:
                nbt_data = gzip.decompress(payload)
            elif compression == 2:
                nbt_data = zlib.decompress(payload)
            elif compression == 3:
                nbt_data = payload
            else:
                raise ValueError(f"Region chunk {index} uses unsupported compression {compression}")
            doc = read_nbt_bytes(nbt_data)
        except Exception as exc:
            raise ValueError(f"Unable to parse region chunk {index}") from exc
        chunks.append(
            {
                "index": index,
                "timestamp": timestamp,
                "raw": raw_chunk,
                "doc": doc,
                "changed": False,
            }
        )
    return chunks


def write_region_chunks(path: Path, chunks: list[dict[str, Any]]) -> None:
    locations = bytearray(4096)
    timestamps = bytearray(4096)
    body = bytearray()
    sector = 2
    for chunk in chunks:
        if chunk.get("changed"):
            raw_nbt = write_nbt_bytes(chunk["doc"].name, chunk["doc"].root)
            payload = zlib.compress(raw_nbt)
            chunk_data = len(payload + b"\x02").to_bytes(4, "big") + b"\x02" + payload
            sectors = int(math.ceil(len(chunk_data) / 4096))
            chunk_data += b"\x00" * (sectors * 4096 - len(chunk_data))
        else:
            chunk_data = bytes(chunk["raw"])
            sectors = len(chunk_data) // 4096
        index = int(chunk["index"])
        locations[index * 4 : index * 4 + 4] = sector.to_bytes(3, "big") + bytes([sectors])
        timestamps[4096 + index * 4 - 4096 : 4096 + index * 4 - 4092] = int(chunk["timestamp"]).to_bytes(4, "big")
        body.extend(chunk_data)
        sector += sectors
    _write_bytes_atomic(path, bytes(locations) + bytes(timestamps) + bytes(body))


def _read_payload(stream: BinaryIO, type_id: int) -> Any:
    if type_id == TAG_BYTE:
        return _unpack(stream, ">b", 1)
    if type_id == TAG_SHORT:
        return _unpack(stream, ">h", 2)
    if type_id == TAG_INT:
        return _unpack(stream, ">i", 4)
    if type_id == TAG_LONG:
        return _unpack(stream, ">q", 8)
    if type_id == TAG_FLOAT:
        return _unpack(stream, ">f", 4)
    if type_id == TAG_DOUBLE:
        return _unpack(stream, ">d", 8)
    if type_id == TAG_BYTE_ARRAY:
        length = _unpack(stream, ">i", 4)
        return list(stream.read(length))
    if type_id == TAG_STRING:
        return _read_string(stream)
    if type_id == TAG_LIST:
        elem_type = _read_u8(stream)
        length = _unpack(stream, ">i", 4)
        return (elem_type, [NbtTag(elem_type, _read_payload(stream, elem_type)) for _ in range(length)])
    if type_id == TAG_COMPOUND:
        values: dict[str, NbtTag] = {}
        while True:
            child_type = _read_u8(stream)
            if child_type == TAG_END:
                break
            name = _read_string(stream)
            values[name] = NbtTag(child_type, _read_payload(stream, child_type))
        return values
    if type_id == TAG_INT_ARRAY:
        length = _unpack(stream, ">i", 4)
        return [_unpack(stream, ">i", 4) for _ in range(length)]
    if type_id == TAG_LONG_ARRAY:
        length = _unpack(stream, ">i", 4)
        return [_unpack(stream, ">q", 8) for _ in range(length)]
    raise ValueError(f"unsupported NBT tag {type_id}")


def _write_payload(stream: BinaryIO, tag: NbtTag) -> None:
    value = tag.value
    if tag.type_id == TAG_BYTE:
        stream.write(struct.pack(">b", int(value)))
    elif tag.type_id == TAG_SHORT:
        stream.write(struct.pack(">h", int(value)))
    elif tag.type_id == TAG_INT:
        stream.write(struct.pack(">i", int(value)))
    elif tag.type_id == TAG_LONG:
        stream.write(struct.pack(">q", int(value)))
    elif tag.type_id == TAG_FLOAT:
        stream.write(struct.pack(">f", float(value)))
    elif tag.type_id == TAG_DOUBLE:
        stream.write(struct.pack(">d", float(value)))
    elif tag.type_id == TAG_BYTE_ARRAY:
        stream.write(struct.pack(">i", len(value)))
        stream.write(bytes(value))
    elif tag.type_id == TAG_STRING:
        _write_string(stream, str(value))
    elif tag.type_id == TAG_LIST:
        elem_type, items = value
        _write_u8(stream, int(elem_type))
        stream.write(struct.pack(">i", len(items)))
        for item in items:
            _write_payload(stream, item)
    elif tag.type_id == TAG_COMPOUND:
        for name, child in value.items():
            _write_u8(stream, child.type_id)
            _write_string(stream, name)
            _write_payload(stream, child)
        _write_u8(stream, TAG_END)
    elif tag.type_id == TAG_INT_ARRAY:
        stream.write(struct.pack(">i", len(value)))
        for item in value:
            stream.write(struct.pack(">i", int(item)))
    elif tag.type_id == TAG_LONG_ARRAY:
        stream.write(struct.pack(">i", len(value)))
        for item in value:
            stream.write(struct.pack(">q", int(item)))
    else:
        raise ValueError(f"unsupported NBT tag {tag.type_id}")


def _read_string(stream: BinaryIO) -> str:
    length = _unpack(stream, ">H", 2)
    data = stream.read(length)
    if len(data) != length:
        raise EOFError("unexpected EOF in NBT string")
    return _decode_mutf8(data)


def _write_string(stream: BinaryIO, value: str) -> None:
    data = _encode_mutf8(value)
    if len(data) > 0xFFFF:
        raise ValueError("NBT string exceeds 65535 encoded bytes")
    stream.write(struct.pack(">H", len(data)))
    stream.write(data)


def _decode_mutf8(data: bytes) -> str:
    """Decode Java modified UTF-8: NUL as C0 80, supplementary as CESU-8 pairs.

    Standard 4-byte UTF-8 is tolerated on read (Java writeUTF never emits it)
    but re-encodes as CESU-8. Any other invalid byte raises so corrupt data
    fails closed instead of being silently mangled and rewritten.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    chars: list[str] = []
    i = 0
    total = len(data)
    while i < total:
        b0 = data[i]
        if b0 < 0x80:
            chars.append(chr(b0))
            i += 1
        elif b0 & 0xE0 == 0xC0:
            if i + 2 > total or data[i + 1] & 0xC0 != 0x80:
                raise ValueError(f"invalid modified UTF-8 at byte {i}")
            code = ((b0 & 0x1F) << 6) | (data[i + 1] & 0x3F)
            if 0 < code < 0x80:
                raise ValueError(f"invalid modified UTF-8 at byte {i}")
            chars.append(chr(code))
            i += 2
        elif b0 & 0xF0 == 0xE0:
            code = _decode_mutf8_triple(data, i)
            i += 3
            if 0xD800 <= code <= 0xDBFF and i + 3 <= total and data[i] & 0xF0 == 0xE0:
                low = _decode_mutf8_triple(data, i)
                if 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    i += 3
            chars.append(chr(code))
        elif b0 & 0xF8 == 0xF0:
            if i + 4 > total or any(data[j] & 0xC0 != 0x80 for j in range(i + 1, i + 4)):
                raise ValueError(f"invalid modified UTF-8 at byte {i}")
            code = ((b0 & 0x07) << 18) | ((data[i + 1] & 0x3F) << 12) | ((data[i + 2] & 0x3F) << 6) | (data[i + 3] & 0x3F)
            if code < 0x10000 or code > 0x10FFFF:
                raise ValueError(f"invalid modified UTF-8 at byte {i}")
            chars.append(chr(code))
            i += 4
        else:
            raise ValueError(f"invalid modified UTF-8 at byte {i}")
    return "".join(chars)


def _decode_mutf8_triple(data: bytes, i: int) -> int:
    if i + 3 > len(data) or data[i + 1] & 0xC0 != 0x80 or data[i + 2] & 0xC0 != 0x80:
        raise ValueError(f"invalid modified UTF-8 at byte {i}")
    code = ((data[i] & 0x0F) << 12) | ((data[i + 1] & 0x3F) << 6) | (data[i + 2] & 0x3F)
    if code < 0x800:
        raise ValueError(f"invalid modified UTF-8 at byte {i}")
    return code


def _encode_mutf8(value: str) -> bytes:
    """Encode Java modified UTF-8; inverse of _decode_mutf8 for round-trip safety."""
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError:
        pass
    else:
        if not data or (max(data) < 0xF0 and 0 not in data):
            return data
    out = bytearray()
    for ch in value:
        code = ord(ch)
        if code == 0:
            out += b"\xc0\x80"
        elif code < 0x10000:
            out += ch.encode("utf-8", "surrogatepass")
        else:
            code -= 0x10000
            out += chr(0xD800 | (code >> 10)).encode("utf-8", "surrogatepass")
            out += chr(0xDC00 | (code & 0x3FF)).encode("utf-8", "surrogatepass")
    return bytes(out)


def _read_u8(stream: BinaryIO) -> int:
    data = stream.read(1)
    if not data:
        raise EOFError("unexpected EOF")
    return data[0]


def _write_u8(stream: BinaryIO, value: int) -> None:
    stream.write(bytes([value]))


def _unpack(stream: BinaryIO, fmt: str, size: int) -> Any:
    data = stream.read(size)
    if len(data) != size:
        raise EOFError("unexpected EOF")
    return struct.unpack(fmt, data)[0]
