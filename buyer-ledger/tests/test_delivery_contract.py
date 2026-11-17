"""P0.1 delivery contract (Verovo / buyer-ledger share).

The machine-readable contract in docs/delivery-contract.json is what a user
meets on first interaction (`--help`, `contract show`).  These tests keep the
contract, the CLI and the implementation honest with each other:

- `contract show` prints exactly the file on disk (no drift between the two);
- every intent's entry command really exists in the parser;
- the terminal-state vocabulary matches bill_candidate.TERMINAL_STATES and
  keeps UNPROVABLE out of it (§3.1: UNPROVABLE is a verification conclusion);
- the §5 procurement template carries the frozen fields and never a
  transaction;
- the exclusions still name the payment red line.
"""
import contextlib
import io
import json
import unittest
from pathlib import Path

from buyer_ledger import bill_candidate as bc
from buyer_ledger import cli

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "docs" / "delivery-contract.json"


class DeliveryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_contract_show_prints_the_file_verbatim(self):
        code, out = self._run(["contract", "show"])
        self.assertEqual(0, code)
        self.assertEqual(json.loads(CONTRACT_PATH.read_text(encoding="utf-8")),
                         json.loads(out))

    def test_first_interaction_help_mentions_the_golden_chain_entries(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--help"])
        self.assertEqual(0, ctx.exception.code)
        text = out.getvalue()
        for token in self.contract["first_interaction"]["help_mentions"]:
            self.assertIn(token, text)

    def test_every_intent_entry_exists_in_the_parser(self):
        parser_help = io.StringIO()
        with contextlib.redirect_stdout(parser_help):
            try:
                cli.build_parser().parse_args(["--help"])
            except SystemExit:
                pass
        text = parser_help.getvalue()
        for intent in self.contract["supported_intents"]:
            entry = intent["entry"].split()[0]
            self.assertIn(entry, text,
                          "contract intent %s has no CLI command" % intent["id"])

    def test_terminal_state_vocabulary_matches_implementation(self):
        stated = [row["state"] for row in self.contract["delivery_terminal_states"]]
        self.assertEqual(sorted(bc.TERMINAL_STATES), sorted(stated))
        self.assertEqual(("DELIVERED", "DELIVERED_WITH_GAP", "BLOCKED_USER_INPUT",
                          "UNSUPPORTED", "FAILED_UNRESOLVED"),
                         bc.TERMINAL_STATES)
        # every terminal state an intent may reach is in the frozen vocabulary
        for intent in self.contract["supported_intents"]:
            for state in intent["terminal_states"]:
                self.assertIn(state, bc.TERMINAL_STATES)

    def test_unprovable_stays_a_verification_conclusion(self):
        conclusions = self.contract["verification_conclusions"]
        self.assertEqual(["PASS", "FAIL", "UNPROVABLE"], conclusions["values"])
        terminal_words = {row["state"]
                          for row in self.contract["delivery_terminal_states"]}
        self.assertNotIn("UNPROVABLE", terminal_words)
        self.assertNotIn("UNPROVABLE", bc.TERMINAL_STATES)

    def test_procurement_template_is_the_frozen_section_5_shape(self):
        template = self.contract["requires_procurement"]["template"]
        payload = bc.requires_procurement_payload(
            template["item"], template["why"], template["user_action"],
            template["alternatives"])
        for key in ("terminal_state", "reason", "item", "why", "user_action",
                    "alternatives", "transaction_executed"):
            self.assertEqual(template[key], payload[key])
        self.assertEqual("BLOCKED_USER_INPUT", payload["terminal_state"])
        self.assertEqual("requires_procurement", payload["reason"])
        self.assertFalse(payload["transaction_executed"])
        # the module's paid-engine branch uses exactly this shape
        result = bc.add_candidate(pathlib_store(), engine="paid-cloud-ocr",
                                  pasted_text="x")
        self.assertIsNone(result["candidate"])
        for key in ("terminal_state", "reason", "item", "why", "user_action",
                    "alternatives", "transaction_executed"):
            self.assertIn(key, result["procurement"])

    def test_exclusions_keep_the_payment_red_line(self):
        joined = " ".join(self.contract["exclusions"])
        self.assertIn("付款", joined)
        self.assertIn("采购", joined)
        self.assertIn("自动入账", joined)


def pathlib_store():
    import tempfile
    temp = tempfile.TemporaryDirectory()
    store = Path(temp.name) / "bill-candidates"
    # cleanup happens after add_candidate already refused to write anything
    temp.cleanup()
    return store


if __name__ == "__main__":
    unittest.main()
