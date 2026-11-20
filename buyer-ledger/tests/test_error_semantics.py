"""The shared error families must cover every frozen C-01 close code.

The families are a cross-product vocabulary; they do not change any Verovo
verdict or booking status. These tests freeze the mapping so a new close code
cannot ship without a family, and an unknown code cannot silently look like a
business refusal with a specific meaning.
"""
import unittest

from buyer_ledger.close import ERROR_CODES
from buyer_ledger.error_semantics import (
    CODE_TO_FAMILY,
    FAMILIES,
    describe,
    semantic_code,
)


class ErrorSemanticsTest(unittest.TestCase):
    def test_every_close_code_has_a_family(self):
        self.assertEqual(set(CODE_TO_FAMILY), set(ERROR_CODES))
        for code in ERROR_CODES:
            self.assertIn(CODE_TO_FAMILY[code], FAMILIES)

    def test_families_are_the_shared_seven(self):
        self.assertEqual(
            FAMILIES,
            (
                "NEEDS_USER_INPUT",
                "UNSUPPORTED_CAPABILITY",
                "INVALID_INPUT",
                "PERMISSION_DENIED",
                "SAFETY_BLOCKED",
                "STALE_OR_CONFLICT",
                "CONTRACT_ERROR",
            ),
        )

    def test_unknown_code_falls_back_to_contract_error(self):
        self.assertEqual(semantic_code("NOT_A_CLOSE_CODE"), "CONTRACT_ERROR")
        described = describe("NOT_A_CLOSE_CODE")
        self.assertEqual(described["family"], "CONTRACT_ERROR")
        self.assertTrue(described["next_action"])

    def test_spot_check_families(self):
        self.assertEqual(semantic_code("INPUT_REQUIRED"), "NEEDS_USER_INPUT")
        self.assertEqual(semantic_code("REVIEW_REQUIRED"), "NEEDS_USER_INPUT")
        self.assertEqual(semantic_code("POLICY_DENIED"), "PERMISSION_DENIED")
        self.assertEqual(semantic_code("UNSUPPORTED_CAPABILITY"), "UNSUPPORTED_CAPABILITY")
        self.assertEqual(semantic_code("STALE_SOURCE"), "STALE_OR_CONFLICT")
        self.assertEqual(
            semantic_code("INDETERMINATE_EXTERNAL_ACTION"), "STALE_OR_CONFLICT"
        )
        self.assertEqual(semantic_code("RATE_LIMITED"), "SAFETY_BLOCKED")
        self.assertEqual(semantic_code("INTERNAL_ERROR"), "CONTRACT_ERROR")

    def test_every_code_describes_the_four_user_parts(self):
        for code in ERROR_CODES:
            described = describe(code)
            self.assertEqual(described["code"], code)
            self.assertIn(described["family"], FAMILIES)
            for key in ("missing", "impact", "next_action"):
                self.assertTrue(described[key].strip(), "%s lacks %s" % (code, key))

    def test_stale_source_points_at_review_not_retry(self):
        described = describe("STALE_SOURCE")
        self.assertIn("re-review", described["next_action"])
        self.assertIn("cannot be booked", described["impact"])

    def test_indeterminate_external_action_never_says_retry_blindly(self):
        described = describe("INDETERMINATE_EXTERNAL_ACTION")
        self.assertIn("check the external side", described["next_action"])


if __name__ == "__main__":
    unittest.main()
