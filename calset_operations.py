"""Fixture reference-plane transforms and named CalSet comparisons."""

from __future__ import annotations

import cmath
import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from parse_pna_cal import (
    ERROR_TERM_PORTS,
    REFLECTION_TERM_NAMES,
    CalSetHeader,
    CalSetHeaderRecord,
    ComplexTrace,
    ParsedCalFile,
    load_matrix_error_term_names,
    parse_cal_file,
)


DERIVED_FORMAT = "calset-viewer-fixture-deembed"
FREQUENCY_UNITS = {"hz": 1, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}
THRU = (0j, 1 + 0j, 1 + 0j, 0j)


@dataclass(frozen=True)
class Fixture:
    path: Path
    frequencies_hz: tuple[float, ...]
    z0: float
    # Touchstone two-port order: S11, S21, S12, S22.
    samples: tuple[tuple[complex, complex, complex, complex], ...]
    reversed_ports: bool


@dataclass(frozen=True)
class TermComparison:
    name: str
    status: str
    residuals: list[tuple[float, float]]
    frequencies_hz: tuple[float, ...] | None = None
    max_error: float | None = None
    rms_error: float | None = None
    peak_index: int | None = None

    @property
    def peak_frequency_hz(self) -> float | None:
        if self.frequencies_hz is None or self.peak_index is None:
            return None
        return self.frequencies_hz[self.peak_index]


def read_fixture(path: Path, reversed_ports: bool = False) -> Fixture:
    fields: list[str] = []
    options = ["ghz", "s", "ma", "r", "50"]
    has_options = False
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.partition("!")[0].strip()
        if not line:
            continue
        if line.startswith("["):
            raise ValueError(f"{path.name}：当前支持 Touchstone 1.x 的 .s2p 文件。")
        if line.startswith("#"):
            if fields or has_options:
                raise ValueError(f"{path.name}：选项行必须位于数据之前。")
            supplied = line[1:].lower().split()
            if len(supplied) > len(options):
                raise ValueError(f"{path.name}：无法识别 Touchstone 选项行。")
            options[:len(supplied)] = supplied
            has_options = True
            continue
        fields.extend(line.split())

    unit, parameter, data_format, ref_token, ref_value = options
    if unit not in FREQUENCY_UNITS or parameter != "s" or ref_token != "r":
        raise ValueError(f"{path.name}：需要 S 参数、有效的频率单位和单一参考阻抗。")
    if data_format not in ("ri", "ma", "db"):
        raise ValueError(f"{path.name}：支持 RI、MA、DB 三种数据格式。")
    z0 = float(ref_value)
    if z0 <= 0 or not math.isfinite(z0):
        raise ValueError(f"{path.name}：参考阻抗必须是有限的正实数。")
    if not fields or len(fields) % 9:
        raise ValueError(f"{path.name}：每个频点需要 9 个数值。请导出仅含 S 参数的 .s2p。")

    values = [float(field) for field in fields]
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{path.name}：S 参数含非有限值。")
    frequencies: list[float] = []
    samples = []
    for index in range(0, len(values), 9):
        frequency = values[index] * FREQUENCY_UNITS[unit]
        if not math.isfinite(frequency) or frequency < 0 or (
            frequencies and frequency <= frequencies[-1]
        ):
            raise ValueError(f"{path.name}：频率必须非负并严格递增。")
        frequencies.append(frequency)
        pairs = zip(values[index + 1:index + 9:2], values[index + 2:index + 9:2])
        if data_format == "ri":
            sample = tuple(complex(a, b) for a, b in pairs)
        else:
            sample = tuple(
                cmath.rect(10 ** (a / 20) if data_format == "db" else a, math.radians(b))
                for a, b in pairs
            )
        samples.append(sample[::-1] if reversed_ports else sample)
    return Fixture(path, tuple(frequencies), z0, tuple(samples), reversed_ports)


