import unittest
from pathlib import Path
from collector_07.sources import FixtureTransport
from collector_07.adapter import decode


class PolicySourceTests(unittest.TestCase):
    def test_http_unchanged_and_unavailable_are_distinct(self):
        transport = FixtureTransport(Path(__file__).resolve().parents[1] / "fixtures")
        self.assertEqual(transport.request("/rule/cached").status, 304)
        self.assertEqual(transport.request("/rule/unavailable").status, 503)

    def test_document_times_preserved(self):
        root = Path(__file__).resolve().parents[1] / "fixtures"
        row = decode((root / "v2.json").read_bytes())[0]
        self.assertGreater(row["published_at"], row["effective_at"])

    def test_source_requires_object_array(self):
        with self.assertRaises(ValueError):
            decode(b'{"rule":"A"}')
