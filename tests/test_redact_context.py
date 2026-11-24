import unittest

from waves.redact import redact


class TestContextRedaction(unittest.TestCase):
    def test_contextual_identifiers_and_preserved_measurements(self):
        note = ("Spoke with Edna about her husband’s meds. Cole goes by “Bee” on the unit. "
                "Marin Fox received the handoff. From 27 Palmer Mews, born the fourth of April, ’39 "
                "and now ninety-two. SS suffix 3498. Band JX-03\n19-T. "
                "Reach the home line at 212 555 0172 ext 23. Monitor RX/81243/K. "
                "Parking slip shows ZY-0728-E. Ground transfer took 32 minutes.")
        result, tags = redact(note)
        self.assertTrue(result.endswith("Ground transfer took 32 minutes."))
        for token in ["Edna", "Cole", "Bee", "Marin", "Fox", "Palmer", "April", "ninety", "3498",
                      "19-T", "0172", "ext 23", "81243", "0728"]:
            self.assertNotIn(token, result)
        self.assertEqual(tags["NAME"], 4)
