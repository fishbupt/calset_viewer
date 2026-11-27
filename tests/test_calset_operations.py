"""Regression coverage for fixture reference-plane transforms and CalSet comparison."""

import tempfile
import unittest
from pathlib import Path

from calset_operations import (
    THRU,
    Fixture,
    compare_terms,
    deembed_calset,
    fixture_samples,
    read_calset_document,
    read_fixture,
    write_calset_document,
)
from parse_pna_cal import CalSetHeader, ComplexTrace, ErrorTermSet, ParsedCalFile


FREQUENCIES = (1e9, 2e9)
LEFT = (0.14 + 0.08j, 0.7 - 0.22j, 0.62 - 0.3j, -0.18 + 0.03j)
RIGHT = (-0.13 + 0.06j, 0.82 + 0.11j, 0.69 + 0.15j, 0.08 + 0.1j)
DUT = (0.34 + 0.1j, 0.52 - 0.13j, 0.43 + 0.19j, -0.23 + 0.21j)


def sample_calset():
    ports = [2, 4]
    values = {}
    for source, receiver, coeffs in (
        (2, 4, (0.02 + 0.03j, 0.24 - 0.11j, 0.84 - 0.04j, 0.19 + 0.14j, 0.74 + 0.06j, 0.001j)),
        (4, 2, (-0.01j, -0.14 + 0.08j, 0.88 + 0.12j, 0.08 - 0.23j, 0.76 - 0.12j, -0.002j)),
    ):
        keys = (
            f"Directivity({source},{source})", f"SourceMatch({source},{source})",
            f"ReflectionTracking({source},{source})", f"LoadMatch({receiver},{source})",
            f"TransmissionTracking({receiver},{source})", f"CrossTalk({receiver},{source})",
        )
        values.update(zip(keys, coeffs))
    traces = [trace(key, value) for key, value in values.items()]
    return ParsedCalFile(
        file_size=0,
        header=CalSetHeader("PNA Cal File", 1, 3, 1, "CalSet", "guid", "", 0, 0, 0, 0),
        raw_measurements=[],
        error_term_set=ErrorTermSet(0, 1006, 1, 1, ports, [(50., 0.)] * 2, [1., 2.], []),
        reconstructed_error_terms=traces,
    ), values


def trace(name, value, frequencies=FREQUENCIES):
    return ComplexTrace(name, 2, 4, 1, [(value.real, value.imag)] * 2, frequencies)


def cascade(left, right):
    """Connect left port 2 to right port 1 in Touchstone port order."""
    l11, l21, l12, l22 = left
    r11, r21, r12, r22 = right
    junction = 1 - l22 * r11
    return (
        l11 + l12 * r11 * l21 / junction,
        r21 * l21 / junction,
        l12 * r12 / junction,
        r22 + r21 * l22 * r12 / junction,
    )


def uncorrected(network, coefficients, source, receiver):
    """Apply the directional VNA error model to a DUT in source-first order."""
    s11, s21, s12, s22 = network
    d = coefficients[f"Directivity({source},{source})"]
    m = coefficients[f"SourceMatch({source},{source})"]
    r = coefficients[f"ReflectionTracking({source},{source})"]
    l = coefficients[f"LoadMatch({receiver},{source})"]
    t = coefficients[f"TransmissionTracking({receiver},{source})"]
    x = coefficients[f"CrossTalk({receiver},{source})"]
    den = (1 - m * s11) * (1 - l * s22) - m * l * s12 * s21
    return d + r * (s11 * (1 - l * s22) + s12 * l * s21) / den, x + t * s21 / den


class CalSetOperationsTests(unittest.TestCase):
    def test_fixture_transform_matches_cascaded_network_in_both_directions(self):
        parsed, old = sample_calset()
        for left, right in ((LEFT, THRU), (THRU, RIGHT), (LEFT, RIGHT)):
            with self.subTest(left=left, right=right):
                fixtures = {
                    port: Fixture(Path(f"{port}.s2p"), FREQUENCIES, 50., (s,) * 2, False)
                    for port, s in ((2, left), (4, right)) if s != THRU
                }
                document = deembed_calset(parsed, Path("original.cal"), fixtures)
                new = {key: complex(*points[0]) for key, points in document["terms"].items()}
                externally_measured = cascade(cascade(left, DUT), right[::-1])
                for ext, dut, src, recv in (
                    (externally_measured, DUT, 2, 4), (externally_measured[::-1], DUT[::-1], 4, 2),
                ):
                    measured_original = uncorrected(ext, old, src, recv)
                    measured_combined = uncorrected(dut, new, src, recv)
                    for original, combined in zip(measured_original, measured_combined):
                        self.assertAlmostEqual(abs(original - combined), 0.0, places=12)

    def test_roundtrip_preserves_axis_and_traces_and_compares_by_name(self):
        parsed, original = sample_calset()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "combined.calset.json"
            thru = Fixture(Path("thru.s2p"), FREQUENCIES, 50., (THRU,) * 2, False)
            write_calset_document(output, deembed_calset(parsed, Path("original.cal"), {2: thru}))
            reopened = read_calset_document(output)
        self.assertEqual(reopened.metadata["校准端口"], [2, 4])
        self.assertEqual(reopened.metadata["去嵌来源"]["source_file"], "original.cal")
        rows = compare_terms(reopened.reconstructed_error_terms, parsed.reconstructed_error_terms[::-1])
        self.assertEqual(len(rows), len(original))
        self.assertTrue(all(row.max_error == 0.0 and row.frequencies_hz == FREQUENCIES for row in rows))

    def test_s2p_port_reversal_and_complex_linear_interpolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.s2p"
            path.write_text(
                "# GHz S RI R 50\n1 0.1 0 0.6 0 0.4 0 0.2 0\n"
                "3 0.2 0 0.8 0 0.6 0 0.3 0 ! comment\n",
                encoding="ascii",
            )
            fixture = read_fixture(path, reversed_ports=True)
        sample = fixture_samples(fixture, (2e9,), 50)[0]
        for actual, expected in zip(sample, (0.25, 0.5, 0.7, 0.15)):
            self.assertAlmostEqual(actual, expected)
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            fixture_samples(fixture, (4e9,), 50)
        with self.assertRaisesRegex(ValueError, "参考阻抗"):
            fixture_samples(fixture, FREQUENCIES, 75)

    def test_compare_marks_missing_terms_and_frequency_mismatch(self):
        result = compare_terms(
            [trace("Directivity(1,1)", 1j), trace("SourceMatch(1,1)", 0j)],
            [trace("directivity(1,1)", -1j), trace("SourceMatch(1,1)", 0j, (1e9, 3e9)),
             trace("CrossTalk(2,1)", 0j)],
        )
        indexed = {row.name: row for row in result}
        directivity = indexed["Directivity(1,1)"]
        self.assertEqual(directivity.residuals, [(0., 2.)] * 2)
        self.assertEqual(directivity.max_error, 2)
        self.assertEqual(directivity.rms_error, 2)
        self.assertEqual(directivity.peak_frequency_hz, 1e9)
        self.assertEqual(indexed["SourceMatch(1,1)"].status, "频率轴不一致")
        self.assertEqual(indexed["CrossTalk(2,1)"].status, "仅参考 CalSet 有")


if __name__ == "__main__":
    unittest.main()
