"""V-NEXT-1 acceptance tests: late/duplicate usage events and billing replay.

Every fixture is synthetic.  The replay never creates an invoice, never moves
money, never writes back canonical data and never turns a replay result into a
delivery verdict.
"""
import csv
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from buyer_ledger import usage_replay as ur

PERIOD = "2026-10"
FIXED = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)


def _rule_map(**overrides):
    payload = {
        "schema": ur.RULE_MAP_SCHEMA,
        "rule_map_version": "rm-1",
        "approved_by": "buyer-ops",
        "approved_at": "2026-11-01T00:00:00Z",
        "rules": [
            {"rule_id": "r-count", "metric_code": "api_call",
             "aggregate": "count", "unit": "call", "currency": "USD",
             "unit_price": "0.01", "price_version": "p-1"},
            {"rule_id": "r-sum", "metric_code": "compute_hours",
             "aggregate": "sum", "unit": "hour", "currency": "USD",
             "unit_price": "2.50", "price_version": "p-1"},
            {"rule_id": "r-unique", "metric_code": "seat",
             "aggregate": "unique_count", "unique_field": "user_id",
             "unit": "seat", "currency": "USD",
             "unit_price": "10", "price_version": "p-1"},
        ],
    }
    payload.update(overrides)
    return payload


def _event(tid, metric="api_call", occurred="2026-10-05T10:00:00+00:00",
           ingested="2026-11-02T00:00:00+00:00", source_hash="sha256:aaa",
           source_ref="usage.csv", **props):
    return {
        "transaction_id": tid,
        "external_subject_id": "acct-1",
        "metric_code": metric,
        "occurred_at": occurred,
        "ingested_at": ingested,
        "source_ref": source_ref,
        "source_hash": source_hash,
        "properties": props,
    }


def _result(events, *, rule_map=None, period=PERIOD, **kwargs):
    return ur.replay(events, period=period, rule_map=rule_map or _rule_map(),
                     generated_at=FIXED, **kwargs)