def fixture_samples(
    fixture: Fixture | None, frequencies_hz: tuple[float, ...], z0: complex,
) -> list[tuple[complex, complex, complex, complex]]:
    if fixture is None:
        return [THRU] * len(frequencies_hz)
    if not cmath.isclose(z0, fixture.z0, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(f"{fixture.path.name}：夹具参考阻抗 {fixture.z0:g} Ω 与 CalSet 的 {z0} Ω 不同。")
    axis = fixture.frequencies_hz
    samples = []
    for hz in frequencies_hz:
        index = bisect_left(axis, hz)
        if index < len(axis) and math.isclose(axis[index], hz, rel_tol=1e-12, abs_tol=1e-6):
            samples.append(fixture.samples[index])
        elif index and math.isclose(axis[index - 1], hz, rel_tol=1e-12, abs_tol=1e-6):
            samples.append(fixture.samples[index - 1])
        elif index == 0 or index == len(axis):
            raise ValueError(f"{fixture.path.name}：未覆盖 CalSet 频点 {hz:.12g} Hz。")
        else:
            fraction = (hz - axis[index - 1]) / (axis[index] - axis[index - 1])
            samples.append(tuple(
                a + fraction * (b - a)
                for a, b in zip(fixture.samples[index - 1], fixture.samples[index])
            ))
    return samples


def write_calset_document(path: Path, document: dict) -> None:
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
        ) as output:
            temporary_path = Path(output.name)
            json.dump(document, output, ensure_ascii=False, allow_nan=False)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_calset_document(path: Path, raw: bytes | None = None) -> ParsedCalFile:
    if raw is None:
        raw = path.read_bytes()
    if path.suffix.lower() != ".json":
        parsed = parse_cal_file(raw)
        load_matrix_error_term_names(parsed, path, raw)
        return parsed

    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("format") != DERIVED_FORMAT or document.get("version") != 1:
        raise ValueError("无法识别 CalSet Viewer 去嵌结果文件。")
    try:
        frequencies = tuple(float(hz) for hz in document["frequencies_hz"])
        if not frequencies or any(not math.isfinite(hz) or hz < 0 for hz in frequencies):
            raise ValueError("结果文件含无效频率。")
        ports = tuple(item["id"] for item in document["ports"])
        impedances = tuple(complex(*item["z0_ohm"]) for item in document["ports"])
        if (
            len(ports) not in (1, 2, 4) or len(set(ports)) != len(ports)
            or any(type(port) is not int or port < 1 for port in ports)
            or not all(cmath.isfinite(z0) for z0 in impedances)
        ):
            raise ValueError("结果文件含无效的校准端口或参考阻抗。")
        traces = []
        for name, samples in document["terms"].items():
            points = [(float(real), float(imag)) for real, imag in samples]
            if len(points) != len(frequencies) or any(not cmath.isfinite(complex(*point)) for point in points):
                raise ValueError(f"{name} 含无效的复数迹线。")
            traces.append(ComplexTrace(name, 2, 4, 1, points, frequencies))
        if not traces:
            raise ValueError("结果文件缺少误差项。")
        name, guid, timestamp = (document[key] for key in ("name", "guid", "timestamp_utc"))
        if not all(isinstance(value, str) and value for value in (name, guid, timestamp)):
            raise ValueError("结果文件含无效的 CalSet 标识。")
        return ParsedCalFile(
            file_size=len(raw),
            header=CalSetHeader(
                magic=DERIVED_FORMAT, file_version=1, sub_ver_a=0, sub_ver_b=0,
                calset_name=name, calset_guid=guid, timestamp_utc=timestamp,
                header_flags=0, unk_2=0, unk_3=0, unk_4=0,
                calset_records=[CalSetHeaderRecord(name, guid, timestamp, 0)],
            ),
            raw_measurements=[],
            metadata={
                "校准端口": list(ports),
                "参考阻抗 (Ω)": [(z0.real, z0.imag) for z0 in impedances],
                "去嵌来源": document["provenance"],
            },
            reconstructed_error_terms=traces,
        )
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("CalSet Viewer 结果文件缺少所需数据。") from exc


def named_calset_groups(parsed: ParsedCalFile) -> list[tuple[str, list[ComplexTrace]]]:
    if parsed.mapped_measurement_groups:
        return [(g.calset.name, g.error_terms) for g in parsed.mapped_measurement_groups if g.error_terms]
    traces = parsed.reconstructed_error_terms or parsed.named_error_terms
    return [(parsed.header.calset_name, traces)] if traces else []


