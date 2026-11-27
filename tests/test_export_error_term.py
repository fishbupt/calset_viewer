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

from parse_pna_cal import encode_cstringw

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "export_error_term.py"
with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]):
    from export_error_term import main


class VerifyCalTests(unittest.TestCase):
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
        self.assertIn("rows=12 | points=3", report)
        self.assertIn("Term 2: Directivity(1,1)", report)
        self.assertIn("Term 0: ambiguous (Isolation(2,1))", report)
        self.assertIn("Term 1: ambiguous (Isolation(2,1))", report)
        self.assertIn("SourceMatch(1,1): skipped (frequency mismatch at point 2", report)
        self.assertIn("LoadMatch(2,1): no matching row", report)
        self.assertIn("ReflectionTracking(2,2): skipped (trace point count=2", report)
        self.assertIn("Unique matches: 1/12 rows | verified traces: 3/5", report)
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
