"""Export CalSet error terms or verify a local .cal against a PNA or VNA Simulator."""

import argparse
import hashlib
import math
import re
import struct
import sys
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

from locate_error_term import parse_error_term_response, parse_scpi_ascii_values


DEFAULT_RESOURCE = "TCPIP0::127.0.0.1::hislip0::INSTR"


def split_catalog(response):
    # The PNA can quote the full list. Commas inside port pairs and gain
    # states are part of the error term name.
    fields = []
    start = 0
    depth = 0
    for index, char in enumerate(response):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            fields.append(response[start:index])
            start = index + 1
    fields.append(response[start:])
    return [name for field in fields if (name := field.strip().strip('"').strip())]


def quote_scpi_name(name):
    if not name.strip() or any(char in name for char in "\r\n\x00"):
        raise ValueError("SCPI name must be a nonempty single line")
    return '"' + name.replace('"', '""') + '"'


def select_from_catalog(names, label, list_only=False):
    if not names:
        raise ValueError(f"No {label} returned by the PNA-X")
    for index, name in enumerate(names, start=1):
        print(f"{index:3d}  {name}")
    if list_only:
        return None
    choice = input(f"Select {label} [1-{len(names)}]: ").strip()
    if not choice.isascii() or not choice.isdecimal() or not 1 <= int(choice) <= len(names):
        raise ValueError(f"Selection must be between 1 and {len(names)}")
    return names[int(choice) - 1]


@contextmanager
def ascii_transfer(instrument):
    previous_format = instrument.query(":FORM:DATA?").strip()
    if not re.fullmatch(r"(?:ASC(?:II)?|REAL)(?:\s*,\s*\d+)?", previous_format, re.I):
        raise ValueError(f"Unrecognized :FORM:DATA? response: {previous_format!r}")
    change_format = not previous_format.upper().startswith("ASC")
    if change_format:
        instrument.write(":FORM:DATA ASC")
    try:
        yield
    finally:
        if change_format:
            instrument.write(f":FORM:DATA {previous_format}")


def read_numeric_response(instrument, query, parse_response):
    instrument.write(query)
    try:
        return parse_response(instrument.read_raw().decode("ascii"))
    except ValueError as exc:
        raise ValueError(f"{query} returned invalid numeric data: {exc}") from exc


def read_error_term(instrument, calset, term):
    query = f":CSET:ETER:DATA? {quote_scpi_name(calset)},{quote_scpi_name(term)}"
    return read_numeric_response(instrument, query, parse_error_term_response)


def read_verified_trace(instrument, calset, name, point_count, expected_hz):
    if expected_hz is not None:
        query = f":CSET:ETER:X:VAL? {quote_scpi_name(calset)},{quote_scpi_name(name)}"
        actual_hz = read_numeric_response(instrument, query, parse_scpi_ascii_values)
        if len(actual_hz) != len(expected_hz):
            raise ValueError(f"frequency point count: .cal={len(expected_hz)}, instrument={len(actual_hz)}")
        for point, (expected, actual) in enumerate(zip(expected_hz, actual_hz), start=1):
            if not math.isclose(expected, actual, rel_tol=0.0, abs_tol=0.5):
                raise ValueError(
                    f"frequency mismatch at point {point}: .cal={expected:.12g} Hz, "
                    f"instrument={actual:.12g} Hz"
                )
    values = read_error_term(instrument, calset, name)
    if len(values) != 2 * point_count:
        raise ValueError(f"trace point count={len(values) // 2}, expected={point_count}")
    return list(zip(values[::2], values[1::2]))


def read_verified_references(instrument, calset, names, matrix):
    calset_arg = quote_scpi_name(calset)
    port_response = instrument.query(f":CSET:PROP:PORT:CAT? {calset_arg}")
    ports = [int(port) for port in split_catalog(port_response)]
    if sorted(ports) != sorted(matrix.port_ids):
        raise ValueError(f"CalSet ports differ: .cal has {matrix.port_ids}, instrument has {ports}")

    expected_hz = [frequency * 1e9 for frequency in matrix.freqs_ghz]
    references = {}
    skipped = {}
    for name in names:
        try:
            references[name] = read_verified_trace(
                instrument, calset, name, len(expected_hz), expected_hz,
            )
        except ValueError as exc:
            skipped[name] = str(exc)
    return references, skipped


