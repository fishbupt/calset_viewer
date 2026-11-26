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
import json
import logging
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


def find_and_parse_section4_mapped(data: bytes, start_offset: int) -> List[ComplexTrace]:
    """Locate Section 4 MappedMeasurements by finding the SA_0(1,1) or Thru_ anchor."""
    for anchor_str in ("SA_0(1,1)", "SA_0(2,2)", "Thru_12(1,1)"):
        pat = encode_cstringw(anchor_str)
        idx = data.find(pat, start_offset)
        if idx >= 4:
            r = BinaryReader(data, idx - 4)
            count = struct.unpack_from("<I", data, idx - 4)[0]
            if 1 <= count <= 4096:
                try:
                    return parse_trace_map(r)
                except (ValueError, EOFError) as exc:
                    LOGGER.debug("Section 4 candidate at 0x%X: %s", idx - 4, exc)
                    continue
    return []


def find_and_parse_diq_mapped_groups(
    data: bytes, start_offset: int, records: List[CalSetHeaderRecord]
) -> List[MappedMeasurementGroup]:
    """Read DIQ trace streams between their stimulus GUID and DIQ Stimulus footer."""
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
            groups.append(MappedMeasurementGroup(record, traces))
            LOGGER.debug(
                "DIQ mapped stream for %s: %d traces at 0x%X",
                record.name, len(traces), trace_offset,
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
        mapped_traces = find_and_parse_section4_mapped(data, sec3_end_hint)
    LOGGER.debug("Section 5 CalSettings search from 0x%X", sec3_end_hint)
    cal_settings = find_and_parse_section5_cal_settings(data, sec3_end_hint)
    LOGGER.debug("Section 6 Metadata search from 0x%X", sec3_end_hint)
    metadata, meta_end = find_and_parse_section6_metadata(data, sec3_end_hint)

    sec7_start = meta_end if meta_end is not None else sec3_end_hint
    LOGGER.debug("Section 7 ErrorTermSet search from 0x%X", sec7_start)
    error_term_set = find_and_parse_section7_error_terms(data, sec7_start)

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
    )


def export_results(parsed: ParsedCalFile, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

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
            "error_terms": len(parsed.error_term_set.error_terms) if parsed.error_term_set else 0,
            "freq_points": len(parsed.error_term_set.freqs_ghz) if parsed.error_term_set else 0,
        },
    }
    if parsed.mapped_measurement_groups:
        summary["mapped_measurement_groups"] = [
            {
                "calset_name": group.calset.name,
                "calset_guid": group.calset.guid,
                "trace_count": len(group.traces),
            }
            for group in parsed.mapped_measurement_groups
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

    if parsed.mapped_measurement_groups:
        with (out_dir / "mapped_measurements.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["calset_name", "trace_key", "point_index", "freq_hz", "real", "imag"])
            for group in parsed.mapped_measurement_groups:
                for tr in group.traces:
                    for i, (re_v, im_v) in enumerate(tr.points):
                        frequency = tr.frequencies_hz[i] if tr.frequencies_hz is not None else ""
                        writer.writerow([
                            group.calset.name, tr.key, i, frequency, f"{re_v:.9g}", f"{im_v:.9g}",
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
        print(f"  {group.calset.name}: {len(group.traces)} mapped traces")
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse Keysight PNA/PNA-X *.cal files")
    ap.add_argument("cal_file", type=Path, help="Path to the .cal file")
    ap.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="Optional directory to export summary.json, traces CSV, and error_terms.csv",
    )
    args = ap.parse_args()

    data = args.cal_file.read_bytes()
    parsed = parse_cal_file(data)
    print_summary(parsed)
    if args.export_dir:
        export_results(parsed, args.export_dir)
        print(f"\nExported JSON & CSV files to: {args.export_dir.resolve()}")


if __name__ == "__main__":
    main()
