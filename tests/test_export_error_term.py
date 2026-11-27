"""Exercise the verification CLI with simulated VISA responses."""

import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from uuid import UUID

import test_parse_pna_cal
from parse_pna_cal import ErrorTermSet, encode_cstringw, match_matrix_error_term_names

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "export_error_term.py"
with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]):
    from export_error_term import (
        check_grouped_reflection_rows,
        main,
        print_coefficient_samples,
        print_grouped_matrix_verification,
        print_mismatch_diagnostics,
        print_sign_and_reflection_checks,
    )


class VerifyCalTests(unittest.TestCase):
    def test_one_port_cli_compares_named_traces_with_and_without_frequency_axis(self):
        calset = "Local1Port"
        keys = ["Directivity(1,1)", "ReflectionTracking(1,1)", "SourceMatch(1,1)"]
        frequencies = (10e6, 20e6, 30e6)
        make_trace = test_parse_pna_cal.CalRecordLayoutTests.complex_trace
        data = (
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw(calset) + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHIII", 0, 0, 0x3C, 2, 1, 15, 0, 0, 3)
            + make_trace(keys[0], 0.1, frequencies)
            + make_trace(keys[1], 1.0, frequencies, save_axis=False)
            + make_trace(keys[2], 0.2, frequencies, save_axis=False)
        )
        instrument = Mock()
        manager = Mock()
        manager.open_resource.return_value = instrument
        visa = SimpleNamespace(ResourceManager=Mock(return_value=manager))
        instrument.query.side_effect = {
            "*IDN?": "Keysight Technologies,N5245B,0,A.20.82.01",
            ':CSET:EXIS? "{00000000-0000-0000-0000-000000000001}"': "1",
            f':CSET:ETER:CAT? "{calset}"': '"' + ",".join(reversed(keys)) + '"',
            ":FORM:DATA?": "ASC",
        }.__getitem__
        instrument.read_raw.side_effect = [
            b"10000000,20000000,30000000", b"0.1,0,0.1,0,0.1,0",
            b"1,0,1,0,1,0.0001", b"0.2,0,0.2,0",
        ]
        output = StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.cal"
            path.write_bytes(data)
            args = [str(SCRIPT), "--verify-cal", str(path)]
            with patch.dict(sys.modules, {"pyvisa": visa}), patch.object(sys, "argv", args):
                with redirect_stdout(output):
                    main()
            self.assertEqual(list(Path(folder).iterdir()), [path])

        report = output.getvalue()
        self.assertIn("Directivity(1,1): MATCH | 3 points | frequency checked", report)
        self.assertIn("ReflectionTracking(1,1): DIFFER (first point 3) | 3 points | point order only", report)
        self.assertIn("SourceMatch(1,1): NOT VERIFIED (trace point count=2, expected=3)", report)
        self.assertIn("Matched: 1/3 named error terms", report)
        self.assertEqual(instrument.write.call_args_list, [
            call(f':CSET:ETER:X:VAL? "{calset}","{keys[0]}"'),
            *(call(f':CSET:ETER:DATA? "{calset}","{key}"') for key in keys),
        ])
        instrument.close.assert_called_once()
        manager.close.assert_called_once()

    def test_report_matches_complete_traces_only_after_frequency_validation(self):
        freqs_hz = (10e6, 20e6, 30e6)
        freqs_ghz = tuple(frequency / 1e9 for frequency in freqs_hz)
        rows = [
            tuple(value for point in range(3) for value in (row, row * point))
            for row in range(12)
        ]
        rows[1] = rows[0]
        data = (
            encode_cstringw("PNA Cal File")
            + struct.pack("<III", 1, 3, 1)
            + encode_cstringw("Local2Port") + UUID(int=1).bytes_le
            + struct.pack("<IIIIIHII", 0, 0, 0x3C, 2, 1, 15, 0, 0)
            + struct.pack("<9I4dI3d5I", 0, 1006, 1, 1, 2, 1, 2, 2, 1,
                          50.0, 0.0, 50.0, 0.0, 3, *freqs_ghz, 1, 1, 0, 12, 3)
            + b"".join(struct.pack("<6d", *row) for row in rows)
        )

        instrument = Mock()
        manager = Mock()
        manager.open_resource.return_value = instrument
        visa = SimpleNamespace(ResourceManager=Mock(return_value=manager))
        catalog = (
            '"Directivity(1,1),Isolation(2,1),SourceMatch(1,1),LoadMatch(2,1),'
            'ReflectionTracking(2,2)"'
        )
        query_responses = {
            "*IDN?": "Keysight Technologies,N5247B,0,A.20.82.01",
            ':CSET:ETER:CAT? "Simulator2Port"': catalog,
            ":FORM:DATA?": "REAL,64",
            ':CSET:PROP:PORT:CAT? "Simulator2Port"': "1,2",
            ':CSET:EXIS? "{00000000-0000-0000-0000-000000000001}"': "0",
        }
        instrument.query.side_effect = query_responses.__getitem__
        axis = ",".join(map(str, freqs_hz)).encode("ascii")
        ieee_axis = b"#2" + str(len(axis)).encode("ascii") + axis + b"\n"
        instrument.read_raw.side_effect = [
            ieee_axis, b"2,0,2,2,2,4",
            axis, b"0,0,0,0,0,0",
            b"10000000,20000001,30000000",
            axis, b"4,0,4,4,4,999",
            axis, b"5,0,5,5",
        ]

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.cal"
            path.write_bytes(data)
            args = [str(SCRIPT), "--verify-cal", str(path), "--calset", "Simulator2Port"]
            output = StringIO()
            with patch.dict(sys.modules, {"pyvisa": visa}), patch.object(sys, "argv", args):
                with redirect_stdout(output):
                    main()
            self.assertEqual(list(Path(folder).iterdir()), [path])
            self.assertEqual(path.read_bytes(), data)

        report = output.getvalue()
        self.assertIn("GUID exists in instrument storage: 0", report)
        self.assertIn("rows=12 | points=3", report)
        self.assertIn("Term 2: Directivity(1,1)", report)
        self.assertIn("Term 0: ambiguous (Isolation(2,1))", report)
        self.assertIn("Term 1: ambiguous (Isolation(2,1))", report)
        self.assertIn("SourceMatch(1,1): skipped (frequency mismatch at point 2", report)
        self.assertIn("LoadMatch(2,1): no matching row", report)
        self.assertIn("ReflectionTracking(2,2): skipped (trace point count=2", report)
        self.assertIn("Unique matches: 1/12 rows | verified traces: 3/5", report)
        self.assertIn("Directivity(1,1): Term 2 | RMS residual=0 | max residual=0", report)
        self.assertNotIn(
            call(':CSET:ETER:DATA? "Simulator2Port","SourceMatch(1,1)"'),
            instrument.write.call_args_list,
        )
        self.assertEqual(
            [args[0] for args, _ in instrument.write.call_args_list if "?" not in args[0]],
            [":FORM:DATA ASC", ":FORM:DATA REAL,64"],
        )
        instrument.close.assert_called_once()
        manager.close.assert_called_once()

    def test_mismatch_diagnostics_recognizes_point_major_data(self):
        references = {
            "Directivity(1,1)": [(1.0, 2.0), (2.0, 3.0), (3.0, 4.0)],
            "SourceMatch(1,1)": [(4.0, 5.0), (5.0, 6.0), (6.0, 7.0)],
        }
        point_major = [pair for point in zip(*references.values()) for pair in point]
        rows = [point_major[:3], point_major[3:]]
        matrix = ErrorTermSet(
            offset=0, magic=1006, ver_a=1, ver_b=1, port_ids=[1, 2],
            port_z0=[(50.0, 0.0)] * 2, freqs_ghz=[0.01, 0.02, 0.03],
            error_terms=rows,
        )
        output = StringIO()
        with redirect_stdout(output):
            print_mismatch_diagnostics(matrix, references, match_matrix_error_term_names)

        self.assertIn("point-major interleaved Re/Im: 2/2 unique matches", output.getvalue())
        self.assertEqual(matrix.term_names, {})

    def test_sign_and_reflection_checks_find_signed_and_scaled_coefficients(self):
        def pairs(values):
            return [(value.real, value.imag) for value in values]

        d1, m1, r1 = 0.4 + 0.3j, 2.0 - 1.0j, 0.017j
        d2, m2, r2 = 0.2 + 0.15j, -1.0 + 3.0j, 0.034 - 0.015j
        scale2 = (1.2 + 0.5j, 0.9 - 0.1j, 0.6 + 0.7j)
        references = {
            "Directivity(1,1)": pairs([d1] * 3),
            "SourceMatch(1,1)": pairs([m1] * 3),
            "ReflectionTracking(1,1)": pairs([r1] * 3),
            "Directivity(2,2)": pairs([d2] * 3),
            "SourceMatch(2,2)": pairs([m2] * 3),
            "ReflectionTracking(2,2)": pairs([r2] * 3),
            "CrossTalk(2,1)": [(0.0, 0.0)] * 3,
        }
        matrix = ErrorTermSet(
            offset=0, magic=1006, ver_a=1, ver_b=1, port_ids=[1, 2],
            port_z0=[(50.0, 0.0)] * 2, freqs_ghz=[0.01, 0.02, 0.03],
            error_terms=[
                pairs([-d1] * 3),
                pairs([m1] * 3),
                pairs([r1 - d1 * m1] * 3),
                pairs(scale2),
                pairs(-d2 * a for a in scale2),
                pairs(m2 * a for a in scale2),
                pairs((r2 - d2 * m2) * a for a in scale2),
                [(0.0, 0.0)] * 3,
            ],
        )
        output = StringIO()
        with redirect_stdout(output):
            print_sign_and_reflection_checks(matrix, references, match_matrix_error_term_names)

        report = output.getvalue()
        self.assertIn("Term 0 = -Directivity(1,1)", report)
        self.assertNotIn("-CrossTalk", report)
        self.assertIn("Port 1 | scale unity | -D -> [0], M -> [1], R-D*M -> [2]", report)
        self.assertIn("Port 2 | scale Term 3 | -D -> [4], M -> [5], R-D*M -> [6]", report)
        self.assertEqual(matrix.term_names, {})

    def test_grouped_check_recovers_scale_without_a_stored_scale_row(self):
        coefficients = {
            "-D": [0.25j, -0.25, 0.5j],
            "M": [0.125, 0.25j, 0.125j],
            "R-D*M": [0.5j, 0.25, 0.125],
        }
        scale = [1.25j, 2.0, 0.5j]
        rows = [[(3.0, 0.0)] * 3 for _ in range(12)]
        for key, index in [("-D", 7), ("M", 5), ("R-D*M", 10)]:
            scaled = [a * b for a, b in zip(scale, coefficients[key])]
            rows[index] = [(value.real, value.imag) for value in scaled]
        matrix = ErrorTermSet(
            offset=0, magic=1006, ver_a=1, ver_b=1, port_ids=[1, 2],
            port_z0=[(50.0, 0.0)] * 2, freqs_ghz=[0.01, 0.02, 0.03],
            error_terms=rows,
        )
        output = StringIO()
        with redirect_stdout(output):
            check_grouped_reflection_rows(matrix, 2, coefficients, match_matrix_error_term_names)
            print_coefficient_samples(matrix, {"Directivity(2,2)": [(0.25, 0.0)] * 3})

        report = output.getvalue()
        self.assertIn("Port 2 | grouped rows -D: 7, M: 5 | relative cross-product residual=0", report)
        self.assertIn("From fitted common scale: R-D*M -> [10]", report)
        self.assertIn("Point indices (0-based): 0, 1, 2", report)
        self.assertIn("Directivity(2,2): (0.25,0)  (0.25,0)  (0.25,0)", report)

    def test_grouped_matrix_reconstructs_error_terms_including_transmission_direction(self):
        ports = [1, 3]
        a = [1.0, 2.0 - 0.5j]
        e = [100.0j, 130.0 + 15.0j]
        directivity = [0.375 - 0.125j, -0.29 + 0.4j]
        source_match = [2.0 - 0.25j, 0.3 + 1.25j]
        reflection = [1e-5 - 4e-6j, 0.011 + 0.003j]
        load_match = [0.125 + 0.025j, -0.35 + 0.7j]
        b = [x * (r - d * m) for x, r, d, m in zip(a, reflection, directivity, source_match)]
        b[0] += 2e-8j  # Rounding before cancellation in the reflection calculation.
        c = [x * m for x, m in zip(a, source_match)]
        d = [-x * value for x, value in zip(a, directivity)]
        f = [x * value for x, value in zip(e, load_match)]

        def samples(value):
            return [(value.real, value.imag)] * 3

        matrix = ErrorTermSet(
            offset=0, magic=1006, ver_a=1, ver_b=1, port_ids=ports,
            port_z0=[(50.0, 0.0)] * 2, freqs_ghz=[0.01, 0.02, 0.03],
            error_terms=[samples(value) for block in (a, b, c, d, e, f) for value in block],
        )
        reference = {
            f"{name}({port},{port})": samples(value)
            for name, values in [("Directivity", directivity), ("SourceMatch", source_match),
                                 ("ReflectionTracking", reflection)]
            for port, value in zip(ports, values)
        }
        reference.update({
            "LoadMatch(1,3)": samples(load_match[0]),
            "LoadMatch(3,1)": samples(load_match[1]),
            "TransmissionTracking(1,3)": samples(a[1] * reflection[1] / e[0]),
            "TransmissionTracking(3,1)": samples(a[0] * reflection[0] / e[1]),
            "CrossTalk(1,3)": samples(0j),
            "CrossTalk(3,1)": samples(0j),
        })
        output = StringIO()
        with redirect_stdout(output):
            print_grouped_matrix_verification(matrix, reference)
        report = output.getvalue()
        self.assertIn("Grouped model matches: 10/10 predicted terms", report)
        self.assertIn("Transmission relation matches: 2/2 using simulator R", report)
        self.assertIn("Simulator CrossTalk: 2/2 traces are zero", report)

        reference["TransmissionTracking(1,3)"] = reference["TransmissionTracking(3,1)"]
        output = StringIO()
        with redirect_stdout(output):
            print_grouped_matrix_verification(matrix, reference)
        self.assertIn("TransmissionTracking(1,3): DIFFER", output.getvalue())
        self.assertIn("Transmission relation matches: 1/2 using simulator R", output.getvalue())