def verify_named_terms(instrument, calset, names, traces):
    catalog = {name.casefold(): name for name in names}
    print(f"CalSet: {calset}")
    print("\n[Named error term verification]")
    matched = 0
    for trace in traces:
        name = catalog.get(trace.key.casefold())
        if name is None:
            print(f"  {trace.key}: NOT VERIFIED (missing from instrument catalog)")
            continue
        try:
            reference = read_verified_trace(
                instrument, calset, name, len(trace.points), trace.frequencies_hz,
            )
        except ValueError as exc:
            print(f"  {trace.key}: NOT VERIFIED ({exc})")
            continue
        first_mismatch = next(
            (
                point
                for point, (local, remote) in enumerate(zip(trace.points, reference), start=1)
                if not all(
                    math.isclose(actual, expected, rel_tol=5e-7, abs_tol=1e-15)
                    for actual, expected in zip(local, remote)
                )
            ),
            None,
        )
        max_residual = max(
            abs(complex(*local) - complex(*remote))
            for local, remote in zip(trace.points, reference)
        )
        axis = "frequency checked" if trace.frequencies_hz is not None else "point order only"
        status = "MATCH" if first_mismatch is None else f"DIFFER (first point {first_mismatch})"
        print(
            f"  {trace.key}: {status} | {len(reference)} points "
            f"| {axis} | max residual={max_residual:.6g}"
        )
        if first_mismatch is None:
            matched += 1
    print(f"\nMatched: {matched}/{len(traces)} named error terms")


def print_verification_report(calset, matrix, labels, candidates, skipped):
    print(f"CalSet: {calset}")
    print(
        f"Matrix: ports={matrix.port_ids} | version={matrix.ver_a},{matrix.ver_b} "
        f"| offset=0x{matrix.offset:X} | rows={len(matrix.error_terms)} "
        f"| points={len(matrix.freqs_ghz)}"
    )
    row_candidates = [[] for _ in matrix.error_terms]
    print("\n[Simulator terms]")
    for name, indices in candidates.items():
        print(f"  {name}: candidate rows {indices}" if indices else f"  {name}: no matching row")
        for index in indices:
            row_candidates[index].append(name)
    for name, reason in skipped.items():
        print(f"  {name}: skipped ({reason})")

    print("\n[Matrix row mapping]")
    for index, names in enumerate(row_candidates):
        if index in labels:
            print(f"  Term {index}: {labels[index]}")
        elif names:
            print(f"  Term {index}: ambiguous ({', '.join(names)})")
        else:
            print(f"  Term {index}: no matching term")
    print(
        f"\nUnique matches: {len(labels)}/{len(matrix.error_terms)} rows "
        f"| verified traces: {len(candidates)}/{len(candidates) + len(skipped)} catalog entries"
    )