class UsageReplayTest(unittest.TestCase):
    def test_vn1_01_duplicate_same_hash_counted_once(self):
        event = _event("t1", quantity="1")
        payload = _result([event, dict(event)])
        self.assertEqual(payload["counts"]["received"], 2)
        self.assertEqual(payload["counts"]["duplicates"], 1)
        self.assertEqual(payload["counts"]["included"], 1)
        self.assertEqual(len(payload["duplicates"]), 1)
        metrics = {m["metric_code"]: m for m in payload["metrics"]}
        self.assertEqual(metrics["api_call"]["quantity"], "1.0000")

    def test_vn1_02_conflict_same_key_different_hash_not_merged(self):
        first = _event("t1", quantity="1", seat="A")
        second = _event("t1", quantity="5", seat="B")
        payload = _result([first, second])
        self.assertEqual(payload["counts"]["conflicts"], 1)
        conflict = payload["conflicts"][0]
        self.assertEqual(conflict["state"], "conflict")
        self.assertNotEqual(conflict["properties_hash"],
                            conflict["prior_properties_hash"])
        conflicted = [line for line in payload["lines"]
                      if line["transaction_id"] == "t1"
                      and line["dedupe_state"] == "conflict"]
        self.assertEqual(len(conflicted), 1)
        self.assertFalse(conflicted[0]["included"])
        self.assertEqual(conflicted[0]["exclusion_reason"], "DEDUPE_CONFLICT")
        metrics = {m["metric_code"]: m for m in payload["metrics"]}
        self.assertEqual(metrics["api_call"]["quantity"], "1.0000")

    def test_vn1_03_occurred_and_ingested_are_separate(self):
        payload = _result([_event("t1")])
        line = payload["lines"][0]
        self.assertEqual(line["occurred_at"], "2026-10-05T10:00:00+00:00")
        self.assertEqual(line["ingested_at"], "2026-11-02T00:00:00+00:00")
        with self.assertRaises(ur.ReplayError):
            _result([_event("t2", occurred="2026-10-05T10:00:00")])
        with self.assertRaises(ur.ReplayError):
            _result([_event("t3", ingested="2026-11-02T00:00:00")])

    def test_vn1_04_late_event_new_revision_keeps_signoff(self):
        payload = _result(
            [_event("t1", occurred="2026-09-30T10:00:00+00:00")],
            closed_at="2026-11-03T00:00:00Z", previous_revision="r1")
        self.assertEqual(payload["revision"]["effect"], "new_revision")
        self.assertEqual(payload["revision"]["late_event_count"], 1)
        self.assertTrue(payload["revision"]["previous_signoff_preserved"])
        self.assertEqual(payload["revision"]["parent_revision_id"], "r1")
        self.assertEqual(payload["counts"]["late"], 1)
        late = [line for line in payload["lines"] if line["late"]]
        self.assertEqual(late[0]["transaction_id"], "t1")
        self.assertFalse(late[0]["included"])

    def test_vn1_05_period_boundary_no_double_count(self):
        inside = _event("t-in", occurred="2026-10-01T00:00:00+00:00")
        outside_next = _event("t-next", occurred="2026-11-01T00:00:00+00:00")
        outside_prev = _event("t-prev", occurred="2026-09-30T23:59:59+00:00")
        payload = _result([inside, outside_next, outside_prev])
        by_id = {line["transaction_id"]: line for line in payload["lines"]}
        self.assertTrue(by_id["t-in"]["included"])
        for key in ("t-next", "t-prev"):
            self.assertFalse(by_id[key]["included"])
            self.assertIn("PERIOD_OUT_OF_WINDOW",
                          by_id[key]["exclusion_reasons"])
        metrics = {m["metric_code"]: m for m in payload["metrics"]}
        self.assertEqual(metrics["api_call"]["quantity"], "1.0000")

    def test_vn1_06_aggregates_follow_rule_map(self):
        events = [
            _event("t1", quantity="1"),
            _event("t2", quantity="3", metric="compute_hours"),
            _event("t3", quantity="4", metric="compute_hours"),
            _event("t4", metric="unmapped_metric"),
        ]
        payload = _result(events)
        metrics = {m["metric_code"]: m for m in payload["metrics"]}
        self.assertEqual(metrics["api_call"]["quantity"], "1.0000")
        self.assertEqual(metrics["api_call"]["amount"], "0.0100")
        self.assertEqual(metrics["compute_hours"]["quantity"], "7.0000")
        self.assertEqual(metrics["compute_hours"]["amount"], "17.5000")
        self.assertNotIn("unmapped_metric", metrics)
        unmapped = [line for line in payload["lines"]
                    if line["metric_code"] == "unmapped_metric"]
        self.assertEqual(unmapped[0]["exclusion_reason"],
                         "METRIC_NOT_IN_RULE_MAP")

    def test_vn1_06b_unapproved_rule_map_only_yields_draft(self):
        rule_map = _rule_map(approved_by="", approved_at="")
        payload = _result([_event("t1", quantity="1")], rule_map=rule_map)
        self.assertFalse(payload["rule_map"]["approved"])
        self.assertEqual(payload["aggregation_state"],
                         "RULE_MAP_NOT_APPROVED")
        self.assertEqual(payload["metrics"], [])
        self.assertIn("RULE_MAP_NOT_APPROVED",
                      payload["lines"][0]["exclusion_reasons"])

    def test_vn1_07_unique_count_expandable(self):
        events = [
            _event("t1", metric="seat", user_id="u-1"),
            _event("t2", metric="seat", user_id="u-2"),
            _event("t3", metric="seat", user_id="u-1"),
        ]
        payload = _result(events)
        metric = [m for m in payload["metrics"] if m["metric_code"] == "seat"][0]
        self.assertEqual(metric["quantity"], "2.0000")
        self.assertEqual(metric["unique_values"], ["u-1", "u-2"])
        self.assertEqual(metric["amount"], "20.0000")

    def test_vn1_08_missing_price_null_amount_with_reason(self):
        rule_map = _rule_map()
        rule_map["rules"][1]["unit_price"] = ""
        payload = _result(
            [_event("t1", metric="compute_hours", quantity="3")],
            rule_map=rule_map)
        line = payload["lines"][0]
        self.assertTrue(line["included"])
        self.assertIsNone(line["amount"])
        self.assertEqual(line["amount_reason"], "PRICE_MISSING")
        metric = [m for m in payload["metrics"]
                  if m["metric_code"] == "compute_hours"][0]
        self.assertIsNone(metric["amount"])
        self.assertEqual(metric["amount_reason"], "PRICE_MISSING")

    def test_vn1_08b_price_version_missing_is_not_guessed(self):
        rule_map = _rule_map()
        del rule_map["rules"][0]["price_version"]
        payload = _result([_event("t1", quantity="1")], rule_map=rule_map)
        self.assertEqual(payload["lines"][0]["amount_reason"],
                         "PRICE_VERSION_MISSING")
        self.assertIsNone(payload["lines"][0]["amount"])

    def test_vn1_09_cross_file_idempotent(self):
        shared = _event("t1", quantity="1")
        extra = _event("t2", quantity="1")
        once = _result([shared, extra])
        twice = _result([shared, extra, dict(shared)])
        self.assertEqual(twice["counts"]["duplicates"], 1)
        self.assertEqual(once["counts"]["included"], twice["counts"]["included"])
        m1 = {m["metric_code"]: m["quantity"] for m in once["metrics"]}
        m2 = {m["metric_code"]: m["quantity"] for m in twice["metrics"]}
        self.assertEqual(m1, m2)

    def test_vn1_10_diff_reports_change_without_blame(self):
        before = _result([_event("t1", quantity="1")])
        after = _result([_event("t1", quantity="2")])
        diff = ur.diff_replays(after, before)
        self.assertEqual(len(diff["changed"]), 1)
        self.assertIn("quantity", diff["changed"][0]["fields"])
        self.assertIn("不归责", diff["note"])
        rendered = ur.render_diff(diff)
        self.assertIn("变化行：1", rendered)

    def test_vn1_11_replay_identical_delivery_unknown(self):
        payload = _result([_event("t1")])
        self.assertEqual(payload["delivery_state"], "UNKNOWN")
        self.assertIn("UNKNOWN", payload["delivery_note"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("delivery_accepted", text)
        self.assertNotIn("verified", text)

    def test_vn1_12_repeat_replay_same_revision(self):
        events = [_event("t1")]
        first = _result(events)
        second = _result(events)
        self.assertEqual(first["input_fingerprint"],
                         second["input_fingerprint"])
        self.assertEqual(first["revision"]["revision_id"],
                         second["revision"]["revision_id"])
        self.assertEqual(second["revision"]["effect"], "no_change")
        self.assertEqual(second["counts"], first["counts"])

    def test_vn1_13_csv_formula_injection_escaped(self):
        event = _event("t1", source_ref="=HYPERLINK(\"http://evil\")")
        payload = _result([event])
        with tempfile.TemporaryDirectory() as tmp:
            paths = ur.write_replay(payload, Path(tmp))
            with open(paths["csv"], encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["source_ref"],
                         "'=HYPERLINK(\"http://evil\")")

    def test_vn1_14_zero_invoice_zero_payment(self):
        payload = _result([_event("t1")])
        text = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("invoice_id", "payment_id", "paid_at",
                          "charge_created"):
            self.assertNotIn(forbidden, text)
        self.assertIn("不生成 invoice", payload["boundary"])

    def test_xn2_02_replay_does_not_touch_input_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / "events.jsonl"
            events_path.write_text(
                json.dumps(_event("t1")) + "\n", encoding="utf-8")
            rule_path = root / "rules.json"
            rule_path.write_text(json.dumps(_rule_map()), encoding="utf-8")
            before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.iterdir() if path.is_file()
            }
            events = ur.load_events(events_path)
            rule_map = ur.load_rule_map(rule_path)
            payload = ur.replay(events, period=PERIOD, rule_map=rule_map,
                                generated_at=FIXED,
                                source_files=[events_path])
            ur.write_replay(payload, root / "out")
            after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.iterdir() if path.is_file()
            }
            self.assertEqual(before, after)

    def test_xn2_04_source_traceability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / "events.json"
            events_path.write_text(
                json.dumps({"events": [_event("t1")]}), encoding="utf-8")
            rule_path = root / "rules.json"
            rule_path.write_text(json.dumps(_rule_map()), encoding="utf-8")
            payload = ur.replay(ur.load_events(events_path), period=PERIOD,
                                rule_map=ur.load_rule_map(rule_path),
                                generated_at=FIXED,
                                source_files=[events_path, rule_path])
        self.assertEqual(payload["source_files"][0], str(events_path))
        self.assertTrue(payload["rule_map"]["hash"].startswith("sha256:"))
        self.assertTrue(payload["input_fingerprint"].startswith("sha256:"))
        self.assertEqual(payload["lines"][0]["source_ref"], "usage.csv")
        self.assertEqual(payload["lines"][0]["source_hash"], "sha256:aaa")

    def test_xn2_10_deterministic_replay(self):
        events = [_event("t1", quantity="2")]
        one = _result(events)
        two = _result(events)
        self.assertEqual(one, two)
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(ur.write_replay(one, Path(tmp) / "a")["json"])
            second = Path(ur.write_replay(two, Path(tmp) / "b")["json"])
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_xn2_11_float_money_refused(self):
        rule_map = _rule_map()
        rule_map["rules"][0]["unit_price"] = 0.01
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(rule_map), encoding="utf-8")
            with self.assertRaises(ur.ReplayError) as caught:
                ur.load_rule_map(path)
        self.assertIn("float", str(caught.exception))

    def test_xn2_12_missing_input_fails_clearly(self):
        with self.assertRaises(ur.ReplayError):
            ur.load_events(Path("/nonexistent/events.jsonl"))
        with self.assertRaises(ur.ReplayError):
            ur.replay([{"transaction_id": "t1"}], period=PERIOD,
                      rule_map=_rule_map())

    def test_cli_usage_replay_smoke(self):
        import contextlib
        from buyer_ledger import cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rule_path = root / "rules.json"
            rule_path.write_text(json.dumps(_rule_map()), encoding="utf-8")
            events_path = root / "events.jsonl"
            events_path.write_text(json.dumps(_event("t1", quantity="1")) + "\n",
                                   encoding="utf-8")
            out_dir = root / "out"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli.main(["usage", "replay", "--input", str(events_path),
                          "--period", PERIOD, "--rule-map", str(rule_path),
                          "--out", str(out_dir)])
            text = buffer.getvalue()
            self.assertIn("用量重放", text)
            self.assertIn("Delivery：UNKNOWN", text)
            self.assertTrue((out_dir / "usage-replay.json").is_file())
            payload = json.loads(
                (out_dir / "usage-replay.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], ur.SCHEMA)


if __name__ == "__main__":
    unittest.main()
