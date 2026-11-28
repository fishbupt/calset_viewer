#!/usr/bin/env python3
"""
Keysight PNA / PNA-X Calibration File (*.cal) Parser
Zero-dependency Python 3 implementation based on reverse-engineered MFC CArchive layout.

Usage:
    python parse_pna_cal.py calset_101pts.cal
    python parse_pna_cal.py calset_101pts.cal --export-dir ./cal_out
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import logging
import math
import re
import struct
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
CSTRINGW_MARKER = b"\xff\xfe\xff"
TRACE_FREQUENCY_HEADER = struct.Struct("<24xIBI")
LOGGER = logging.getLogger("calset_viewer.parser")


class BinaryReader:
    """Little-endian binary stream reader with MFC CArchive helpers."""

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.pos = offset
        self.length = len(data)

    def remaining(self) -> int:
        return self.length - self.pos

    def read_bytes(self, n: int) -> bytes:
        if self.pos + n > self.length:
            raise EOFError(
                f"Unexpected EOF at 0x{self.pos:X}: need {n} bytes, only {self.remaining()} left"
            )
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt: str) -> Tuple[Any, ...]:
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.read_bytes(size))

    def u8(self) -> int:
        return self.unpack("<B")[0]

    def u16(self) -> int:
        return self.unpack("<H")[0]

    def i16(self) -> int:
        return self.unpack("<h")[0]

    def u32(self) -> int:
        return self.unpack("<I")[0]

    def i32(self) -> int:
        return self.unpack("<i")[0]

    def f32(self) -> float:
        return self.unpack("<f")[0]

    def f64(self) -> float:
        return self.unpack("<d")[0]

    def read_cstringw(self) -> str:
        """Read an MFC CArchive UTF-16LE string prefixed with FF FE FF."""
        marker = self.read_bytes(3)
        if marker != CSTRINGW_MARKER:
            raise ValueError(
                f"Expected CStringW marker FF FE FF at 0x{self.pos - 3:X}, got {marker.hex(' ').upper()}"
            )
        char_len = self.u8()
        if char_len == 0xFF:
            char_len = self.u16()
            if char_len == 0xFFFF:
                char_len = self.u32()
        raw = self.read_bytes(char_len * 2)
        return raw.decode("utf-16-le")

    def peek_is_cstringw(self) -> bool:
        return self.data[self.pos : self.pos + 3] == CSTRINGW_MARKER

    def read_guid(self) -> str:
        return str(uuid.UUID(bytes_le=self.read_bytes(16)))

    def read_filetime_swapped(self) -> str:
        """Read 8-byte Windows FILETIME stored with dwHighDateTime before dwLowDateTime."""
        hi, lo = self.unpack("<II")
        ticks = (hi << 32) | lo
        if ticks == 0:
            return ""
        ts = FILETIME_EPOCH + dt.timedelta(microseconds=ticks / 10.0)
        return ts.isoformat()


def encode_cstringw(text: str) -> bytes:
    """Encode a Python str into MFC CArchive CStringW bytes (for anchor search & tests)."""
    raw = text.encode("utf-16-le")
    n = len(text)
    if n < 0xFF:
        header = CSTRINGW_MARKER + bytes([n])
    elif n < 0xFFFF:
        header = CSTRINGW_MARKER + b"\xff" + struct.pack("<H", n)
    else:
        header = CSTRINGW_MARKER + b"\xff\xff\xff" + struct.pack("<I", n)
    return header + raw


@dataclass
class ComplexTrace:
    key: str
    data_type: int
    elem_type: int
    dim_count: int
    points: List[Tuple[float, float]]
    frequencies_hz: Optional[Tuple[float, ...]] = None


@dataclass
class CalSetHeaderRecord:
    name: str
    guid: str
    timestamp_utc: str
    flags: int


@dataclass
class MappedMeasurementGroup:
    calset: CalSetHeaderRecord
    traces: List[ComplexTrace]
    error_terms: List[ComplexTrace] = field(default_factory=list)


@dataclass
class CalSetHeader:
    magic: str
    file_version: int
    sub_ver_a: int
    sub_ver_b: int
    calset_name: str
    calset_guid: str
    timestamp_utc: str
    header_flags: int
    unk_2: int
    unk_3: int
    unk_4: int
    schemas: Dict[str, int] = field(default_factory=dict)
    calset_records: List[CalSetHeaderRecord] = field(default_factory=list)


@dataclass
class StimulusHeader:
    params: Dict[int, float]
    name: str
    alias: str
    created_by: str
    description: str
    calset_guid: str
    timestamp_utc: str
    port_config: str


@dataclass
class CalSettingsInfo:
    timestamp_utc: str
    port_kits: List[Dict[str, Any]]
    core_stimulus_text: str


@dataclass
class ErrorTermSet:
    offset: int
    magic: int
    ver_a: int
    ver_b: int
    port_ids: List[int]
    port_z0: List[Tuple[float, float]]
    freqs_ghz: List[float]
    error_terms: List[List[Tuple[float, float]]]
    term_names: Dict[int, str] = field(default_factory=dict)
    data_bounds: Optional[Tuple[int, int]] = None

    def term_label(self, index: int) -> str:
        return self.term_names.get(index, f"Term {index}")


@dataclass
class ParsedCalFile:
    file_size: int
    header: CalSetHeader
    raw_measurements: List[ComplexTrace]
    stimulus_header: Optional[StimulusHeader] = None
    mapped_measurements: List[ComplexTrace] = field(default_factory=list)
    cal_settings: Optional[CalSettingsInfo] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_term_set: Optional[ErrorTermSet] = None
    mapped_measurement_groups: List[MappedMeasurementGroup] = field(default_factory=list)
    named_error_terms: List[ComplexTrace] = field(default_factory=list)
    reconstructed_error_terms: List[ComplexTrace] = field(default_factory=list)


def read_complex_float_trace(r: BinaryReader, key: str) -> ComplexTrace:
    """Parse one ComplexFloatTrace entry (Section 2 & Section 4)."""
    flag = r.u16()
    if flag != 1:
        raise ValueError(f"Unexpected trace flag {flag} for '{key}' at 0x{r.pos - 2:X}")
    outer_size = r.u32()
    outer_end = r.pos + outer_size

    _unk_a = r.u16()
    _unk_b = r.u32()
    data_type = r.u16()
    _unk_c = r.u16()
    inner_size = r.u32()
    inner_start = r.pos

    _unk_d = r.u32()
    _unk_e = r.u32()
    header_size = r.u16()
    elem_type = r.u16()
    dim_count = r.u32()
    num_points = r.u32()

    if r.pos != inner_start + header_size:
        r.pos = inner_start + header_size

    if elem_type == 3:  # ComplexFloat32
        flat = struct.unpack(f"<{num_points * 2}f", r.read_bytes(num_points * 8))
        points = [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]
    elif elem_type == 4:  # Fallback if ComplexFloat64 ever appears
        flat = struct.unpack(f"<{num_points * 2}d", r.read_bytes(num_points * 16))
        points = [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]
    else:
        raise ValueError(f"Unsupported trace elem_type={elem_type} for '{key}'")

    if r.pos > outer_end:
        raise ValueError(f"Trace '{key}' overran outer_size ({r.pos} > {outer_end})")
    if outer_end > r.length:
        raise EOFError(f"Trace '{key}' extends beyond EOF at 0x{outer_end:X}")

    frequencies_hz = None
    inner_end = inner_start + inner_size
    axis_size = num_points * 8
    axis_start = inner_end + TRACE_FREQUENCY_HEADER.size
    if num_points and r.pos <= inner_end and outer_end == axis_start + axis_size:
        axis_count, axis_type, byte_count = TRACE_FREQUENCY_HEADER.unpack_from(r.data, inner_end)
        if (axis_count, axis_type, byte_count) == (num_points, 3, axis_size):
            frequencies_hz = struct.unpack_from(f"<{num_points}d", r.data, axis_start)
    r.pos = outer_end

    return ComplexTrace(
        key=key,
        data_type=data_type,
        elem_type=elem_type,
        dim_count=dim_count,
        points=points,
        frequencies_hz=frequencies_hz,
    )


def parse_section1_header(r: BinaryReader) -> CalSetHeader:
    magic = r.read_cstringw()
    if magic != "PNA Cal File":
        raise ValueError(f"Not a Keysight PNA Cal File (magic={magic!r})")
    file_version = r.u32()
    sub_ver_a = r.u32()
    sub_ver_b = r.u32()

    # In the observed v1.3 headers the third word counts CalSet records.
    record_count = max(1, sub_ver_b) if (file_version, sub_ver_a) == (1, 3) else 1
    LOGGER.debug("Section 1 CalSet records: %d, starting at 0x%X", record_count, r.pos)
    calset_records = [
        CalSetHeaderRecord(
            name=r.read_cstringw(),
            guid=r.read_guid(),
            timestamp_utc=r.read_filetime_swapped(),
            flags=r.u32(),
        )
        for _ in range(record_count)
    ]
    first_record = calset_records[0]
    unk_2 = r.u32()
    unk_3 = r.u32()
    unk_4 = r.u16()
    schema_count = r.u32()
    LOGGER.debug("Section 1 schema entries: %d, starting at 0x%X", schema_count, r.pos)
    schemas: Dict[str, int] = {}
    for _ in range(schema_count):
        name = r.read_cstringw()
        ver = r.u32()
        schemas[name] = ver
    return CalSetHeader(
        magic=magic,
        file_version=file_version,
        sub_ver_a=sub_ver_a,
        sub_ver_b=sub_ver_b,
        calset_name=first_record.name,
        calset_guid=first_record.guid,
        timestamp_utc=first_record.timestamp_utc,
        header_flags=first_record.flags,
        unk_2=unk_2,
        unk_3=unk_3,
        unk_4=unk_4,
        schemas=schemas,
        calset_records=calset_records,
    )


def parse_trace_map(r: BinaryReader) -> List[ComplexTrace]:
    count = r.u32()
    traces: List[ComplexTrace] = []
    for _ in range(count):
        key = r.read_cstringw()
        traces.append(read_complex_float_trace(r, key))
    return traces


def parse_section3_stimulus_header(r: BinaryReader) -> StimulusHeader:
    param_count = r.u32()
    if param_count > 64:
        raise ValueError(f"Implausible stimulus param_count={param_count}")
    params: Dict[int, float] = {}
    for _ in range(param_count):
        pid = r.u32()
        val = r.f64()
        params[pid] = val
    name = r.read_cstringw()
    alias = r.read_cstringw()
    created_by = r.read_cstringw()
    description = r.read_cstringw()
    calset_guid = r.read_guid()
    _flag_1 = r.u8()
    timestamp_utc = r.read_filetime_swapped()
    _flag_0 = r.u8()
    _unk_one = r.u32()
    port_config = r.read_cstringw()
    return StimulusHeader(
        params=params,
        name=name,
        alias=alias,
        created_by=created_by,
        description=description,
        calset_guid=calset_guid,
        timestamp_utc=timestamp_utc,
        port_config=port_config,
    )


MAPPED_KEY = re.compile(
    r"(?:S[ABC](?:_\d+)?|Thru_(?:[0-9_]+|RefRatio))\([1-9]\d*,[1-9]\d*\)"
)
MAPPED_KEY_PREFIX = re.compile(
    re.escape(CSTRINGW_MARKER) + rb"[\x01-\xfe](?:S\x00[ABC]\x00[(_]\x00|"
    + re.escape("Thru_".encode("utf-16le"))
    + rb")"
)


def find_and_parse_section4_mapped(data: bytes, start_offset: int) -> List[ComplexTrace]:
    """Locate a counted mapped trace table by its first standard key."""
    for match in MAPPED_KEY_PREFIX.finditer(data, start_offset + 4):
        offset = match.start()
        reader = BinaryReader(data, offset - 4)
        count = reader.u32()
        if not 1 <= count <= 4096:
            continue
        try:
            key = reader.read_cstringw()
            if not MAPPED_KEY.fullmatch(key):
                continue
            reader.pos = offset - 4
            traces = parse_trace_map(reader)
        except (ValueError, EOFError, struct.error) as exc:
            LOGGER.debug("Section 4 candidate at 0x%X: %s", offset - 4, exc)
            continue
        if len({trace.key for trace in traces}) == count and all(trace.points for trace in traces):
            LOGGER.debug("Section 4 mapped trace table at 0x%X: %d traces", offset - 4, count)
            return traces
    return []


REFLECTION_TERM_NAMES = ("Directivity", "ReflectionTracking", "SourceMatch")
TRANSMISSION_TERM_NAMES = ("CrossTalk", "LoadMatch", "TransmissionTracking")
NAMED_ERROR_KEY_PREFIX = re.compile(
    re.escape(CSTRINGW_MARKER) + rb"[\x01-\xfe](?:"
    + b"|".join(
        re.escape((name + "(").encode("utf-16le"))
        for name in (*REFLECTION_TERM_NAMES, *TRANSMISSION_TERM_NAMES, "Isolation")
    )
    + rb")"
)
ERROR_TERM_PORTS = re.compile(r"[A-Za-z]+\(([1-9]\d*),([1-9]\d*)\)")


def find_and_parse_named_error_terms(data: bytes, start_offset: int) -> List[ComplexTrace]:
    """Read a complete one-port or two-port table of named error terms."""
    for anchor in NAMED_ERROR_KEY_PREFIX.finditer(data, start_offset + 4):
        table_start = anchor.start() - 4
        count, = struct.unpack_from("<I", data, table_start)
        if count not in (3, 12):
            continue

        reader = BinaryReader(data, table_start)
        try:
            traces = parse_trace_map(reader)
        except (ValueError, EOFError, struct.error) as exc:
            LOGGER.debug("Named error term candidate at 0x%X: %s", table_start, exc)
            continue

        names = {trace.key for trace in traces}
        pairs = [ERROR_TERM_PORTS.fullmatch(name) for name in names]
        if len(names) != count or any(pair is None for pair in pairs):
            continue
        ports = {port for pair in pairs for port in pair.groups()}
        expected = {
            f"{name}({port},{port})" for port in ports for name in REFLECTION_TERM_NAMES
        } | {
            f"{name}({receiver},{source})"
            for receiver in ports for source in ports if receiver != source
            for name in TRANSMISSION_TERM_NAMES
        }
        point_count = len(traces[0].points)
        if (
            names == expected
            and point_count
            and all(trace.dim_count == 1 and len(trace.points) == point_count for trace in traces)
        ):
            LOGGER.debug(
                "Named error terms at 0x%X..0x%X: %d ports, %d traces, %d points each",
                table_start, reader.pos, len(ports), count, point_count,
            )
            return traces
    return []


def find_and_parse_diq_error_terms(
    data: bytes, start_offset: int, end_offset: int,
) -> List[ComplexTrace]:
    """Find the count-prefixed table of named error terms before the mapped traces."""
    anchor = encode_cstringw("CrossTalk(1,2)")
    pos = start_offset + 4
    while (offset := data.find(anchor, pos, end_offset)) >= 0:
        pos = offset + len(anchor)
        table_start = offset - 4
        count, = struct.unpack_from("<I", data, table_start)
        if not 1 <= count <= 4096:
            continue

        reader = BinaryReader(data, table_start)
        reader.length = end_offset
        try:
            traces = parse_trace_map(reader)
        except (ValueError, EOFError, struct.error) as exc:
            LOGGER.debug("DIQ error term candidate at 0x%X: %s", table_start, exc)
            continue

        names = {trace.key for trace in traces}
        if (
            len(names) == count
            and any(name.startswith("Directivity(") for name in names)
            and any(name.startswith("TransmissionTracking(") for name in names)
        ):
            LOGGER.debug(
                "DIQ named error terms at 0x%X..0x%X: %d terms",
                table_start, reader.pos, count,
            )
            return traces
    return []


def find_and_parse_diq_mapped_groups(
    data: bytes, start_offset: int, records: List[CalSetHeaderRecord]
) -> List[MappedMeasurementGroup]:
    """Read mapped measurements and named error terms for each DIQ CalSet."""
    trace_anchor = encode_cstringw("SA_0(1,1)")
    stimulus_anchor = encode_cstringw("DIQ Stimulus")
    references = []
    for record in records:
        offset = data.find(uuid.UUID(record.guid).bytes_le, start_offset)
        if offset >= 0:
            references.append((offset, record))
    references.sort(key=lambda reference: reference[0])

    groups: List[MappedMeasurementGroup] = []
    for index, (offset, record) in enumerate(references):
        limit = references[index + 1][0] if index + 1 < len(references) else len(data)
        trace_offset = data.find(trace_anchor, offset + 16, limit)
        if trace_offset < 0:
            continue

        r = BinaryReader(data, trace_offset)
        traces: List[ComplexTrace] = []
        try:
            while r.pos < limit and r.peek_is_cstringw():
                key = r.read_cstringw()
                trace = read_complex_float_trace(r, key)
                if r.pos > limit:
                    raise ValueError("trace crosses the next CalSet reference")
                traces.append(trace)
        except (ValueError, EOFError, struct.error) as exc:
            LOGGER.debug("DIQ mapped stream for %s at 0x%X: %s", record.name, trace_offset, exc)
            continue

        if traces and data.startswith(stimulus_anchor, r.pos + 4, limit):
            error_terms = find_and_parse_diq_error_terms(data, offset + 16, trace_offset)
            groups.append(MappedMeasurementGroup(record, traces, error_terms))
            LOGGER.debug(
                "DIQ CalSet %s: %d mapped traces at 0x%X, %d named error terms",
                record.name, len(traces), trace_offset, len(error_terms),
            )
    return groups


def find_and_parse_section5_cal_settings(data: bytes, start_offset: int) -> Optional[CalSettingsInfo]:
    """Locate Section 5 via 'Cal Settings File' and 'Std CoreStimulus' anchors."""
    marker_pat = encode_cstringw("Cal Settings File")
    idx = data.find(marker_pat, start_offset)
    if idx < 0:
        return None

    LOGGER.debug("Section 5 settings header at 0x%X", idx)
    r = BinaryReader(data, idx)
    _marker = r.read_cstringw()
    _ver = r.u16()
    _empty = r.read_cstringw()
    _unk_451 = r.u32()
    _unk_1 = r.u32()
    _unk_0 = r.u32()
    timestamp_utc = r.read_cstringw()

    port_table_offset = r.pos
    # This layout adds two words before the count and one before each port ID.
    has_port_record_prefix = (
        data[port_table_offset : port_table_offset + 8] == struct.pack("<II", 1, 0)
        and data[port_table_offset + 8 : port_table_offset + 11] != CSTRINGW_MARKER
        and data[port_table_offset + 20 : port_table_offset + 23] == CSTRINGW_MARKER
    )
    if has_port_record_prefix:
        r.pos += 8

    port_kits: List[Dict[str, Any]] = []
    port_count = r.u32()
    if 0 <= port_count <= 64:
        for _ in range(port_count):
            if has_port_record_prefix:
                _record_prefix = r.u32()
            pidx = r.u32()
            conn = r.read_cstringw()
            kit = r.read_cstringw()
            port_kits.append({"port": pidx, "connector": conn, "cal_kit": kit})

    core_text = ""
    core_pat = encode_cstringw("Std CoreStimulus")
    cidx = data.find(core_pat, r.pos)
    if cidx >= 0:
        cr = BinaryReader(data, cidx)
        _cm = cr.read_cstringw()
        _cv = cr.u16()
        if cr.peek_is_cstringw():
            core_text = cr.read_cstringw()

    return CalSettingsInfo(
        timestamp_utc=timestamp_utc,
        port_kits=port_kits,
        core_stimulus_text=core_text,
    )


def parse_section6_metadata_at(data: bytes, offset: int) -> Tuple[Dict[str, Any], int]:
    """Parse COM VARIANT metadata key-value table starting at offset (uint32 count)."""
    r = BinaryReader(data, offset)
    count = r.u32()
    if not (1 <= count <= 256):
        raise ValueError(f"Implausible metadata count {count} at 0x{offset:X}")
    result: Dict[str, Any] = {}
    for _ in range(count):
        key = r.read_cstringw()
        vt1 = r.u16()
        vt2 = r.u16()
        if vt1 != vt2:
            raise ValueError(f"Mismatched VARTYPE ({vt1} vs {vt2}) for key {key!r}")
        if vt1 == 0x0008:  # VT_BSTR (byte length + UTF-16LE)
            byte_len = r.u32()
            raw = r.read_bytes(byte_len)
            val: Any = raw.decode("utf-16-le")
        elif vt1 == 0x0002:  # VT_I2
            val = r.i16()
        elif vt1 == 0x0003:  # VT_I4
            val = r.i32()
        elif vt1 == 0x0004:  # VT_R4
            val = r.f32()
        elif vt1 == 0x0005:  # VT_R8
            val = r.f64()
        elif vt1 == 0x000B:  # VT_BOOL
            val = bool(r.i16())
        else:
            raise ValueError(f"Unsupported VARTYPE 0x{vt1:04X} for key {key!r}")
        result[key] = val
    return result, r.pos


def find_and_parse_section6_metadata(data: bytes, start_offset: int) -> Tuple[Dict[str, Any], Optional[int]]:
    """Locate Section 6 via 'Cal Acquisition Information', 'Created By', or 'Model Number' anchor."""
    for anchor_key in ("Cal Acquisition Information", "Created By", "Model Number"):
        pat = encode_cstringw(anchor_key)
        idx = data.find(pat, start_offset)
        if idx >= 4:
            try:
                meta, end_pos = parse_section6_metadata_at(data, idx - 4)
                return meta, end_pos
            except (ValueError, EOFError) as exc:
                LOGGER.debug("Section 6 candidate at 0x%X: %s", idx - 4, exc)
                continue
    return {}, None


def parse_section7_error_terms_at(data: bytes, offset: int) -> ErrorTermSet:
    """Parse Section 7 ErrorTermSet starting at pad_zero (0) + magic (1006)."""
    r = BinaryReader(data, offset)
    pad_zero = r.u32()
    magic = r.u32()
    if pad_zero != 0 or magic != 1006:
        raise ValueError(f"Invalid Section 7 header ({pad_zero}, {magic}) at 0x{offset:X}")
    ver_a = r.u32()
    ver_b = r.u32()

    num_ports = r.u32()
    if not (1 <= num_ports <= 64):
        raise ValueError(f"Implausible num_ports={num_ports}")
    port_ids = list(r.unpack(f"<{num_ports}I"))

    num_z0 = r.u32()
    if num_z0 != num_ports:
        raise ValueError(f"num_z0 ({num_z0}) != num_ports ({num_ports})")
    z0_offset = r.pos
    candidate_offsets = (z0_offset,)
    # The four-port sample has a word before Z0. A legacy double can
    # also start with these bytes, so check the dimensions before committing.
    if data[z0_offset : z0_offset + 4] == struct.pack("<I", 1):
        candidate_offsets = (z0_offset + 4, z0_offset)

    for candidate_offset in candidate_offsets:
        r.pos = candidate_offset
        try:
            z0_flat = r.unpack(f"<{num_z0 * 2}d")
            num_freqs = r.u32()
            if not (1 <= num_freqs <= 200001):
                raise ValueError(f"Implausible num_freqs={num_freqs}")
            freqs_ghz = list(r.unpack(f"<{num_freqs}d"))
            _unk1, _unk2, _unk3 = r.unpack("<III")
            num_terms, num_pts = r.unpack("<II")
            if num_pts != num_freqs:
                raise ValueError(
                    f"Error term num_points ({num_pts}) != num_freqs ({num_freqs})"
                )
            if num_terms * num_pts * 16 > r.remaining():
                raise EOFError("Truncated error term matrix")
        except (ValueError, EOFError) as exc:
            LOGGER.debug("Section 7 Z0 layout at 0x%X: %s", candidate_offset, exc)
            if candidate_offset == z0_offset:
                raise
        else:
            break

    port_z0 = [(z0_flat[i], z0_flat[i + 1]) for i in range(0, len(z0_flat), 2)]

    matrix_start = r.pos
    error_terms: List[List[Tuple[float, float]]] = []
    for _ in range(num_terms):
        flat = struct.unpack(f"<{num_pts * 2}d", r.read_bytes(num_pts * 16))
        error_terms.append([(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)])

    return ErrorTermSet(
        offset=offset,
        magic=magic,
        ver_a=ver_a,
        ver_b=ver_b,
        port_ids=port_ids,
        port_z0=port_z0,
        freqs_ghz=freqs_ghz,
        error_terms=error_terms,
        data_bounds=(matrix_start, r.pos),
    )


def find_and_parse_section7_error_terms(data: bytes, start_offset: int) -> Optional[ErrorTermSet]:
    """Locate Section 7 via signature 00-00-00-00 + EE-03-00-00 + 01-00-00-00 + 01-00-00-00."""
    sig = struct.pack("<IIII", 0, 1006, 1, 1)
    pos = start_offset
    while True:
        idx = data.find(sig, pos)
        if idx < 0:
            return None
        try:
            return parse_section7_error_terms_at(data, idx)
        except (ValueError, EOFError) as exc:
            LOGGER.debug("Section 7 candidate at 0x%X: %s", idx, exc)
            pos = idx + 4


def reconstruct_grouped_matrix_terms(
    matrix: ErrorTermSet,
) -> Tuple[Dict[str, List[Tuple[complex, float]]], Dict[str, Tuple[int, List[complex]]]]:
    """Return named terms with rounding scales and transmission ratios for verification."""
    ports = matrix.port_ids
    port_count = len(ports)
    if (
        port_count not in (2, 4)
        or (matrix.ver_a, matrix.ver_b) != (1, 1)
        or len(matrix.error_terms) != 6 * port_count
    ):
        return {}, {}

    rows = [[complex(*point) for point in row] for row in matrix.error_terms]
    reconstructed = {}
    reflection = {}
    invalid = complex(math.nan, math.nan)

    for index, port in enumerate(ports):
        a, b, c, d, e, f = rows[index::port_count]
        directivity = [-d_i / a_i if a_i else invalid for a_i, d_i in zip(a, d)]
        source_match = [c_i / a_i if a_i else invalid for a_i, c_i in zip(a, c)]
        load_match = [f_i / e_i if e_i else invalid for e_i, f_i in zip(e, f)]
        # R is the difference between two often nearly equal terms. Use
        # their magnitudes to estimate the effects of float32 rounding.
        b_over_a = [b_i / a_i if a_i else invalid for a_i, b_i in zip(a, b)]
        dm = [d_i * m_i for d_i, m_i in zip(directivity, source_match)]
        reflection[port] = [
            (left + right, abs(left) + abs(right)) for left, right in zip(b_over_a, dm)
        ]

        reconstructed[f"Directivity({port},{port})"] = [(value, abs(value)) for value in directivity]
        reconstructed[f"SourceMatch({port},{port})"] = [(value, abs(value)) for value in source_match]
        reconstructed[f"ReflectionTracking({port},{port})"] = reflection[port]
        for source in ports:
            if source != port:
                reconstructed[f"LoadMatch({port},{source})"] = [(value, abs(value)) for value in load_match]

    transmission_ratios = {}
    for receiver_index, receiver in enumerate(ports):
        e = rows[4 * port_count + receiver_index]
        for source_index, source in enumerate(ports):
            if source == receiver:
                continue
            a = rows[source_index]
            ratio = [a_i / e_i if e_i else invalid for a_i, e_i in zip(a, e)]
            name = f"TransmissionTracking({receiver},{source})"
            reconstructed[name] = [
                (k * value, abs(k) * scale)
                for k, (value, scale) in zip(ratio, reflection[source])
            ]
            transmission_ratios[name] = (source, ratio)

    return reconstructed, transmission_ratios


def parse_cal_file(data: bytes) -> ParsedCalFile:
    r = BinaryReader(data, 0)
    LOGGER.debug("Section 1 FileHeader at 0x%X", r.pos)
    header = parse_section1_header(r)
    LOGGER.debug("Section 2 RawMeasurements at 0x%X", r.pos)
    raw_traces = parse_trace_map(r)

    saved_pos = r.pos
    stim_header: Optional[StimulusHeader] = None
    try:
        LOGGER.debug("Section 3 StimulusHeader at 0x%X", saved_pos)
        stim_header = parse_section3_stimulus_header(r)
    except (ValueError, EOFError) as exc:
        LOGGER.debug("Section 3 skipped at 0x%X: %s", r.pos, exc)
        stim_header = None
        r.pos = saved_pos

    sec3_end_hint = r.pos
    LOGGER.debug("Section 4 MappedMeasurements search from 0x%X", sec3_end_hint)
    mapped_groups = []
    if data.find(encode_cstringw("DIQ Stimulus"), saved_pos) >= 0:
        mapped_groups = find_and_parse_diq_mapped_groups(data, saved_pos, header.calset_records)
    if mapped_groups:
        mapped_traces = [trace for group in mapped_groups for trace in group.traces]
    else:
        mapped_traces = find_and_parse_section4_mapped(data, saved_pos)
    LOGGER.debug("Section 5 CalSettings search from 0x%X", sec3_end_hint)
    cal_settings = find_and_parse_section5_cal_settings(data, sec3_end_hint)
    LOGGER.debug("Section 6 Metadata search from 0x%X", sec3_end_hint)
    metadata, meta_end = find_and_parse_section6_metadata(data, sec3_end_hint)

    sec7_start = meta_end if meta_end is not None else sec3_end_hint
    LOGGER.debug("Section 7 ErrorTermSet search from 0x%X", sec7_start)
    error_term_set = find_and_parse_section7_error_terms(data, sec7_start)
    named_error_terms = []
    reconstructed_error_terms = []
    if not mapped_groups and len(header.calset_records) == 1:
        if error_term_set is None:
            named_error_terms = find_and_parse_named_error_terms(data, saved_pos)
        elif (
            len(error_term_set.port_ids) in (2, 4)
            and len(set(error_term_set.port_ids)) == len(error_term_set.port_ids)
            and len(error_term_set.error_terms) == 6 * len(error_term_set.port_ids)
            and all(point == (1.0, 0.0) for point in error_term_set.error_terms[0])
        ):
            # The verified grouped layout stores A for the first port as unity.
            terms, _ = reconstruct_grouped_matrix_terms(error_term_set)
            frequencies_hz = tuple(f * 1e9 for f in error_term_set.freqs_ghz)
            reconstructed_error_terms = [
                ComplexTrace(
                    key=name, data_type=2, elem_type=4, dim_count=1,
                    points=[(value.real, value.imag) for value, _ in trace],
                    frequencies_hz=frequencies_hz,
                )
                for name, trace in sorted(terms.items())
            ]
            LOGGER.debug("Reconstructed %d matrix error terms", len(reconstructed_error_terms))

    return ParsedCalFile(
        file_size=len(data),
        header=header,
        raw_measurements=raw_traces,
        stimulus_header=stim_header,
        mapped_measurements=mapped_traces,
        cal_settings=cal_settings,
        metadata=metadata,
        error_term_set=error_term_set,
        mapped_measurement_groups=mapped_groups,
        named_error_terms=named_error_terms,
        reconstructed_error_terms=reconstructed_error_terms,
    )


def match_matrix_error_term_names(
    matrix: ErrorTermSet, references: Dict[str, List[Tuple[float, float]]],
) -> Tuple[Dict[int, str], Dict[str, List[int]]]:
    """Assign names only when the full Re/Im trace has a one-to-one match."""
    candidates = {}
    names_by_row: Dict[int, List[str]] = {}
    for name, reference in references.items():
        hits = []
        for index, values in enumerate(matrix.error_terms):
            if reference and len(reference) == len(values) and all(
                math.isclose(actual, expected, rel_tol=5e-7, abs_tol=1e-15)
                for point, ref_point in zip(values, reference)
                for actual, expected in zip(point, ref_point)
            ):
                hits.append(index)
        candidates[name] = hits
        for index in hits:
            names_by_row.setdefault(index, []).append(name)
    labels = {
        index: names[0]
        for index, names in names_by_row.items()
        if len(names) == 1 and len(candidates[names[0]]) == 1
    }
    return labels, candidates


def matrix_labels_path(cal_path: Path) -> Path:
    return cal_path.with_name(cal_path.name + ".terms.json")


def save_matrix_error_term_names(cal_path: Path, data: bytes, names: Dict[int, str]) -> Path:
    path = matrix_labels_path(cal_path)
    payload = {"sha256": hashlib.sha256(data).hexdigest(), "names": names}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_matrix_error_term_names(parsed: ParsedCalFile, cal_path: Path, data: bytes) -> None:
    matrix = parsed.error_term_set
    path = matrix_labels_path(cal_path)
    if matrix is None or not path.is_file():
        return

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("sha256") != hashlib.sha256(data).hexdigest():
            raise ValueError("SHA-256 does not match the selected .cal file")
        raw_names = payload.get("names")
        if not isinstance(raw_names, dict):
            raise ValueError("Missing error term name mapping")
        labels = {}
        for raw_index, name in raw_names.items():
            index = int(raw_index)
            if str(index) != raw_index or not 0 <= index < len(matrix.error_terms):
                raise ValueError(f"Invalid error term index {raw_index!r}")
            if not isinstance(name, str) or not name.strip() or not name.isprintable():
                raise ValueError(f"Invalid name for error term {index}")
            labels[index] = name
        if len(set(labels.values())) != len(labels):
            raise ValueError("Duplicate error term names")
    except (OSError, ValueError) as exc:
        LOGGER.warning("Ignored error term names in %s: %s", path.name, exc)
        return

    matrix.term_names = labels
    LOGGER.info("Loaded %d matrix error term names from %s", len(labels), path.name)


def export_results(parsed: ParsedCalFile, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    named_error_term_count = len(parsed.named_error_terms) + sum(
        len(group.error_terms) for group in parsed.mapped_measurement_groups
    )
    matrix_term_count = len(parsed.error_term_set.error_terms) if parsed.error_term_set else 0
    summary = {
        "file_size": parsed.file_size,
        "header": {
            "magic": parsed.header.magic,
            "file_version": parsed.header.file_version,
            "sub_ver_a": parsed.header.sub_ver_a,
            "sub_ver_b": parsed.header.sub_ver_b,
            "calset_name": parsed.header.calset_name,
            "calset_guid": parsed.header.calset_guid,
            "timestamp_utc": parsed.header.timestamp_utc,
            "calset_records": [asdict(record) for record in parsed.header.calset_records],
            "schema_count": len(parsed.header.schemas),
            "schemas": parsed.header.schemas,
        },
        "stimulus_header": (
            {
                "params": parsed.stimulus_header.params,
                "port_config": parsed.stimulus_header.port_config,
                "created_by": parsed.stimulus_header.created_by,
            }
            if parsed.stimulus_header
            else None
        ),
        "cal_settings": (
            {
                "timestamp_utc": parsed.cal_settings.timestamp_utc,
                "port_kits": parsed.cal_settings.port_kits,
                "core_stimulus_text": parsed.cal_settings.core_stimulus_text,
            }
            if parsed.cal_settings
            else None
        ),
        "metadata": parsed.metadata,
        "counts": {
            "raw_measurements": len(parsed.raw_measurements),
            "mapped_measurements": len(parsed.mapped_measurements),
            "error_terms": matrix_term_count + named_error_term_count,
            "freq_points": len(parsed.error_term_set.freqs_ghz) if parsed.error_term_set else 0,
        },
    }
    if parsed.mapped_measurement_groups:
        summary["mapped_measurement_groups"] = [
            {
                "calset_name": group.calset.name,
                "calset_guid": group.calset.guid,
                "trace_count": len(group.traces),
                "error_term_count": len(group.error_terms),
            }
            for group in parsed.mapped_measurement_groups
        ]
    if parsed.error_term_set and parsed.error_term_set.term_names:
        summary["matrix_error_term_names"] = parsed.error_term_set.term_names
    if parsed.reconstructed_error_terms:
        summary["counts"]["reconstructed_error_terms"] = len(parsed.reconstructed_error_terms)
        summary["reconstructed_error_term_names"] = [
            trace.key for trace in parsed.reconstructed_error_terms
        ]
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    trace_files = [("raw_measurements.csv", parsed.raw_measurements)]
    if not parsed.mapped_measurement_groups:
        trace_files.append(("mapped_measurements.csv", parsed.mapped_measurements))
    for filename, traces in trace_files:
        if not traces:
            continue
        with (out_dir / filename).open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["trace_key", "point_index", "real", "imag"])
            for tr in traces:
                for i, (re_v, im_v) in enumerate(tr.points):
                    writer.writerow([tr.key, i, f"{re_v:.9g}", f"{im_v:.9g}"])

    groups = parsed.mapped_measurement_groups
    for filename, key_header, traces_by_calset in (
        ("mapped_measurements.csv", "trace_key", [(g.calset.name, g.traces) for g in groups]),
        ("diq_error_terms.csv", "error_term", [(g.calset.name, g.error_terms) for g in groups]),
        ("named_error_terms.csv", "error_term", [(parsed.header.calset_name, parsed.named_error_terms)]),
        (
            "reconstructed_error_terms.csv",
            "error_term",
            [(parsed.header.calset_name, parsed.reconstructed_error_terms)],
        ),
    ):
        if not any(traces for _, traces in traces_by_calset):
            continue
        with (out_dir / filename).open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["calset_name", key_header, "point_index", "freq_hz", "real", "imag"])
            for calset_name, traces in traces_by_calset:
                for trace in traces:
                    for i, (re_v, im_v) in enumerate(trace.points):
                        frequency = "" if trace.frequencies_hz is None else trace.frequencies_hz[i]
                        writer.writerow([
                            calset_name, trace.key, i, frequency, f"{re_v:.9g}", f"{im_v:.9g}",
                        ])

    if parsed.error_term_set:
        ets = parsed.error_term_set
        with (out_dir / "error_terms.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            header_row = ["point_index", "freq_ghz"]
            for t_idx in range(len(ets.error_terms)):
                header_row.extend([f"term{t_idx}_re", f"term{t_idx}_im"])
            writer.writerow(header_row)
            for p_idx, freq in enumerate(ets.freqs_ghz):
                row: List[Any] = [p_idx, f"{freq:.9g}"]
                for term in ets.error_terms:
                    re_v, im_v = term[p_idx]
                    row.extend([f"{re_v:.12g}", f"{im_v:.12g}"])
                writer.writerow(row)


def print_summary(parsed: ParsedCalFile) -> None:
    h = parsed.header
    print(f"=== Keysight PNA Cal File ({parsed.file_size:,} bytes) ===")
    print(f"CalSet Name : {h.calset_name}")
    print(f"GUID        : {h.calset_guid}")
    print(f"Timestamp   : {h.timestamp_utc}")
    if len(h.calset_records) > 1:
        print(f"CalSet Records: {len(h.calset_records)}")
        for record in h.calset_records:
            print(f"  {record.name} ({record.guid})")
    print(f"Schemas     : {len(h.schemas)} modules")
    print(f"Raw Traces  : {len(parsed.raw_measurements)}")
    if parsed.stimulus_header:
        print(f"Port Config : {parsed.stimulus_header.port_config}")
        print(f"Stim Params : {parsed.stimulus_header.params}")
    print(f"Mapped Tr.  : {len(parsed.mapped_measurements)}")
    for group in parsed.mapped_measurement_groups:
        print(
            f"  {group.calset.name}: {len(group.traces)} mapped traces, "
            f"{len(group.error_terms)} named error terms"
        )
    if parsed.named_error_terms:
        print("\n--- Named Error Terms ---")
        for trace in parsed.named_error_terms:
            print(f"  {trace.key}: {len(trace.points)} pts")
    if parsed.metadata:
        print("\n--- Section 6: Metadata ---")
        for k, v in parsed.metadata.items():
            val_preview = str(v).replace("\n", " | ")
            if len(val_preview) > 90:
                val_preview = val_preview[:87] + "..."
            print(f"  {k}: {val_preview}")
    if parsed.error_term_set:
        ets = parsed.error_term_set
        print("\n--- Section 7: ErrorTermSet ---")
        print(f"  Offset      : 0x{ets.offset:X}")
        print(f"  Ports       : {ets.port_ids} (Z0={ets.port_z0})")
        print(
            f"  Frequencies : {len(ets.freqs_ghz)} pts "
            f"({ets.freqs_ghz[0]:.6g} .. {ets.freqs_ghz[-1]:.6g} GHz)"
        )
        print(f"  Error Terms : {len(ets.error_terms)} terms x {len(ets.freqs_ghz)} pts")
        for index, name in sorted(ets.term_names.items()):
            print(f"  Term {index}: {name}")
    if parsed.reconstructed_error_terms:
        print("\n--- Reconstructed Error Terms ---")
        for trace in parsed.reconstructed_error_terms:
            print(f"  {trace.key}: {len(trace.points)} pts")


def print_hex_range(data: bytes, start: int, end: int) -> None:
    for offset in range(start, end, 16):
        print(f"  {offset:08X}  {data[offset:min(offset + 16, end)].hex(' ').upper()}")


def inspect_matrix_metadata(data: bytes, matrix: ErrorTermSet) -> None:
    if matrix.data_bounds is None:
        return
    start, end = matrix.data_bounds
    print("\n[Matrix boundaries and trailer]")
    print(
        f"  Complex data: 0x{start:X}..0x{end:X} (end exclusive) "
        f"| {len(matrix.error_terms)} rows x {len(matrix.freqs_ghz)} points"
    )
    print("  Header through port information, up to 112 bytes:")
    head_end = min(start - 20, matrix.offset + 112)
    print_hex_range(data, matrix.offset, head_end)
    print("  Words between frequency axis and matrix:")
    print_hex_range(data, start - 20, start)
    print(f"  Trailer: {len(data) - end} bytes")
    trailer_limit = 4096
    if len(data) - end <= trailer_limit:
        print_hex_range(data, end, len(data))
    else:
        half_limit = trailer_limit // 2
        print_hex_range(data, end, end + half_limit)
        print(f"  ... {len(data) - end - trailer_limit} bytes omitted ...")
        print_hex_range(data, len(data) - half_limit, len(data))

    print("\n[Embedded term text candidates]")
    names = (
        "Directivity", "Source Match", "Reflection Tracking", "Load Match",
        "Transmission Tracking", "Cross Talk", "Isolation", "Receiver Match",
        "Response Tracking", "Switch Term",
    )
    for encoding in ("ascii", "utf-16le"):
        spellings = {
            name.replace(" ", separator)
            for name in names
            for separator in ("", " ", "_", "-")
        }
        pattern = re.compile(
            b"|".join(re.escape(name.encode(encoding)) for name in sorted(spellings)),
            re.IGNORECASE,
        )
        count = 0
        for region_start, region_end in ((0, start), (end, len(data))):
            for match in pattern.finditer(data, region_start, region_end):
                count += 1
                if count <= 32:
                    print(f"  0x{match.start():X}: {match.group().decode(encoding)} ({encoding})")
        print(f"  {encoding}: {count} matches")
        if count > 32:
            print(f"  Additional matches omitted: {count - 32}")


def inspect_error_terms(data: bytes) -> None:
    """Report possible error-term layouts, without exporting trace values."""
    print(f"File size: {len(data)} bytes")
    matrix = None
    try:
        parsed = parse_cal_file(data)
    except (ValueError, EOFError, struct.error) as exc:
        print(f"Normal import: {type(exc).__name__}: {exc}")
    else:
        matrix = parsed.error_term_set
        matrix_count = len(matrix.error_terms) if matrix else 0
        named_count = len(parsed.named_error_terms) + sum(
            len(group.error_terms) for group in parsed.mapped_measurement_groups
        )
        print(f"Normal import: {matrix_count} matrix terms, {named_count} named terms")

    print("\n[Error term matrix candidates]")
    marker = struct.pack("<II", 0, 1006)
    offsets = []
    offset = data.find(marker)
    while offset >= 0:
        offsets.append(offset)
        offset = data.find(marker, offset + len(marker))
    print(f"Found: {len(offsets)}")
    for offset in offsets[:8]:
        print(f"  0x{offset:X}: {data[offset:offset + 40].hex(' ').upper()}")
        if offset + 16 <= len(data):
            ver_a, ver_b = struct.unpack_from("<II", data, offset + 8)
            print(f"    Version words: {ver_a}, {ver_b}")
        try:
            candidate = parse_section7_error_terms_at(data, offset)
        except (ValueError, EOFError, struct.error) as exc:
            print(f"    Layout check: {type(exc).__name__}: {exc}")
        else:
            print(
                f"    Layout check: ports={candidate.port_ids}, "
                f"terms={len(candidate.error_terms)}, points={len(candidate.freqs_ghz)}"
            )
    if len(offsets) > 8:
        print(f"  Additional candidates omitted: {len(offsets) - 8}")
    if not offsets:
        magic = struct.pack("<I", 1006)
        print("  Unvalidated magic-only matches:")
        offset = data.find(magic)
        for _ in range(4):
            if offset < 0:
                break
            nearby = data[max(0, offset - 8):offset + 32].hex(" ").upper()
            print(f"    magic at 0x{offset:X}: {nearby}")
            offset = data.find(magic, offset + len(magic))

    print("\n[Named error term keys]")
    count = 0
    for match in NAMED_ERROR_KEY_PREFIX.finditer(data):
        offset = match.start()
        reader = BinaryReader(data, offset)
        try:
            key = reader.read_cstringw()
        except (ValueError, EOFError):
            continue
        if re.fullmatch(r"[A-Za-z]+\(\d+(?:,\s*\d+)?\)", key) is None:
            continue
        count += 1
        if count <= 8:
            prefix = data[max(0, offset - 4):offset].hex(" ").upper()
            record_header = data[reader.pos:reader.pos + 24].hex(" ").upper()
            print(f"  0x{offset:X}: {key} | before: {prefix} | header: {record_header}")
    print(f"Found: {count}")
    if count > 8:
        print(f"  Additional keys omitted: {count - 8}")
    if matrix is not None:
        inspect_matrix_metadata(data, matrix)


def inspect_mapped_measurements(data: bytes) -> None:
    """Report trace names and record boundaries for an unrecognized mapped table."""
    reader = BinaryReader(data)
    header = parse_section1_header(reader)
    raw_traces = parse_trace_map(reader)
    start_offset = reader.pos
    mapped = find_and_parse_section4_mapped(data, start_offset)
    print(f"{header.calset_name} | {len(data)} bytes | raw={len(raw_traces)} | mapped={len(mapped)}")
    print(f"End of raw measurements: 0x{start_offset:X}")
    print("Raw keys:", [trace.key for trace in raw_traces[:12]])
    print("\n[Complex trace records after raw measurements]")
    offset = data.find(CSTRINGW_MARKER, start_offset)
    count = 0
    while offset >= 0 and count < 24:
        reader.pos = offset
        try:
            key = reader.read_cstringw()
            flag, size = reader.unpack("<HI")
            if key and key.isprintable() and flag == 1 and 40 <= size <= reader.remaining():
                fields = data[reader.pos:reader.pos + 14]
                if fields[:6] == struct.pack("<HI", 1, 1):
                    count += 1
                    count_bytes = data[max(0, offset - 4):offset].hex(" ").upper()
                    print(f"  0x{offset:X}: {key} | before: {count_bytes} | size: 0x{size:X}")
        except (ValueError, EOFError):
            pass
        offset = data.find(CSTRINGW_MARKER, offset + len(CSTRINGW_MARKER))
    if count == 0:
        print("  No recognizable complex trace records")
    elif count == 24:
        print("  Output limited to the first 24 candidate records")


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse Keysight PNA/PNA-X *.cal files")
    ap.add_argument("cal_file", type=Path, help="Path to the .cal file")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--inspect-mapped-measurements",
        action="store_true",
        help="Report candidate mapped measurement keys and record headers",
    )
    mode.add_argument(
        "--inspect-error-terms",
        action="store_true",
        help="Report candidate error-term layouts for a .cal file",
    )
    mode.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="Optional directory to export summary.json, traces CSV, and error_terms.csv",
    )
    args = ap.parse_args()

    data = args.cal_file.read_bytes()
    if args.inspect_error_terms:
        inspect_error_terms(data)
        return
    if args.inspect_mapped_measurements:
        inspect_mapped_measurements(data)
        return

    parsed = parse_cal_file(data)
    load_matrix_error_term_names(parsed, args.cal_file, data)
    print_summary(parsed)
    if args.export_dir:
        export_results(parsed, args.export_dir)
        print(f"\nExported JSON & CSV files to: {args.export_dir.resolve()}")


if __name__ == "__main__":
    main()