def print_mismatch_diagnostics(matrix, references, match_names):
    rows = matrix.error_terms
    flat_pairs = [pair for row in rows for pair in row]
    term_major_planar = []
    for row in rows:
        scalars = [value for pair in row for value in pair]
        points = len(row)
        term_major_planar.append(list(zip(scalars[:points], scalars[points:])))

    print("\n[Matrix mismatch diagnostics]")
    layouts = {
        "point-major interleaved Re/Im": [
            flat_pairs[index::len(rows)] for index in range(len(rows))
        ],
        "term-major separate Re and Im": term_major_planar,
        "term-major conjugated Re/Im": [
            [(real, -imag) for real, imag in row] for row in rows
        ],
    }
    for layout, alternative in layouts.items():
        matched, _ = match_names(replace(matrix, error_terms=alternative), references)
        print(f"  {layout}: {len(matched)}/{len(rows)} unique matches")
        for index, name in sorted(matched.items()):
            print(f"    Term {index}: {name}")

    finite_rows = {
        index: [complex(real, imag) for real, imag in row]
        for index, row in enumerate(rows)
        if all(math.isfinite(real) and math.isfinite(imag) for real, imag in row)
    }
    if not finite_rows:
        print("  No finite rows available for residual comparison")
        return

    print("  Closest rows in the original layout (by complex RMS residual):")
    for name, values in references.items():
        reference = [complex(real, imag) for real, imag in values]
        root_count = math.sqrt(len(reference))
        residuals = {
            index: math.hypot(*(abs(actual - expected) for actual, expected in zip(row, reference)))
            / root_count
            for index, row in finite_rows.items()
        }
        nearest = min(residuals, key=residuals.__getitem__)
        residual = residuals[nearest]
        reference_rms = math.hypot(*(abs(value) for value in reference)) / root_count
        max_residual = max(
            abs(actual - expected)
            for actual, expected in zip(finite_rows[nearest], reference)
        )
        print(
            f"    {name}: Term {nearest} | RMS residual={residual:.6g} "
            f"| max residual={max_residual:.6g} | reference RMS={reference_rms:.6g}"
        )


def print_sign_and_reflection_checks(matrix, references, match_names):
    print("\n[Sign and reflection checks]")
    negated = {
        name: [(-real, -imag) for real, imag in points]
        for name, points in references.items()
        if any(real or imag for real, imag in points)
    }
    matches, _ = match_names(matrix, negated)
    print(f"  Negated terms: {len(matches)} unique full-trace matches")
    for index, name in sorted(matches.items()):
        print(f"    Term {index} = -{name}")

    by_name = {
        name.casefold(): [complex(real, imag) for real, imag in points]
        for name, points in references.items()
    }
    scales = [
        (f"Term {index}", [complex(real, imag) for real, imag in row])
        for index, row in enumerate(matrix.error_terms)
        if all((real or imag) and math.isfinite(real) and math.isfinite(imag) for real, imag in row)
    ]
    unity = [1.0] * len(matrix.freqs_ghz)
    print("  Inverse reflection model: Gamma = (measured - D) / (M*measured + R - D*M)")
    for port in matrix.port_ids:
        suffix = f"({port},{port})"
        directivity = by_name.get("directivity" + suffix)
        source_match = by_name.get("sourcematch" + suffix)
        tracking = by_name.get("reflectiontracking" + suffix)
        if directivity is None or source_match is None or tracking is None:
            print(f"    Port {port}: insufficient reference terms")
            continue

        # Test a common normalization factor at every frequency. All three
        # coefficients must match different rows for a scaled candidate.
        coefficients = {
            "-D": [-value for value in directivity],
            "M": source_match,
            "R-D*M": [r - d * m for r, d, m in zip(tracking, directivity, source_match)],
        }
        for scale_name, scale in [("unity", unity), *scales]:
            predictions = {}
            for key, values in coefficients.items():
                scaled = (a * b for a, b in zip(scale, values))
                predictions[key] = [(value.real, value.imag) for value in scaled]
            _, candidates = match_names(matrix, predictions)
            d_rows, m_rows = candidates["-D"], candidates["M"]
            distinct_anchors = len(d_rows) == len(m_rows) == 1 and d_rows != m_rows
            if scale_name == "unity" or distinct_anchors:
                detail = ", ".join(f"{key} -> {indices}" for key, indices in candidates.items())
                print(f"    Port {port} | scale {scale_name} | {detail}")

        check_grouped_reflection_rows(matrix, port, coefficients, match_names)


