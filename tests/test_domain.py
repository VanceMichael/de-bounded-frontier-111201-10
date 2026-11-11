import json
from pathlib import Path
import unittest

from collector.domain import *


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(Path('fixtures/sample.json').read_text())

    def test_domain_behavior(self):
        self.assertEqual(len(collect(self.payload)), 2)
        self.assertFalse(permitted('User-agent: *\nDisallow: /private', 'https://museum.example/private/a'))
        self.assertTrue(permitted('User-agent: *\nAllow: /', 'https://museum.example/a'))

    def test_deterministic_observations(self):
        self.assertEqual(collect(self.payload), collect(self.payload))
