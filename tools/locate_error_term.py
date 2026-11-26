"""Find a PNA-X error-term reference array in a local .cal file."""

import argparse
import math
import re
import struct
from pathlib import Path


def read_reference(path):
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    response = raw.decode(encoding).lstrip()
    if response.startswith("#"):
        if len(response) < 2 or response[1] not in "0123456789":
            raise ValueError("Invalid SCPI block header")
        digits = int(response[1])
        start = 2 + digits
        if digits:
            stop = start + int(response[2:start])
            if stop > len(response) or response[stop:].strip():
                raise ValueError("SCPI ASCII block length does not match its payload")
            response = response[start:stop]
        else:
            response = response[start:]

    values = tuple(float(token) for token in re.split(r"[,\s]+", response) if token)
    if len(values) < 12 or len(values) % 2:
        raise ValueError("Expected at least six complete Re/Im pairs")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Reference contains non-finite values")
    if len(set(zip(values[::2], values[1::2]))) < 2:
        raise ValueError("Choose an error term that varies between points")
    return values


def locate_term(data, values):
    """Yield offsets for complete matches in little-endian float32 or float64."""
    count = len(values) // 2
    anchor_index = max(range(len(values)), key=lambda index: abs(values[index]))
    anchor = values[anchor_index]
    point_index, component = divmod(anchor_index, 2)
    layouts = (
        ("interleaved RI", values, anchor_index),
        ("separate R and I", values[::2] + values[1::2], point_index + component * count),
    )

    def matches(a, b):
        return math.isclose(a, b, rel_tol=5e-7, abs_tol=1e-15)

    view = memoryview(data)
    for code, type_name in (("f", "float32"), ("d", "float64")):
        scalar = struct.Struct("<" + code)
        array = struct.Struct(f"<{len(values)}{code}")
        width = scalar.size
        for alignment in range(width):
            end = alignment + ((len(data) - alignment) // width) * width
            for index, (value,) in enumerate(scalar.iter_unpack(view[alignment:end])):
                if not matches(value, anchor):
                    continue
                for layout, expected, position in layouts:
                    start = alignment + (index - position) * width
                    stop = start + array.size
                    if start < 0 or stop > len(data):
                        continue
                    first, = scalar.unpack_from(data, start)
                    if not matches(first, expected[0]):
                        continue
                    actual = array.unpack_from(data, start)
                    if all(matches(a, b) for a, b in zip(actual, expected)):
                        yield start, stop, type_name, layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cal", type=Path, help="Local .cal file")
    parser.add_argument("reference", type=Path, help="ASCII SCPI response with Re/Im pairs")
    args = parser.parse_args()
    try:
        data = args.cal.read_bytes()
        values = read_reference(args.reference)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(f"CAL bytes={len(data)} | reference points={len(values) // 2}")
    hits = sorted(locate_term(data, values))
    for start, stop, type_name, layout in hits:
        print(f"0x{start:X}..0x{stop:X} (end exclusive) | {type_name} | {layout}")
        print("  preceding bytes:", data[max(0, start - 24):start].hex(" ").upper())
        print("  following bytes:", data[stop:stop + 16].hex(" ").upper())
    print(f"Full matches: {len(hits)}")


if __name__ == "__main__":
    main()
