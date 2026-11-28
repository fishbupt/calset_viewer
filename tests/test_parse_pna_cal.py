"""Regression tests for PNA calibration record layouts."""

import csv
import json
import struct
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from parse_pna_cal import (
    BinaryReader,
    ParsedCalFile,
    encode_cstringw,
    export_results,
    find_and_parse_section4_mapped,
    find_and_parse_section5_cal_settings,
    find_and_parse_section7_error_terms,
    inspect_error_terms,
    load_matrix_error_term_names,
    match_matrix_error_term_names,
    parse_cal_file,
    parse_section1_header,
    save_matrix_error_term_names,
)


class CalRecordLayoutTests(unittest.TestCase):
    def test_file_header_reads_records_before_schemas(self):
        names = ["CalSet_DIQ_DIQ_001", "CalSet_DIQ_DIQ_002", "CalSet_DIQ_DIQ_003"]
        for count, schema_offset in [(1, 0x7A), (3, 0x102)]:
            with self.subTest(count=count):
                data = encode_cstringw("PNA Cal File") + struct.pack("<III", 1, 3, count)
                for index, name in enumerate(names[:count], start=1):
                    data += encode_cstringw(name) + UUID(int=index).bytes_le
                    data += struct.pack("<III", 0x01DD7D45, index * 1000, 0x3C)
                data += struct.pack("<IIHI", 2, 3, 15, 1)
                self.assertEqual(len(data), schema_offset)
                data += encode_cstringw("Allegro") + struct.pack("<I", 102)
                data += struct.pack("<I", 47)  # Marker for the next section.

                reader = BinaryReader(data)
                header = parse_section1_header(reader)

                self.assertEqual(header.calset_name, names[0])
                self.assertEqual(header.calset_guid, str(UUID(int=1)))
                self.assertEqual(header.header_flags, 0x3C)
                self.assertEqual([record.name for record in header.calset_records], names[:count])
                self.assertEqual(
                    [record.guid for record in header.calset_records],
                    [str(UUID(int=index)) for index in range(1, count + 1)],
                )
                self.assertEqual(header.schemas, {"Allegro": 102})
                self.assertEqual(reader.u32(), 47)

    def test_diq_groups_keep_duplicate_keys_and_frequency_points(self):
        names = [f"CalSet_DIQ_{index:03d}" for index in range(1, 4)]
        guids = [
            UUID(f"12345678-1248-4321-a2b3-1223344556{index:02x}")
            for index in range(1, 4)
        ]
        data = encode_cstringw("PNA Cal File") + struct.pack("<III", 1, 3, len(names))
        for name, guid in zip(names, guids):
            data += encode_cstringw(name) + guid.bytes_le + struct.pack("<III", 0, 0, 0x3C)
        data += struct.pack("<IIHIII", 2, 3, 15, 0, 0, 65)

        frequencies = (50e6, 50e6, 250e6)
        term_keys = ["CrossTalk(1,2)", "Directivity(1,1)", "TransmissionTracking(4,3)"]
        term_frequencies = (10e6, 20e6, 30e6)
        for index, guid in enumerate(guids, start=1):
            data += guid.bytes_le + b"\x00"

            # Another valid trace map shares the first key. The other term names identify
            # the actual error term map. A third map follows it, as in the DIQ sample.
            data += struct.pack("<I", 1)
            data += self.complex_trace(term_keys[0], -1, frequencies, save_axis=False)
            data += struct.pack("<I", len(term_keys))
            for key in term_keys:
                data += self.complex_trace(
                    key, index + 10, term_frequencies,
                    save_axis=index == 3 and key == "Directivity(1,1)",
                )
            data += struct.pack("<I", 1)
            data += self.complex_trace("CALALL_R(1)", -2, frequencies)

            data += self.complex_trace("SA_0(1,1)", index, frequencies)
            data += self.complex_trace("TotalSourcePowerCorrection(1)", index + 3, frequencies)
            data += struct.pack("<I", 8) + encode_cstringw("DIQ Stimulus")

        parsed = parse_cal_file(data)
        groups = parsed.mapped_measurement_groups
        self.assertEqual([group.calset.name for group in groups], names)
        self.assertEqual(len(parsed.mapped_measurements), 6)
        for index, group in enumerate(groups, start=1):
            self.assertEqual([tr.key for tr in group.traces], [
                "SA_0(1,1)", "TotalSourcePowerCorrection(1)",
            ])
            self.assertEqual(group.traces[0].frequencies_hz, frequencies)
            self.assertEqual(group.traces[0].points, [(float(index), 0.0)] * 3)
            self.assertEqual([trace.key for trace in group.error_terms], term_keys)
            self.assertIsNone(group.error_terms[0].frequencies_hz)
            self.assertEqual(
                group.error_terms[1].frequencies_hz, term_frequencies if index == 3 else None
            )
            self.assertEqual(group.error_terms[1].points, [(float(index + 10), 0.0)] * 3)

        with TemporaryDirectory() as directory:
            path = Path(directory)
            export_results(parsed, path)
            summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [group["trace_count"] for group in summary["mapped_measurement_groups"]],
                [2, 2, 2],
            )
            with (path / "mapped_measurements.csv").open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 18)
            self.assertEqual([float(row["freq_hz"]) for row in rows[:3]], list(frequencies))
            self.assertEqual(rows[-1]["calset_name"], names[-1])
            self.assertEqual(summary["counts"]["error_terms"], 9)
            self.assertEqual(
                [group["error_term_count"] for group in summary["mapped_measurement_groups"]],
                [3, 3, 3],
            )
            with (path / "diq_error_terms.csv").open(newline="", encoding="utf-8") as f:
                terms = list(csv.DictReader(f))
            self.assertEqual(len(terms), 27)
            self.assertEqual([row["calset_name"] for row in terms[::9]], names)
            self.assertEqual([row["error_term"] for row in terms[:9:3]], term_keys)
            self.assertEqual(
                [float(row["freq_hz"]) for row in terms if row["freq_hz"]],
                list(term_frequencies),
            )
            self.assertTrue(all(row["freq_hz"] for row in terms[21:24]))
            self.assertTrue(all(row["freq_hz"] == "" for row in terms[:21] + terms[24:]))

    def test_mapped_table_accepts_other_standard_and_port_and_skips_decoy(self):
        data = (
            struct.pack("<I", 4) + encode_cstringw("SA_0(1,1)") + b"\x00" * 6
            + struct.pack("<I", 2)
            + self.complex_trace("SB_1(3,3)", 1, range(3))
            + self.complex_trace("SC_0(3,3)", 2, range(3))
        )

        traces = find_and_parse_section4_mapped(data, 0)

        self.assertEqual([trace.key for trace in traces], ["SB_1(3,3)", "SC_0(3,3)"])
        self.assertEqual(traces[1].points, [(2.0, 0.0)] * 3)

    def test_matrix_names_require_unique_full_traces_and_matching_file_hash(self):
        data = self.error_terms(b"", struct.pack("<dd", 50.0, 0.0))
        matrix = find_and_parse_section7_error_terms(data, 0)
        matrix.error_terms[1] = matrix.error_terms[0]
        reference = {
            "Directivity(1,1)": matrix.error_terms[2],
            "SourceMatch(1,1)": matrix.error_terms[0],
            "Wrong(1,1)": [*matrix.error_terms[3][:-1], (3.0, 999.0)],
            "AliasA(1,1)": matrix.error_terms[4],
            "AliasB(1,1)": matrix.error_terms[4],
        }

        labels, candidates = match_matrix_error_term_names(matrix, reference)

        self.assertEqual(labels, {2: "Directivity(1,1)"})
        self.assertEqual(candidates["SourceMatch(1,1)"], [0, 1])
        self.assertEqual(candidates["Wrong(1,1)"], [])
        self.assertEqual(candidates["AliasA(1,1)"], [4])
        header = parse_section1_header(BinaryReader(
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw("CalSet") + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHI", 0, 0, 0x3C, 2, 1, 15, 0)
        ))
        parsed = ParsedCalFile(len(data), header, [], error_term_set=matrix)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.cal"
            save_matrix_error_term_names(path, data, labels)
            load_matrix_error_term_names(parsed, path, data + b"\x00")
            self.assertEqual(matrix.term_label(2), "Term 2")
            load_matrix_error_term_names(parsed, path, data)
            self.assertEqual(matrix.term_label(2), "Directivity(1,1)")
            self.assertEqual(matrix.term_label(0), "Term 0")

    def test_one_port_named_error_terms_before_metadata_or_matrix(self):
        name = "CalSet_OnePort"
        data = (
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw(name) + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHII", 0, 0, 0x3C, 2, 1, 15, 0, 0)
        )

        # The sample stores 3 consecutive length-prefixed records, each with
        # outer_size=0x9C68 and inner_size=0x9C54 for 5000 complex float pairs.
        keys = ["Directivity(1,1)", "ReflectionTracking(1,1)", "SourceMatch(1,1)"]
        points = range(5000)
        bad_keys = [keys[0], keys[1], "SourceMatch(2,2)"]
        data += struct.pack("<I", 3)
        for key in bad_keys:
            data += self.complex_trace(key, -1, range(3), save_axis=False)
        first_offset = len(data) + 4
        data += struct.pack("<I", 3)
        for index, key in enumerate(keys, start=1):
            data += self.complex_trace(key, index, points, save_axis=False)
        record_offset = first_offset + len(encode_cstringw(keys[0]))
        self.assertEqual(struct.unpack_from("<I", data, record_offset + 2)[0], 0x9C68)
        self.assertEqual(struct.unpack_from("<I", data, record_offset + 16)[0], 0x9C54)
        mapped_keys = ["SA(1,1)", "SB(1,1)", "SC(1,1)"]
        data += struct.pack("<I", len(mapped_keys))
        for index, key in enumerate(mapped_keys, start=1):
            data += self.complex_trace(key, index / 4, points, save_axis=False)
        data += struct.pack("<7I", 0, 1006, 0, 0, 0, 0, 0)

        parsed = parse_cal_file(data)

        self.assertIsNone(parsed.error_term_set)
        self.assertEqual([trace.key for trace in parsed.named_error_terms], keys)
        self.assertEqual([trace.key for trace in parsed.mapped_measurements], mapped_keys)
        for index, trace in enumerate(parsed.mapped_measurements, start=1):
            self.assertIsNone(trace.frequencies_hz)
            self.assertEqual(trace.points, [(index / 4, 0.0)] * 5000)
        for value, trace in enumerate(parsed.named_error_terms, start=1):
            self.assertIsNone(trace.frequencies_hz)
            self.assertEqual(trace.points, [(float(value), 0.0)] * 5000)
        with TemporaryDirectory() as directory:
            path = Path(directory)
            export_results(parsed, path)
            summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["counts"]["error_terms"], 3)
            self.assertEqual(summary["counts"]["mapped_measurements"], 3)
            with (path / "mapped_measurements.csv").open(newline="", encoding="utf-8") as f:
                mapped_rows = list(csv.DictReader(f))
            self.assertEqual(len(mapped_rows), 15_000)
            self.assertEqual([row["trace_key"] for row in mapped_rows[::5000]], mapped_keys)
            with (path / "named_error_terms.csv").open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 15_000)
            self.assertEqual([row["error_term"] for row in rows[::5000]], keys)
            self.assertEqual(rows[4999]["point_index"], "4999")
            self.assertTrue(all(row["freq_hz"] == "" for row in rows))
            self.assertTrue(all(row["calset_name"] == name for row in rows))
            self.assertEqual(rows[-1]["real"], "3")

    def test_afr_two_port_named_error_terms_with_no_matrix_or_trace_axis(self):
        data = (
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw("CalSet_2Port_Deembedded") + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHII", 0, 0, 0x3C, 2, 1, 15, 0, 0)
        )
        keys = [
            "CrossTalk(1,2)", "CrossTalk(2,1)", "Directivity(1,1)", "Directivity(2,2)",
            "LoadMatch(1,2)", "LoadMatch(2,1)", "ReflectionTracking(1,1)",
            "ReflectionTracking(2,2)", "SourceMatch(1,1)", "SourceMatch(2,2)",
            "TransmissionTracking(1,2)", "TransmissionTracking(2,1)",
        ]
        decoy = keys[:-1] + ["TransmissionTracking(3,1)"]
        data += struct.pack("<I", 12) + b"".join(
            self.complex_trace(key, -1, range(3), save_axis=False) for key in decoy
        )
        data += bytes(0x2EBE - len(data) - 4)
        data += struct.pack("<I", 12) + b"".join(
            self.complex_trace(key, index, range(5000), save_axis=False)
            for index, key in enumerate(keys)
        )
        data += struct.pack("<7I", 0, 1006, 0, 0, 0, 0, 0)
        record_offset = 0x2EBE + len(encode_cstringw(keys[0]))
        self.assertEqual(struct.unpack_from("<I", data, record_offset + 2)[0], 0x9C68)
        self.assertEqual(data.index(encode_cstringw(keys[1]), 0x2EBE), 0xCB4C)

        parsed = parse_cal_file(data)

        self.assertIsNone(parsed.error_term_set)
        self.assertEqual([trace.key for trace in parsed.named_error_terms], keys)
        for index, trace in enumerate(parsed.named_error_terms):
            self.assertEqual(trace.points, [(float(index), 0.0)] * 5000)
            self.assertIsNone(trace.frequencies_hz)
        output = StringIO()
        with redirect_stdout(output):
            inspect_error_terms(data)
        self.assertIn("Normal import: 0 matrix terms, 12 named terms", output.getvalue())

    @staticmethod
    def complex_trace(key, value, frequencies, *, save_axis=True):
        n = len(frequencies)
        inner = struct.pack("<IIHHII", 1, 0, 20, 3, 1, n)
        inner += struct.pack("<ff", value, 0) * n
        if save_axis:
            tail = struct.pack("<HII14xIBI", 1, 1, 1, n, 3, 8 * n)
            tail += struct.pack(f"<{n}d", *frequencies)
        else:
            tail = struct.pack("<HI", 1, 0)
        body = struct.pack("<HIHHI", 1, 1, 2, 0x0100, len(inner)) + inner + tail
        return encode_cstringw(key) + struct.pack("<HI", 1, len(body)) + body

    def test_cal_settings_port_table_with_prefixes(self):
        # Words immediately after the timestamp in the four-port sample.
        table = bytes.fromhex("01 00 00 00 00 00 00 00 04 00 00 00")
        for port in range(1, 5):
            table += struct.pack("<II", 0, port)
            table += encode_cstringw("1.85 mm male")
            table += encode_cstringw(f"ECal kit {port}")

        settings = find_and_parse_section5_cal_settings(self.cal_settings(table), 0)

        self.assertIsNotNone(settings)
        self.assertEqual(settings.timestamp_utc, "2026-11-25 07:26:59")
        self.assertEqual([entry["port"] for entry in settings.port_kits], [1, 2, 3, 4])
        self.assertEqual(settings.port_kits[0]["connector"], "1.85 mm male")
        self.assertEqual(settings.port_kits[-1]["cal_kit"], "ECal kit 4")
        self.assertEqual(settings.core_stimulus_text, "101 points")

    def test_cal_settings_port_table_without_prefixes(self):
        table = struct.pack("<II", 1, 1)
        table += encode_cstringw("3.5 mm female") + encode_cstringw("Legacy kit")

        settings = find_and_parse_section5_cal_settings(self.cal_settings(table), 0)

        self.assertIsNotNone(settings)
        self.assertEqual(settings.port_kits, [
            {"port": 1, "connector": "3.5 mm female", "cal_kit": "Legacy kit"},
        ])

    def test_two_port_matrix_reconstructs_named_terms_without_z0_prefix(self):
        header = (
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw("CalSet_2Port") + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHII", 0, 0, 0x3C, 2, 1, 15, 0, 0)
        )
        coefficients = [
            1, 2j,  # A
            0.46875j, 0.25 + 0.5j,  # B
            0.25, -1,  # C
            -0.125j, -0.5j,  # D
            2, 4,  # E
            1, 1j,  # F
        ]
        matrix = (
            struct.pack("<8I", 0, 1006, 1, 1, 2, 1, 2, 2)
            + struct.pack("<dd", 50.0, 0.0) * 2
            + struct.pack("<I2d", 2, 0.01, 0.02)
            + struct.pack("<5I", 1, 1, 0, 12, 2)
            + b"".join(struct.pack("<dd", value.real, value.imag) * 2 for value in coefficients)
        )
        parsed = parse_cal_file(header + matrix)
        self.assertEqual(len(parsed.error_term_set.error_terms), 12)
        traces = {trace.key: trace for trace in parsed.reconstructed_error_terms}
        expected = {
            "Directivity(1,1)": 0.125j,
            "Directivity(2,2)": 0.25,
            "SourceMatch(1,1)": 0.25,
            "SourceMatch(2,2)": 0.5j,
            "ReflectionTracking(1,1)": 0.5j,
            "ReflectionTracking(2,2)": 0.25,
            "LoadMatch(1,2)": 0.5,
            "LoadMatch(2,1)": 0.25j,
            "TransmissionTracking(1,2)": 0.25j,
            "TransmissionTracking(2,1)": 0.125j,
        }
        self.assertEqual(set(traces), set(expected))
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(traces[name].points, [(value.real, value.imag)] * 2)
                self.assertEqual(traces[name].frequencies_hz, (10e6, 20e6))

    def test_four_port_matrix_reconstructs_named_terms_and_exports_both_views(self):
        header = (
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw("CalSet_4Port") + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHII", 0, 0, 0x3C, 2, 1, 15, 0, 0)
        )
        # Distinct complex coefficients verify block order and both path directions.
        a = [1, 2j, 3, 4]
        directivity = [0.125j, 0.25, 0.375, 0.5]
        source_match = [0.25, 0.5j, 0.75, 1]
        reflection = [0.5j, 0.25, 0.125, 0.0625]
        e = [2, 4, 8, 16]
        load_match = [0.5, 0.25j, 0.125, 0.0625]
        coefficients = [
            *a,
            *(scale * (r - d * m) for scale, r, d, m in zip(a, reflection, directivity, source_match)),
            *(scale * m for scale, m in zip(a, source_match)),
            *(-scale * d for scale, d in zip(a, directivity)),
            *e,
            *(scale * match for scale, match in zip(e, load_match)),
        ]
        matrix = (
            struct.pack("<11I", 0, 1006, 1, 1, 4, 1, 2, 3, 4, 4, 1)
            + struct.pack("<dd", 50.0, 0.0) * 4
            + struct.pack("<I3d", 3, 0.01, 0.02, 0.03)
            + struct.pack("<5I", 1, 1, 0, 24, 3)
            + b"".join(struct.pack("<dd", value.real, value.imag) * 3 for value in coefficients)
        )
        parsed = parse_cal_file(header + matrix)
        self.assertFalse(parsed.named_error_terms)
        self.assertEqual(len(parsed.error_term_set.error_terms), 24)
        self.assertEqual(parsed.error_term_set.term_names, {})
        traces = {trace.key: trace for trace in parsed.reconstructed_error_terms}
        self.assertEqual(len(traces), 36)
        self.assertFalse(any(key.startswith("CrossTalk") for key in traces))
        expected = {
            "Directivity(1,1)": 0.125j,
            "SourceMatch(2,2)": 0.5j,
            "ReflectionTracking(2,2)": 0.25,
            "LoadMatch(2,1)": 0.25j,
            "LoadMatch(2,4)": 0.25j,
            "TransmissionTracking(1,2)": 0.25j,
            "TransmissionTracking(2,1)": 0.125j,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(traces[name].points, [(value.real, value.imag)] * 3)
                self.assertEqual(traces[name].frequencies_hz, (10e6, 20e6, 30e6))

        with TemporaryDirectory() as directory:
            output = Path(directory)
            export_results(parsed, output)
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["counts"]["error_terms"], 24)
            self.assertEqual(summary["counts"]["reconstructed_error_terms"], 36)
            self.assertEqual(set(summary["reconstructed_error_term_names"]), set(traces))
            with (output / "reconstructed_error_terms.csv").open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 36 * 3)
                self.assertEqual([float(row["freq_hz"]) for row in rows[:3]], [10e6, 20e6, 30e6])
                self.assertEqual(rows[0]["error_term"], "Directivity(1,1)")
            with (output / "error_terms.csv").open(newline="", encoding="utf-8") as f:
                self.assertIn("term23_im", next(csv.reader(f)))

    def test_error_terms_with_z0_prefix(self):
        data = self.error_terms(struct.pack("<I", 1), struct.pack("<dd", 50.0, 0.0))

        error_terms = find_and_parse_section7_error_terms(data, 0)

        self.assertIsNotNone(error_terms)
        self.assertEqual(error_terms.port_ids, [1, 2, 3, 4])
        self.assertEqual(error_terms.port_z0, [(50.0, 0.0)] * 4)
        self.assertEqual(len(error_terms.freqs_ghz), 101)
        self.assertAlmostEqual(error_terms.freqs_ghz[0], 0.01)
        self.assertAlmostEqual(error_terms.freqs_ghz[-1], 1.0)
        self.assertEqual(len(error_terms.error_terms), 24)
        self.assertEqual(len(error_terms.error_terms[-1]), 101)
        self.assertEqual(error_terms.error_terms[-1][-1], (23.0, -100.0))

    def test_inspect_error_terms_reports_matrix_metadata_as_file_offsets(self):
        header = (
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw("CalSet") + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHII", 0, 0, 0x3C, 2, 1, 15, 0, 0)
        )
        data = header + self.error_terms(struct.pack("<I", 1), struct.pack("<dd", 50.0, 0.0))
        matrix_end = len(data)
        matrix_start = matrix_end - 24 * 101 * 16
        payload_text = b"CrossTalk"
        data = data[:matrix_start] + payload_text + data[matrix_start + len(payload_text):]
        trailer_prefix = b"Directivity\x00\x01"
        data += trailer_prefix + "Load_Match".encode("utf-16le")

        output = StringIO()
        with redirect_stdout(output):
            inspect_error_terms(data)

        report = output.getvalue()
        self.assertIn("Normal import: 24 matrix terms, 0 named terms", report)
        self.assertIn(f"Complex data: 0x{matrix_start:X}..0x{matrix_end:X}", report)
        self.assertIn(f"{matrix_end:08X}  44 69 72", report)
        self.assertIn(f"0x{matrix_end:X}: Directivity (ascii)", report)
        self.assertIn(f"0x{matrix_end + len(trailer_prefix):X}: Load_Match (utf-16le)", report)
        self.assertNotIn("CrossTalk", report)

    def test_error_terms_without_prefix_and_z0_starts_with_one(self):
        first_z0 = bytes.fromhex("01 00 00 00 00 00 49 40")
        first_z0 += struct.pack("<d", 0.0)
        data = self.error_terms(b"", first_z0)

        error_terms = find_and_parse_section7_error_terms(data, 0)

        self.assertIsNotNone(error_terms)
        self.assertEqual(error_terms.port_z0[0][0], struct.unpack_from("<d", first_z0)[0])
        self.assertEqual(len(error_terms.freqs_ghz), 101)
        self.assertEqual(error_terms.error_terms[0][1], (0.0, -1.0))

    @staticmethod
    def cal_settings(table):
        return (
            encode_cstringw("Cal Settings File")
            + struct.pack("<H", 1)
            + encode_cstringw("")
            + struct.pack("<III", 1105, 1, 0)
            + encode_cstringw("2026-11-25 07:26:59")
            + table
            + encode_cstringw("Std CoreStimulus")
            + struct.pack("<H", 1)
            + encode_cstringw("101 points")
        )

    @staticmethod
    def error_terms(prefix, first_z0):
        header = struct.pack("<10I", 0, 1006, 1, 1, 4, 1, 2, 3, 4, 4)
        z0 = first_z0 + struct.pack("<dd", 50.0, 0.0) * 3
        frequencies = struct.pack("<I101d", 101, *(0.01 + i * 0.0099 for i in range(101)))
        dimensions = struct.pack("<5I", 1, 1, 0, 24, 101)
        matrix = b"".join(
            struct.pack("<dd", term, -point)
            for term in range(24)
            for point in range(101)
        )
        return header + prefix + z0 + frequencies + dimensions + matrix


if __name__ == "__main__":
    unittest.main()
