import hashlib
import unittest
from datetime import datetime

from trainsolve import verify


class VerificationCodeTest(unittest.TestCase):
    def test_string_format(self):
        stamp = datetime(2026, 10, 20, 19, 42, 33)
        text = verify.verification_string("abc123", stamp)
        self.assertEqual(text, "2026-10-20 19:42 abc123")

    def test_zero_padded_fields(self):
        stamp = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(verify.verification_string("s", stamp), "2026-01-02 03:04 s")

    def test_matches_md5_of_formula(self):
        stamp = datetime(2026, 10, 20, 19, 42, 0)
        expected = hashlib.md5(b"2026-10-20 19:42 70e6e59f5666458baa30fa548ceecd11").hexdigest()
        self.assertEqual(verify.verification_code("70e6e59f5666458baa30fa548ceecd11", stamp), expected)

    def test_minute_granularity(self):
        session = "s1"
        first = verify.verification_code(session, datetime(2026, 10, 20, 19, 42, 1))
        second = verify.verification_code(session, datetime(2026, 10, 20, 19, 42, 59))
        self.assertEqual(first, second)

    def test_candidate_codes_cover_offsets(self):
        codes = list(verify.candidate_codes("s1", datetime(2026, 10, 20, 12, 0), offsets_hours=2))
        self.assertEqual(len(codes), 1 + 2 * 2)
        self.assertEqual(codes[0][0], 0)
        self.assertEqual([offset for offset, _ in codes], [0, 1, -1, 2, -2])


if __name__ == "__main__":
    unittest.main()