def check_grouped_reflection_rows(matrix, port, coefficients, match_names):
    port_count = len(matrix.port_ids)
    if len(matrix.error_terms) != 6 * port_count:
        return
    port_index = matrix.port_ids.index(port)
    d_index = 3 * port_count + port_index
    m_index = 2 * port_count + port_index
    d_row = [complex(*point) for point in matrix.error_terms[d_index]]
    m_row = [complex(*point) for point in matrix.error_terms[m_index]]
    d, m = coefficients["-D"], coefficients["M"]

    # A common scale cancels out of B*M == C*(-D), even if that scale
    # is not stored as a separate row.
    left = [a * b for a, b in zip(d_row, m)]
    right = [a * b for a, b in zip(m_row, d)]
    residual = math.hypot(*(abs(a - b) for a, b in zip(left, right)))
    level = max(math.hypot(*(abs(value) for value in left)),
                math.hypot(*(abs(value) for value in right)))
    if not level or not math.isfinite(residual) or not math.isfinite(level):
        return
    print(
        f"    Port {port} | grouped rows -D: {d_index}, M: {m_index} "
        f"| relative cross-product residual={residual / level:.6g}"
    )
    if not all(
        math.isclose(a.real, b.real, rel_tol=5e-7, abs_tol=1e-12)
        and math.isclose(a.imag, b.imag, rel_tol=5e-7, abs_tol=1e-12)
        for a, b in zip(left, right)
    ):
        return

    power = [abs(a) ** 2 + abs(b) ** 2 for a, b in zip(d, m)]
    if not all(power):
        return
    scale = [
        (a.conjugate() * x + b.conjugate() * y) / denominator
        for a, b, x, y, denominator in zip(d, m, d_row, m_row, power)
    ]
    tracking = [a * b for a, b in zip(scale, coefficients["R-D*M"])]
    predictions = {"R-D*M": [(value.real, value.imag) for value in tracking]}
    _, candidates = match_names(matrix, predictions)
    print(f"      From fitted common scale: R-D*M -> {candidates['R-D*M']}")


