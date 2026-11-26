"""Regression tests for PNA calibration record layouts."""

import struct
import unittest
from uuid import UUID

from parse_pna_cal import (
    BinaryReader,
    encode_cstringw,
    find_and_parse_section5_cal_settings,
    find_and_parse_section7_error_terms,
    parse_section1_header,
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
