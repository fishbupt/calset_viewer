"""Use a named CalSet change on a test copy to identify stored matrix rows."""

import argparse
import json
import math
import struct
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from export_error_term import (
    DEFAULT_RESOURCE,
    ascii_transfer,
    quote_scpi_name,
    read_error_term,
    split_catalog,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parse_pna_cal import parse_cal_file


def close_value(actual, expected):
    return math.isclose(actual, expected, rel_tol=5e-7, abs_tol=1e-11)


def prepare_probe(instrument, source, term):
    source_arg = quote_scpi_name(source)
    names = split_catalog(instrument.query(f":CSET:ETER:CAT? {source_arg}"))
    if term not in names:
        raise ValueError(f"{term!r} is missing from {source!r}: {', '.join(names)}")

    token = uuid4().hex[:10]
    baseline = f"ProbeBase_{token}"
    modified = f"ProbeEdit_{token}"
    # Check both names before issuing any copy command.
    for name in (baseline, modified):
        reply = instrument.query(f":CSET:EXIS? {quote_scpi_name(name)}").strip().upper()
        if reply not in {"0", "+0", "OFF"}:
            raise ValueError(f"Cannot create {name}: name availability response was {reply!r}")

    with ascii_transfer(instrument):
        original = read_error_term(instrument, source, term)
        points = len(original) // 2
        if points < 3:
            raise ValueError("The probe requires at least three frequency points")

        for name in (baseline, modified):
            instrument.write(f":CSET:COPY {source_arg},{quote_scpi_name(name)}")
            if instrument.query("*OPC?").strip() not in {"1", "+1"}:
                raise ValueError(f"Could not confirm creation of {name}")
            copied = read_error_term(instrument, name, term)
            if copied != original:
                raise ValueError(f"{name} differs from the source before editing")

        edits = {
            points // 4: (0.013, -0.021),
            points // 2: (-0.034, 0.017),
            3 * points // 4: (0.027, 0.039),
        }
        marked = list(original)
        for point, (delta_real, delta_imag) in edits.items():
            marked[2 * point] += delta_real
            marked[2 * point + 1] += delta_imag
        payload = ",".join(format(value, ".17g") for value in marked)
        instrument.write(
            f":CSET:ETER:DATA {quote_scpi_name(modified)},{quote_scpi_name(term)},{payload}"
        )
        if instrument.query("*OPC?").strip() not in {"1", "+1"}:
            raise ValueError(f"Could not confirm the error-term update on {modified}")
        readback = read_error_term(instrument, modified, term)
        if len(readback) != len(marked) or any(
            not close_value(actual, expected) for actual, expected in zip(readback, marked)
        ):
            raise ValueError(f"{modified}: readback does not match the requested changes")

    markers = []
    for point, (delta_real, delta_imag) in edits.items():
        actual_real = readback[2 * point] - original[2 * point]
        actual_imag = readback[2 * point + 1] - original[2 * point + 1]
        if not (math.isclose(actual_real, delta_real, abs_tol=1e-7)
                and math.isclose(actual_imag, delta_imag, abs_tol=1e-7)):
            raise ValueError(f"{modified}: marker at point {point} was not preserved")
        markers.append([point, actual_real, actual_imag])

    return {
        "baseline": baseline,
        "modified": modified,
        "term": term,
        "points": points,
        "markers": markers,
    }


def load_matrix(path, expected_name=None):
    parsed = parse_cal_file(path.read_bytes())
    if expected_name is not None and parsed.header.calset_name != expected_name:
        raise ValueError(
            f"{path.name}: expected CalSet {expected_name!r}, "
            f"found {parsed.header.calset_name!r}. Export only the requested CalSet."
        )
    if parsed.error_term_set is None or not parsed.error_term_set.error_terms:
        names = [trace.key for trace in parsed.named_error_terms]
        names.extend(trace.key for group in parsed.mapped_measurement_groups for trace in group.error_terms)
        detail = f" Saved named terms: {', '.join(names)}" if names else ""
        raise ValueError(f"{path.name}: no recognized error-term matrix.{detail}")
    return parsed.error_term_set


def compare_probe(baseline, modified, manifest, original=None):
    def signature(matrix):
        return (matrix.magic, matrix.ver_a, matrix.ver_b, matrix.port_ids,
                matrix.port_z0, matrix.freqs_ghz, len(matrix.error_terms))

    if signature(baseline) != signature(modified):
        raise ValueError("The two exports differ in layout, ports, reference impedance, or frequency axis")
    if len(baseline.freqs_ghz) != manifest["points"]:
        raise ValueError("Exported matrix point count differs from the simulator probe")
    if original is not None:
        if (signature(original) != signature(baseline)
                or original.error_terms != baseline.error_terms):
            raise ValueError("The baseline matrix differs from the original .cal; probe evidence cannot be applied")
        print("Baseline matrix: identical to the original .cal")

    markers = {point: (real, imag) for point, real, imag in manifest["markers"]}
    if len(markers) != 3 or any(
        not isinstance(point, int) or not 0 <= point < manifest["points"]
        or not math.isfinite(real) or not math.isfinite(imag) or (real, imag) == (0.0, 0.0)
        for point, (real, imag) in markers.items()
    ):
        raise ValueError("Expected three distinct, in-range, nonzero probe markers")

    print(f"Marked error term: {manifest['term']}")
    print(f"Marker indices (0-based): {sorted(markers)}")
    print("\n[Matrix probe]")
    changed_rows = []
    marker_rows = []
    for index, (before, after) in enumerate(zip(baseline.error_terms, modified.error_terms)):
        changes = {
            point: (new_real - old_real, new_imag - old_imag)
            for point, ((old_real, old_imag), (new_real, new_imag)) in enumerate(zip(before, after))
            if not (close_value(new_real, old_real) and close_value(new_imag, old_imag))
        }
        if not changes:
            continue
        changed_rows.append(index)
        same_delta = changes.keys() == markers.keys() and all(
            math.isclose(actual, expected, rel_tol=5e-7, abs_tol=1e-7)
            for point, delta in changes.items()
            for actual, expected in zip(delta, markers[point])
        )
        if same_delta:
            marker_rows.append(index)
        print(
            f"  Term {index}: {len(changes)} changed points, "
            f"{len(changes.keys() & markers.keys())}/3 at marker indices, "
            f"marker deltas {'match' if same_delta else 'differ'}"
        )

    if len(changed_rows) == 1 and changed_rows == marker_rows:
        print(f"  Direct storage match: Term {marker_rows[0]} = {manifest['term']}")
    elif changed_rows:
        print(f"  No one-row direct storage match. Changed rows: {changed_rows}")
    else:
        print("  No matrix changes detected. Check the selected exports and whether the edit persisted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    prepare = modes.add_parser("prepare", help="Create two new CalSet copies and mark one named term")
    prepare.add_argument("--calset", required=True, help="Source CalSet name loaded in the simulator")
    prepare.add_argument("--term", required=True, help="Exact error-term name")
    prepare.add_argument("--manifest", type=Path, default=Path("calset_probe.json"))
    prepare.add_argument("--resource", default=DEFAULT_RESOURCE)
    prepare.add_argument("--visa-library")
    compare = modes.add_parser("compare", help="Compare two exported .cal files offline")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("modified", type=Path)
    compare.add_argument("--manifest", type=Path, default=Path("calset_probe.json"))
    compare.add_argument("--original", type=Path, help="Check that the baseline matrix matches this .cal")
    args = parser.parse_args()

    try:
        if args.mode == "compare":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            baseline = load_matrix(args.baseline, manifest["baseline"])
            modified = load_matrix(args.modified, manifest["modified"])
            original = load_matrix(args.original) if args.original else None
            compare_probe(baseline, modified, manifest, original)
            return
        if args.manifest.exists():
            raise FileExistsError(f"Choose a new --manifest path: {args.manifest}")

        try:
            import pyvisa
        except ImportError:
            parser.error("Install PyVISA or run with uv run --locked --with pyvisa")
        try:
            with closing(pyvisa.ResourceManager(args.visa_library or "")) as manager:
                with closing(manager.open_resource(args.resource)) as instrument:
                    instrument.timeout = 30000
                    instrument.write_termination = "\n"
                    print(f"Instrument: {instrument.query('*IDN?').strip()}")
                    manifest = prepare_probe(instrument, args.calset, args.term)
        except pyvisa.errors.Error as exc:
            parser.exit(1, f"{args.resource}: {exc}\n")

        with args.manifest.open("x", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        print(f"Baseline CalSet: {manifest['baseline']}")
        print(f"Marked CalSet:   {manifest['modified']}")
        print(f"Manifest: {args.manifest.resolve()}")
    except (OSError, ValueError, KeyError, TypeError, EOFError, struct.error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
