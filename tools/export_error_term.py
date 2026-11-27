"""Export CalSet error terms or verify a local .cal matrix against a PNA or VNA Simulator."""

import argparse
import math
import re
import struct
import sys
from contextlib import closing, contextmanager
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
        query = f":CSET:ETER:X:VAL? {calset_arg},{quote_scpi_name(name)}"
        try:
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
            if len(values) != 2 * len(actual_hz):
                raise ValueError(f"trace point count={len(values) // 2}, frequency point count={len(actual_hz)}")
        except ValueError as exc:
            skipped[name] = str(exc)
            continue
        references[name] = list(zip(values[::2], values[1::2]))
    return references, skipped


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
        help="Compare CalSet frequencies and error terms with a local .cal and print the row mapping",
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
            if matrix is None or not matrix.error_terms:
                raise ValueError("The .cal does not contain a recognized error term matrix")

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
