"""Tests for a CalSet probe on test copies and its offline matrix comparison."""

import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

from parse_pna_cal import ErrorTermSet

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "tools"), *sys.path]):
    import probe_cal_matrix as probe


class MatrixProbeTests(unittest.TestCase):
    def test_prepare_writes_only_to_the_marked_copy_and_reads_it_back(self):
        instrument = Mock()
        instrument.query.side_effect = {
            ':CSET:ETER:CAT? "Original"': '"Directivity(1,1),Directivity(2,2)"',
            ':CSET:EXIS? "ProbeBase_1234567890"': "0",
            ':CSET:EXIS? "ProbeEdit_1234567890"': "+0",
            ":FORM:DATA?": "REAL,64",
            "*OPC?": "1",
        }.__getitem__
        original = b"0,0,0,0,0,0,0,0"
        instrument.read_raw.side_effect = [
            original, original, original, b"0,0,0.013,-0.021,-0.034,0.017,0.027,0.039",
        ]

        with patch.object(probe, "uuid4", return_value=UUID("12345678-9000-0000-0000-000000000000")):
            manifest = probe.prepare_probe(instrument, "Original", "Directivity(1,1)")

        writes = [entry.args[0] for entry in instrument.write.call_args_list]
        self.assertIn(':CSET:COPY "Original","ProbeBase_1234567890"', writes)
        self.assertIn(':CSET:COPY "Original","ProbeEdit_1234567890"', writes)
        changes = [command for command in writes if command.startswith(":CSET:ETER:DATA ")]
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].startswith(':CSET:ETER:DATA "ProbeEdit_1234567890","Directivity(1,1)",'))
        self.assertEqual(manifest["markers"][0], [1, 0.013, -0.021])
        self.assertEqual(writes[-1], ":FORM:DATA REAL,64")

    def test_compare_requires_one_row_with_the_expected_deltas(self):
        baseline = ErrorTermSet(
            offset=0, magic=1006, ver_a=1, ver_b=1, port_ids=[1, 2],
            port_z0=[(50.0, 0.0)] * 2, freqs_ghz=[0.01, 0.02, 0.03, 0.04],
            error_terms=[[(0.0, 0.0)] * 4, [(0.0, 0.0)] * 4],
        )
        markers = [(0, 0.013, -0.021), (2, -0.034, 0.017), (3, 0.027, 0.039)]
        manifest = {"term": "Directivity(1,1)", "points": 4, "markers": markers}
        marked = list(baseline.error_terms[1])
        for point, real, imag in markers:
            marked[point] = (real, imag)

        one_row = replace(baseline, offset=40, error_terms=[baseline.error_terms[0], marked])
        output = StringIO()
        with redirect_stdout(output):
            probe.compare_probe(baseline, one_row, manifest, baseline)
        self.assertIn("Baseline matrix: identical to the original .cal", output.getvalue())
        self.assertIn("Direct storage match: Term 1 = Directivity(1,1)", output.getvalue())

        two_rows = replace(baseline, error_terms=[marked, marked])
        output = StringIO()
        with redirect_stdout(output):
            probe.compare_probe(baseline, two_rows, manifest)
        self.assertIn("Changed rows: [0, 1]", output.getvalue())
        self.assertNotIn("Direct storage match:", output.getvalue())
        with self.assertRaisesRegex(ValueError, "baseline matrix differs"):
            probe.compare_probe(baseline, two_rows, manifest, original=one_row)