def fixture_calibration_ports(parsed: ParsedCalFile) -> list[tuple[int, complex | None]]:
    if parsed.mapped_measurement_groups:
        raise ValueError("夹具去嵌需要常规 S 参数校准。")

    if parsed.reconstructed_error_terms:
        if parsed.error_term_set:
            ports = parsed.error_term_set.port_ids
            impedances = parsed.error_term_set.port_z0
        elif parsed.header.magic == DERIVED_FORMAT:
            ports = parsed.metadata["校准端口"]
            impedances = parsed.metadata["参考阻抗 (Ω)"]
        else:
            raise ValueError("CalSet 缺少校准端口和参考阻抗。")
        if (
            len(ports) not in (1, 2, 4)
            or len(set(ports)) != len(ports)
            or len(ports) != len(impedances)
        ):
            raise ValueError("夹具去嵌支持 1、2 或 4 个校准端口。")
        return [(port, complex(*z0)) for port, z0 in zip(ports, impedances)]

    if len(parsed.named_error_terms) == len(REFLECTION_TERM_NAMES):
        names = {trace.key for trace in parsed.named_error_terms}
        match = ERROR_TERM_PORTS.fullmatch(parsed.named_error_terms[0].key)
        if match is not None:
            port = int(match[1])
            if names == {f"{name}({port},{port})" for name in REFLECTION_TERM_NAMES}:
                return [(port, None)]
    raise ValueError("需要具名单端口校准，或带端口及参考阻抗的两端口或四端口 CalSet。")


def terminated_fixture(
    sample: tuple[complex, complex, complex, complex],
    match: complex,
    port: int,
    frequency_hz: float,
) -> tuple[complex, complex]:
    s11, s21, s12, s22 = sample
    if not s21 or not s12:
        raise ValueError(f"端口 {port} 在 {frequency_hz:.12g} Hz 的夹具传输为零。")
    denominator = 1 - match * s11
    if abs(denominator) <= 1e-12:
        raise ValueError(f"端口 {port} 在 {frequency_hz:.12g} Hz 的夹具失配分母接近零。")
    return denominator, s22 + s21 * match * s12 / denominator


def deembed_calset(
    parsed: ParsedCalFile,
    source_file: Path,
    fixtures: dict[int, Fixture],
    *,
    reference_impedance: float | None = None,
    use_fixture_frequency_axis: bool = False,
) -> dict:
    calibration_ports = fixture_calibration_ports(parsed)
    if not fixtures or not fixtures.keys() <= {port for port, _ in calibration_ports}:
        raise ValueError("至少为一个已校准端口选择夹具文件。")
    ports: dict[int, complex] = {}
    for port, z0 in calibration_ports:
        if z0 is None:
            if (
                reference_impedance is None or not math.isfinite(reference_impedance)
                or reference_impedance <= 0
            ):
                raise ValueError("请输入单端口 CalSet 的参考阻抗。")
            z0 = complex(reference_impedance)
        ports[port] = z0

    original = parsed.reconstructed_error_terms or parsed.named_error_terms
    traces = {trace.key.casefold(): trace for trace in original}
    frequencies = next(
        (trace.frequencies_hz for trace in original if trace.frequencies_hz is not None), None,
    )
    frequency_source = "calset"
    if frequencies is None and use_fixture_frequency_axis and len(ports) == 1:
        frequencies = fixtures[next(iter(ports))].frequencies_hz
        frequency_source = "fixture"
    if not frequencies or any(not math.isfinite(hz) or hz < 0 for hz in frequencies):
        raise ValueError("CalSet 缺少有效频率轴。若为单端口，请先核对 CalSet 与夹具的频点及顺序。")

    for trace in original:
        if len(trace.points) != len(frequencies) or trace.frequencies_hz not in (None, frequencies):
            raise ValueError(f"{trace.key} 的点数或频率轴与去嵌频率轴不一致。")

    def input_points(key: str) -> list[tuple[float, float]]:
        trace = traces.get(key.casefold())
        if trace is None:
            raise ValueError(f"CalSet 缺少误差项 {key}。")
        return trace.points

    terms: dict[str, list[tuple[float, float]]] = {}

    def store_series(keys: tuple[str, ...], series: list[list[complex]]) -> None:
        for key, values in zip(keys, series):
            if not all(cmath.isfinite(value) for value in values):
                raise ValueError(f"{key} 去嵌后含非有限值。")
            terms[key] = [(value.real, value.imag) for value in values]

    fixture_rows = {
        port: fixture_samples(fixtures.get(port), frequencies, z0) for port, z0 in ports.items()
    }
    for source, source_rows in fixture_rows.items():
        reflection_keys = tuple(f"{name}({source},{source})" for name in REFLECTION_TERM_NAMES)
        directivity, tracking, source_match = (input_points(key) for key in reflection_keys)
        reflection = [[], [], []]
        outgoing_gains = []
        for hz, sample, d, r, m in zip(frequencies, source_rows, directivity, tracking, source_match):
            s11, s21, s12, _ = sample
            denominator, new_match = terminated_fixture(sample, complex(*m), source, hz)
            outgoing = s21 / denominator
            reflection[0].append(complex(*d) + complex(*r) * s11 / denominator)
            reflection[1].append(complex(*r) * (s12 / denominator) * outgoing)
            reflection[2].append(new_match)
            outgoing_gains.append(outgoing)
        store_series(reflection_keys, reflection)

        for receiver, receiver_rows in fixture_rows.items():
            if receiver == source:
                continue
            path_keys = (f"LoadMatch({receiver},{source})", f"TransmissionTracking({receiver},{source})")
            load_match, transmission = (input_points(key) for key in path_keys)
            path_terms = [[], []]
            for hz, sample, l, t, outgoing in zip(
                frequencies, receiver_rows, load_match, transmission, outgoing_gains
            ):
                denominator, new_match = terminated_fixture(sample, complex(*l), receiver, hz)
                path_terms[0].append(new_match)
                path_terms[1].append(complex(*t) * outgoing * (sample[2] / denominator))
            store_series(path_keys, path_terms)

            # Isolation is additive at the raw receiver in this model.
            isolation = traces.get(f"crosstalk({receiver},{source})")
            if isolation is not None:
                terms[isolation.key] = isolation.points

    return {
        "format": DERIVED_FORMAT,
        "version": 1,
        "name": f"{parsed.header.calset_name}_deembed",
        "guid": str(uuid4()),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ports": [{"id": port, "z0_ohm": [z0.real, z0.imag]} for port, z0 in ports.items()],
        "frequencies_hz": frequencies,
        "terms": dict(sorted(terms.items())),
        "provenance": {
            "source_file": source_file.name,
            "source_calset": parsed.header.calset_name,
            "source_guid": parsed.header.calset_guid,
            "frequency_axis": frequency_source,
            "fixtures": [
                {"port": port, "file": fixture.path.name, "reversed_ports": fixture.reversed_ports}
                for port, fixture in fixtures.items()
            ],
        },
    }