def print_coefficient_samples(matrix, references):
    """Print a few shared points for analyzing combinations of stored rows."""
    count = len(matrix.freqs_ghz)
    indices = sorted({0, count // 2, count - 1})
    print("\n[Coefficient samples]")
    print("  Point indices (0-based): " + ", ".join(map(str, indices)))
    print("  Frequencies (GHz): " + ", ".join(f"{matrix.freqs_ghz[i]:.12g}" for i in indices))
    traces = {f"Term {index}": row for index, row in enumerate(matrix.error_terms)}
    # Keep all diagonal reflection terms and one load-match curve per
    # receiver. Include the transmission paths connected to the first port.
    first_port = matrix.port_ids[0]
    load_ports = set()
    for name, points in references.items():
        match = re.fullmatch(r"([A-Za-z]+)\((\d+),\s*(\d+)\)", name)
        if match is None:
            continue
        kind, receiver, source = match.group(1).casefold(), int(match[2]), int(match[3])
        if kind in {"directivity", "sourcematch", "reflectiontracking"} and receiver == source:
            traces[name] = points
        elif kind == "loadmatch" and receiver not in load_ports:
            traces[name] = points
            load_ports.add(receiver)
        elif kind == "transmissiontracking" and first_port in (receiver, source):
            traces[name] = points

    for name, points in traces.items():
        values = "  ".join(f"({points[i][0]:.12g},{points[i][1]:.12g})" for i in indices)
        print(f"  {name}: {values}")


def print_grouped_matrix_verification(matrix, references):
    from parse_pna_cal import reconstruct_grouped_matrix_terms

    reconstructed, transmission_ratios = reconstruct_grouped_matrix_terms(matrix)
    if not reconstructed:
        return

    reference = {
        name.casefold(): [complex(*point) for point in points]
        for name, points in references.items()
    }
    transmission_links = {}
    for name, (source, ratio) in transmission_ratios.items():
        source_reflection = reference.get(f"reflectiontracking({source},{source})")
        if source_reflection is not None and len(source_reflection) == len(ratio):
            values = [k * r for k, r in zip(ratio, source_reflection)]
            transmission_links[name] = [(value, abs(value)) for value in values]

    print("\n[Grouped matrix verification]")
    print("  A..F: six successive port-indexed blocks in the file")
    print("  Directivity=-D/A | SourceMatch=C/A | ReflectionTracking=B/A+Directivity*SourceMatch")
    print("  LoadMatch(receiver,source)=F_receiver/E_receiver")
    print("  TransmissionTracking(receiver,source)=A_source*ReflectionTracking(source,source)/E_receiver")
    # Bound float32 rounding in the native calculation in terms of the
    # intermediate magnitudes, especially before the reflection subtraction.
    rounding_factor = 8 * 2.0 ** -23
    matched = 0
    tested_links = 0
    matched_links = 0
    for name, predicted in sorted(reconstructed.items()):
        expected = reference.get(name.casefold())
        if expected is None or len(expected) != len(predicted):
            print(f"  {name}: NOT VERIFIED (reference missing)")
            continue
        worst = 0.0
        max_residual = 0.0
        for (value, scale), actual in zip(predicted, expected):
            residual = abs(value - actual)
            if not (math.isfinite(residual) and math.isfinite(scale)):
                worst = max_residual = math.inf
                break
            max_residual = max(max_residual, residual)
            bound = 1e-12 + rounding_factor * max(scale, abs(actual))
            worst = max(worst, residual / bound)
        status = "MATCH" if worst <= 1 else "DIFFER"
        if worst <= 1:
            matched += 1

        link_detail = ""
        if name in transmission_links:
            tested_links += 1
            link = all(
                math.isfinite(scale)
                and abs(value - actual) <= 1e-12 + rounding_factor * max(scale, abs(actual))
                for (value, scale), actual in zip(transmission_links[name], expected)
            )
            matched_links += link
            link_detail = f" | using simulator R: {'MATCH' if link else 'DIFFER'}"
        print(
            f"  {name}: {status} | max residual={max_residual:.6g} "
            f"| tolerance fraction={worst:.6g}{link_detail}"
        )
    print(f"  Grouped model matches: {matched}/{len(reconstructed)} predicted terms")
    if tested_links:
        print(f"  Transmission relation matches: {matched_links}/{tested_links} using simulator R")
    crosstalk = [points for name, points in reference.items() if name.startswith("crosstalk(")]
    if crosstalk:
        zero_traces = sum(all(value == 0 for value in points) for points in crosstalk)
        print(f"  Simulator CrossTalk: {zero_traces}/{len(crosstalk)} traces are zero")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", default=DEFAULT_RESOURCE, help="VISA resource address")
    parser.add_argument("--visa-library", help="VISA backend or library path, e.g. @py")
    parser.add_argument("--calset", help="CalSet name. Omit to select from the instrument.")
    parser.add_argument("--term", help="Exact error term name. Omit to select from the catalog.")
    parser.add_argument("--output", type=Path, default=Path("term.txt"))
    parser.add_argument("--timeout-ms", type=int, default=15000)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-calsets", action="store_true")
    mode.add_argument("--list-terms", action="store_true")
    mode.add_argument(
        "--label-cal", type=Path, metavar="CAL",
        help="Match all PNA terms to the matrix in a local .cal and save its names",
    )
    mode.add_argument(
        "--verify-cal", type=Path, metavar="CAL",
        help="Compare a local .cal with the instrument by term name or by matrix row",
    )
    args = parser.parse_args()
    cal_path = args.verify_cal or args.label_cal
    if cal_path and args.term:
        parser.error("--verify-cal and --label-cal compare every error term; omit --term")
    if args.timeout_ms <= 0:
        parser.error("--timeout-ms must be positive")
    try:
        import pyvisa
    except ImportError:
        parser.error("Install PyVISA or run with uv run --locked --with pyvisa")

    try:
        if cal_path:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from parse_pna_cal import (
                match_matrix_error_term_names,
                parse_cal_file,
                save_matrix_error_term_names,
            )

            cal_data = cal_path.read_bytes()
            parsed = parse_cal_file(cal_data)
            matrix = parsed.error_term_set
            named_traces = parsed.named_error_terms if args.verify_cal else []
            if not named_traces and (matrix is None or not matrix.error_terms):
                expected = "named error terms or an error term matrix" if args.verify_cal else "an error term matrix"
                raise ValueError(f"The .cal does not contain recognized {expected}")

        with closing(pyvisa.ResourceManager(args.visa_library or "")) as manager:
            with closing(manager.open_resource(args.resource)) as instrument:
                instrument.timeout = args.timeout_ms
                instrument.write_termination = "\n"
                print(f"Instrument: {instrument.query('*IDN?').strip()}")

                calset = args.calset
                if cal_path and calset is None:
                    calset = parsed.header.calset_name
                if args.list_calsets or calset is None:
                    names = split_catalog(instrument.query(":CSET:CAT?"))
                    calset = select_from_catalog(names, "CalSets", args.list_calsets)
                    if args.list_calsets:
                        return

                if args.verify_cal:
                    guid = "{" + parsed.header.calset_guid + "}"
                    print("\n[CalSet identity]")
                    print(f"  File: {cal_path.name} | {len(cal_data)} bytes")
                    print(f"  SHA-256: {hashlib.sha256(cal_data).hexdigest()}")
                    print(f"  File CalSet: {parsed.header.calset_name} | GUID: {guid}")
                    try:
                        guid_exists = instrument.query(f":CSET:EXIS? {quote_scpi_name(guid)}").strip()
                    except pyvisa.errors.Error as exc:
                        guid_exists = f"unavailable ({exc})"
                    print(f"  GUID exists in instrument storage: {guid_exists}")

                term = args.term
                if cal_path or args.list_terms or term is None:
                    names = split_catalog(instrument.query(f":CSET:ETER:CAT? {quote_scpi_name(calset)}"))
                    if not cal_path:
                        term = select_from_catalog(names, "error terms", args.list_terms)
                        if args.list_terms:
                            return
                with ascii_transfer(instrument):
                    if cal_path:
                        if not names:
                            raise ValueError(f"No error terms returned for {calset!r}")
                        names = list(dict.fromkeys(names))
                        if args.verify_cal:
                            if named_traces:
                                verify_named_terms(instrument, calset, names, named_traces)
                                return
                            references, skipped = read_verified_references(instrument, calset, names, matrix)
                        else:
                            references = {}
                            for name in names:
                                try:
                                    values = read_error_term(instrument, calset, name)
                                except ValueError as exc:
                                    print(f"{name}: {exc}", file=sys.stderr)
                                    continue
                                references[name] = list(zip(values[::2], values[1::2]))
                    else:
                        values = read_error_term(instrument, calset, term)

        if cal_path:
            labels, candidates = match_matrix_error_term_names(matrix, references)
            if args.verify_cal:
                print_verification_report(calset, matrix, labels, candidates, skipped)
                if references and len(labels) != len(matrix.error_terms):
                    print_mismatch_diagnostics(matrix, references, match_matrix_error_term_names)
                    print_sign_and_reflection_checks(matrix, references, match_matrix_error_term_names)
                    print_coefficient_samples(matrix, references)
                if references:
                    print_grouped_matrix_verification(matrix, references)
                return
            for name, indices in candidates.items():
                if not indices:
                    status = "no match"
                elif len(indices) == 1 and labels.get(indices[0]) == name:
                    status = f"Term {indices[0]}"
                else:
                    status = f"ambiguous (indices: {indices})"
                print(f"{name}: {status}")
            if not labels:
                raise ValueError("No unique full-trace matches for the selected CalSet")
            path = save_matrix_error_term_names(args.label_cal, cal_data, labels)
            print(f"Saved {len(labels)}/{len(matrix.error_terms)} matrix names to {path.resolve()}")
            return

        pairs = list(zip(values[::2], values[1::2]))
        args.output.write_text(
            "".join(f"{real:.17g},{imag:.17g}\n" for real, imag in pairs),
            encoding="utf-8",
        )
        print(f"{calset} | {term} | {len(pairs)} complex points")
        print(f"Saved {args.output.resolve()}")
        if len(pairs) < 6 or len(set(pairs)) < 2:
            print("For file matching, choose a varying term with at least six points.", file=sys.stderr)
    except (OSError, ValueError, EOFError, struct.error, pyvisa.errors.Error) as exc:
        parser.exit(1, f"{args.resource}: {exc}\n")


if __name__ == "__main__":
    main()