def compare_terms(
    left: list[ComplexTrace], right: list[ComplexTrace], *, allow_point_order: bool = False,
) -> list[TermComparison]:
    left_terms = {trace.key.casefold(): trace for trace in left}
    right_terms = {trace.key.casefold(): trace for trace in right}
    rows = []
    for key in sorted(left_terms.keys() | right_terms.keys()):
        a, b = left_terms.get(key), right_terms.get(key)
        if a is None:
            rows.append(TermComparison(b.key, "仅参考 CalSet 有", []))
        elif b is None:
            rows.append(TermComparison(a.key, "仅当前 CalSet 有", []))
        elif not a.points or len(a.points) != len(b.points):
            rows.append(TermComparison(a.key, "点数不同或无数据", []))
        elif not allow_point_order and (a.frequencies_hz is None) != (b.frequencies_hz is None):
            rows.append(TermComparison(a.key, "一侧缺少频率轴", []))
        elif a.frequencies_hz is not None and b.frequencies_hz is not None and (
            len(a.frequencies_hz) != len(a.points) or len(b.frequencies_hz) != len(b.points)
            or not all(math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-6) for x, y in zip(
                a.frequencies_hz, b.frequencies_hz
            ))
        ):
            rows.append(TermComparison(a.key, "频率轴不一致", []))
        else:
            differences = [(ar - br, ai - bi) for (ar, ai), (br, bi) in zip(a.points, b.points)]
            errors = [math.hypot(real, imag) for real, imag in differences]
            if not all(math.isfinite(error) for error in errors):
                rows.append(TermComparison(a.key, "含非有限复数值", []))
                continue
            peak_index = max(range(len(errors)), key=errors.__getitem__)
            maximum = errors[peak_index]
            rms = 0.0
            if maximum:
                rms = maximum * math.sqrt(
                    math.fsum((error / maximum) ** 2 for error in errors) / len(errors)
                )
            axis = a.frequencies_hz if b.frequencies_hz is not None else None
            status = "逐频点" if axis is not None else "按点序（频率未核对）"
            rows.append(TermComparison(a.key, status, differences, axis, maximum, rms, peak_index))
    return rows
