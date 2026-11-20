"""V-NEXT-3 acceptance dataset, evaluation replay and calibration tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from buyer_ledger import acceptance_dataset as ad


T0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 19, 8, 5, 0, tzinfo=timezone.utc)


def _criteria(version="1"):
    return {
        "criteria_id": "bc-support",
        "criteria_version": version,
        "checks": [
            {"check_id": "first-response", "description": "72h 内首响"},
            {"check_id": "resolution", "description": "问题闭环"},
        ],
    }


def _samples():
    return [
        {
            "sample_id": "s1",
            "input_ref": "in/s1.json",
            "input_hash": "sha256:" + "1" * 64,
            "buyer_criteria_ref": "bc-support",
            "criteria_version": "1",
            "trace_ref": "traces/s1.json",
            "artifact_refs": ["reports/s1.md"],
            "note": "首响 12h",
        },
        {
            "sample_id": "s2",
            "input_ref": "in/s2.json",
            "input_hash": "sha256:" + "2" * 64,
            "buyer_criteria_ref": "bc-support",
            "criteria_version": "1",
            "trace_ref": "traces/s2.json",
            "artifact_refs": ["reports/s2.md"],
            "note": "转派两次",
        },
    ]


def _dataset(**kwargs):
    return ad.build_dataset(
        kwargs.pop("samples", _samples()),
        dataset_id="ds-1",
        dataset_version="1",
        criteria=kwargs.pop("criteria", _criteria()),
        generated_at=kwargs.pop("generated_at", T0),
        **kwargs)


def _scores(pass_s1=True):
    return [
        {"sample_id": "s1", "auto_label": "PASS" if pass_s1 else "FAIL",
         "score_kind": "pass_fail"},
        {"sample_id": "s2", "auto_label": "PASS", "score_kind": "numeric",
         "auto_score": "0.75"},
    ]


def _label(sample_id, label, reviewer="alice", reviewed_at="2026-11-19T09:00:00+00:00"):
    return {"sample_id": sample_id, "label": label, "reviewer": reviewer,
            "reviewed_at": reviewed_at}


class AcceptanceDatasetTest(unittest.TestCase):
    # VN3-01
    def test_vn3_01_two_service_versions_kept(self):
        dataset = _dataset()
        run_v1 = ad.evaluate(dataset, service_version="svc-1",
                             evaluator_version="eval-1",
                             scores=_scores(), evaluated_at=T0)
        run_v2 = ad.evaluate(dataset, service_version="svc-2",
                             evaluator_version="eval-1",
                             scores=_scores(), evaluated_at=T1)
        self.assertEqual(run_v1["service_version"], "svc-1")
        self.assertEqual(run_v2["service_version"], "svc-2")
        self.assertNotEqual(run_v1["run_id"], run_v2["run_id"])
        self.assertEqual(len(run_v1["results"]), 2)
        self.assertEqual(len(run_v2["results"]), 2)
        self.assertEqual(run_v1["dataset"]["dataset_hash"],
                         run_v2["dataset"]["dataset_hash"])

    # VN3-02
    def test_vn3_02_dataset_change_requires_new_version(self):
        first = _dataset(generated_at=T0)
        same = _dataset(generated_at=T1)
        self.assertEqual(first["dataset_hash"], same["dataset_hash"])
        changed = _samples()
        changed[0]["note"] = "首响 30h，超出标准"
        with self.assertRaises(ad.EvaluationError) as caught:
            ad.build_dataset(changed, dataset_id="ds-1", dataset_version="1",
                             criteria=_criteria(), previous=first,
                             generated_at=T1)
        self.assertEqual(caught.exception.code, "DATASET_VERSION_CONFLICT")
        upgraded = ad.build_dataset(changed, dataset_id="ds-1",
                                    dataset_version="2", criteria=_criteria(),
                                    previous=first, generated_at=T1)
        self.assertNotEqual(upgraded["dataset_hash"], first["dataset_hash"])
        self.assertEqual(upgraded["dataset_version"], "2")

    # VN3-03
    def test_vn3_03_evaluator_change_requires_new_version(self):
        dataset = _dataset()
        base = ad.evaluate(dataset, service_version="svc-1",
                           evaluator_version="eval-1", scores=_scores(),
                           evaluated_at=T0)
        with self.assertRaises(ad.EvaluationError) as caught:
            ad.evaluate(dataset, service_version="svc-1",
                        evaluator_version="eval-1", scores=_scores(False),
                        evaluated_at=T1, previous=base)
        self.assertEqual(caught.exception.code, "EVALUATOR_VERSION_CONFLICT")
        bumped = ad.evaluate(dataset, service_version="svc-1",
                             evaluator_version="eval-2", scores=_scores(False),
                             evaluated_at=T1, previous=base)
        self.assertEqual(bumped["evaluator_version"], "eval-2")
        self.assertNotEqual(bumped["input_fingerprint"],
                            base["input_fingerprint"])

    # VN3-04
    def test_vn3_04_auto_pass_human_fail_disagreement(self):
        run = ad.evaluate(_dataset(), service_version="svc-1",
                          evaluator_version="eval-1", scores=_scores(),
                          human_labels=[_label("s1", "FAIL")], evaluated_at=T0)
        result = {item["sample_id"]: item for item in run["results"]}["s1"]
        self.assertEqual(result["auto_label"], "PASS")
        self.assertEqual(result["human_label"], "FAIL")
        self.assertEqual(result["agreement"], "disagree")
        self.assertEqual(result["buyer_verified"], "UNKNOWN")
        self.assertEqual([item["reason"] for item in run["review_queue"]],
                         ["AUTO_HUMAN_DISAGREEMENT"])

    # VN3-05
    def test_vn3_05_unlabelled_excluded_from_denominator(self):
        run = ad.evaluate(_dataset(), service_version="svc-1",
                          evaluator_version="eval-1", scores=_scores(),
                          human_labels=[_label("s1", "PASS")], evaluated_at=T0)
        calibration = run["calibration"]
        self.assertEqual(calibration["samples_total"], 2)
        self.assertEqual(calibration["labelled"], 1)
        self.assertEqual(calibration["unlabelled"], 1)
        self.assertEqual(calibration["agreement_denominator"], 1)
        self.assertEqual(calibration["agreement_rate"], "1.0000")
        self.assertEqual(run["summary"]["unlabelled"], 1)

    # VN3-06
    def test_vn3_06_two_humans_disagree_to_adjudication(self):
        dataset = _dataset()
        run = ad.evaluate(
            dataset, service_version="svc-1", evaluator_version="eval-1",
            scores=_scores(),
            human_labels=[_label("s1", "PASS", "alice"),
                          _label("s1", "FAIL", "bob")],
            evaluated_at=T0)
        result = {item["sample_id"]: item for item in run["results"]}["s1"]
        self.assertIsNone(result["human_label"])
        self.assertEqual(result["disagreement_state"], "disagreement")
        self.assertEqual(run["review_queue"][0]["reason"], "HUMAN_DISAGREEMENT")

        adjudicated = ad.evaluate(
            dataset, service_version="svc-1", evaluator_version="eval-1",
            scores=_scores(),
            human_labels=[_label("s1", "PASS", "alice"),
                          _label("s1", "FAIL", "bob")],
            adjudications=[{"sample_id": "s1", "label": "FAIL",
                            "adjudicator": "carol",
                            "adjudicated_at": "2026-11-19T10:00:00+00:00"}],
            evaluated_at=T1)
        result = {item["sample_id"]: item for item in adjudicated["results"]}["s1"]
        self.assertEqual(result["disagreement_state"], "adjudicated")
        self.assertEqual(result["human_label"], "FAIL")
        self.assertEqual(result["adjudication"]["adjudicator"], "carol")

        with self.assertRaises(ad.EvaluationError) as caught:
            ad.evaluate(
                dataset, service_version="svc-1", evaluator_version="eval-1",
                scores=_scores(),
                human_labels=[_label("s1", "PASS", "alice"),
                              _label("s1", "FAIL", "bob")],
                adjudications=[{"sample_id": "s1", "label": "FAIL",
                                "adjudicator": "alice",
                                "adjudicated_at": "2026-11-19T10:00:00+00:00"}],
                evaluated_at=T1)
        self.assertEqual(caught.exception.code, "ADJUDICATOR_CONFLICT")

    # VN3-07
    def test_vn3_07_missing_criteria_unknown(self):
        dataset = ad.build_dataset(_samples(), dataset_id="ds-1",
                                   dataset_version="1", generated_at=T0)
        self.assertIsNone(dataset["criteria"])
        for sample in dataset["samples"]:
            self.assertEqual(sample["criteria_state"], "CRITERIA_MISSING")
        run = ad.evaluate(dataset, service_version="svc-1",
                          evaluator_version="eval-1", scores=_scores(),
                          evaluated_at=T0)
        for result in run["results"]:
            self.assertEqual(result["score_state"], "UNKNOWN")
            self.assertEqual(result["score_reason"], "CRITERIA_MISSING")
            self.assertIsNone(result["auto_label"])
        self.assertEqual(dataset["counts"]["criteria_missing"], 2)

    # VN3-08
    def test_vn3_08_broken_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = ad.build_dataset(_samples(), dataset_id="ds-1",
                                       dataset_version="1", criteria=_criteria(),
                                       reference_root=Path(tmp),
                                       generated_at=T0)
            for sample in dataset["samples"]:
                self.assertEqual(sample["reference_state"], "BROKEN_REFERENCE")
            self.assertEqual(dataset["counts"]["broken_references"], 2)
            run = ad.evaluate(dataset, service_version="svc-1",
                              evaluator_version="eval-1", scores=_scores(),
                              evaluated_at=T0)
            result = run["results"][0]
            self.assertEqual(result["score_state"], "UNKNOWN")
            self.assertEqual(result["score_reason"], "BROKEN_REFERENCE")
            self.assertIsNone(result["auto_label"])
            self.assertEqual(run["review_queue"][0]["reason"],
                             "BROKEN_REFERENCE")

    # VN3-09
    def test_vn3_09_repeated_evaluation_replayable(self):
        dataset = _dataset()
        first = ad.evaluate(dataset, service_version="svc-1",
                            evaluator_version="eval-1", scores=_scores(),
                            evaluated_at=T0)
        second = ad.evaluate(dataset, service_version="svc-1",
                             evaluator_version="eval-1", scores=_scores(),
                             evaluated_at=T0)
        self.assertEqual(first, second)
        self.assertEqual(first["run_id"], second["run_id"])
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            ad.write_run(first, a)
            ad.write_run(second, b)
            self.assertEqual((a / "evaluation-run.json").read_bytes(),
                             (b / "evaluation-run.json").read_bytes())
            self.assertEqual((a / "calibration.md").read_bytes(),
                             (b / "calibration.md").read_bytes())

    # VN3-10
    def test_vn3_10_invalid_score_format(self):
        dataset = _dataset()
        cases = [
            ({"sample_id": "s1", "auto_label": "MAYBE"}, "SCORE_FORMAT_INVALID"),
            ({"sample_id": "s1", "auto_label": "PASS",
              "score_kind": "rank"}, "SCORE_FORMAT_INVALID"),
            ({"sample_id": "s1", "auto_label": "PASS",
              "auto_score": "abc"}, "SCORE_FORMAT_INVALID"),
            ({"sample_id": "s1", "auto_label": "PASS",
              "auto_score": 0.9}, "FLOAT_REFUSED"),
        ]
        for score, code in cases:
            with self.assertRaises(ad.EvaluationError) as caught:
                ad.evaluate(dataset, service_version="svc-1",
                            evaluator_version="eval-1", scores=[score],
                            evaluated_at=T0)
            self.assertEqual(caught.exception.code, code, score)

    # VN3-11
    def test_vn3_11_sensitive_field_rejected_or_redacted(self):
        samples = _samples()
        samples[0]["email"] = "buyer@example.com"
        with self.assertRaises(ad.EvaluationError) as caught:
            ad.build_dataset(samples, dataset_id="ds-1", dataset_version="1",
                             criteria=_criteria(), generated_at=T0)
        self.assertEqual(caught.exception.code, "SENSITIVE_FIELD_REJECTED")

        samples = _samples()
        samples[0]["note"] = "联系人 buyer@example.com，电话 +8613800138000"
        dataset = ad.build_dataset(samples, dataset_id="ds-1",
                                   dataset_version="1", criteria=_criteria(),
                                   generated_at=T0)
        note = dataset["samples"][0]["note"]
        self.assertIn("[redacted-email]", note)
        self.assertIn("[redacted-number]", note)
        self.assertNotIn("buyer@example.com", note)

        run = ad.evaluate(dataset, service_version="svc-1",
                          evaluator_version="eval-1",
                          scores=[{"sample_id": "s1", "auto_label": "PASS",
                                   "note": "见 buyer@example.com"},
                                  {"sample_id": "s2", "auto_label": "PASS"}],
                          evaluated_at=T0)
        self.assertNotIn("buyer@example.com", json.dumps(run, ensure_ascii=False))
        self.assertIn("[redacted-email]",
                      json.dumps(run, ensure_ascii=False))

    # VN3-12
    def test_vn3_12_auto_pass_still_needs_human(self):
        run = ad.evaluate(_dataset(), service_version="svc-1",
                          evaluator_version="eval-1", scores=_scores(),
                          evaluated_at=T0)
        for result in run["results"]:
            self.assertTrue(result["requires_human_acceptance"])
            self.assertEqual(result["buyer_verified"], "UNKNOWN")
            self.assertEqual(result["delivery_accepted"], "UNKNOWN")
        view = ad.delivery_view(run)
        self.assertEqual(view["delivery_state"], "UNKNOWN")
        self.assertEqual(view["buyer_verified"], "UNKNOWN")
        self.assertEqual(view["delivery_accepted"], "UNKNOWN")
        self.assertFalse(view["auto_pass_is_acceptance"])

    # VN3-13
    def test_vn3_13_calibration_writes_counts(self):
        run = ad.evaluate(_dataset(), service_version="svc-1",
                          evaluator_version="eval-1", scores=_scores(),
                          human_labels=[_label("s1", "PASS")], evaluated_at=T0)
        calibration = ad.calibrate(run)
        for field in ("window", "samples_total", "labelled", "unlabelled",
                      "agreement_denominator", "agreement_rate", "note"):
            self.assertIn(field, calibration)
        text = ad.render_calibration(run)
        self.assertIn("不进一致率分母", text)
        with tempfile.TemporaryDirectory() as tmp:
            paths = ad.write_run(run, Path(tmp))
            self.assertTrue(Path(paths["calibration"]).is_file())
            self.assertIn("不进一致率分母",
                          Path(paths["calibration"]).read_text(encoding="utf-8"))

    # VN3-14
    def test_vn3_14_zero_payment_renewal_signoff(self):
        run = ad.evaluate(_dataset(), service_version="svc-1",
                          evaluator_version="eval-1", scores=_scores(),
                          evaluated_at=T0)
        self.assertEqual(run["effects"], {"payment": "none", "renewal": "none",
                                          "signoff": "none",
                                          "canonical": "none"})
        self.assertEqual(run["delivery_state"], "UNKNOWN")
        for result in run["results"]:
            self.assertEqual(result["verification_effect"], "none")
        self.assertIn("不付款", run["boundary"])


class CrossCuttingTest(unittest.TestCase):
    # XN2-02 canonical inputs stay byte-identical
    def test_xn2_canonical_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = Path(tmp) / "canonical.json"
            canonical.write_text('{"verdict": "UNKNOWN"}\n', encoding="utf-8")
            before = canonical.read_bytes()
            dataset = _dataset()
            ad.write_dataset(dataset, Path(tmp) / "out")
            ad.write_run(ad.evaluate(dataset, service_version="svc-1",
                                     evaluator_version="eval-1",
                                     scores=_scores(), evaluated_at=T0),
                         Path(tmp) / "run")
            self.assertEqual(canonical.read_bytes(), before)

    # XN2-03 idempotent writes
    def test_xn2_repeated_writes_are_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = _dataset()
            ad.write_dataset(dataset, Path(tmp) / "a")
            ad.write_dataset(dataset, Path(tmp) / "b")
            for name in ("acceptance-dataset.json", "acceptance-samples.csv"):
                self.assertEqual((Path(tmp) / "a" / name).read_bytes(),
                                 (Path(tmp) / "b" / name).read_bytes())

    # XN2-04 provenance is recorded on the run
    def test_xn2_run_records_provenance(self):
        run = ad.evaluate(_dataset(), service_version="svc-9",
                          evaluator_version="eval-9", scores=_scores(),
                          evaluated_at=T0)
        self.assertEqual(run["dataset"]["dataset_id"], "ds-1")
        self.assertEqual(run["dataset"]["dataset_version"], "1")
        self.assertTrue(run["dataset"]["dataset_hash"].startswith("sha256:"))
        self.assertTrue(run["input_fingerprint"].startswith("sha256:"))
        self.assertEqual(run["generator"], ad.GENERATOR)
        self.assertEqual(run["generator_version"], ad.GENERATOR_VERSION)

    # XN2-05 missing labels are null, never zero
    def test_xn2_missing_labels_not_zero(self):
        run = ad.evaluate(_dataset(), service_version="svc-1",
                          evaluator_version="eval-1", scores=_scores(),
                          evaluated_at=T0)
        self.assertEqual(run["calibration"]["agreement_denominator"], 0)
        self.assertIsNone(run["calibration"]["agreement_rate"])
        for result in run["results"]:
            self.assertIsNone(result["human_label"])

    # XN2-09 CSV formula injection is escaped
    def test_xn2_csv_formula_injection_escaped(self):
        samples = _samples()
        samples[0]["sample_id"] = "=cmd"
        dataset = ad.build_dataset(samples, dataset_id="ds-1",
                                   dataset_version="1", criteria=_criteria(),
                                   generated_at=T0)
        with tempfile.TemporaryDirectory() as tmp:
            paths = ad.write_dataset(dataset, Path(tmp))
            lines = Path(paths["csv"]).read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[1].startswith("'=cmd"), lines[1])

    # XN2-10 deterministic replay from serialized inputs
    def test_xn2_deterministic_replay(self):
        dataset = _dataset()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ad.write_dataset(dataset, root)
            loaded = ad.load_dataset(root / "acceptance-dataset.json")
            scores = _scores()
            first = ad.evaluate(loaded, service_version="svc-1",
                                evaluator_version="eval-1", scores=scores,
                                evaluated_at=T0)
            second = ad.evaluate(loaded, service_version="svc-1",
                                 evaluator_version="eval-1",
                                 scores=json.loads(json.dumps(scores)),
                                 evaluated_at=T0)
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(json.dumps(first, sort_keys=True),
                             json.dumps(second, sort_keys=True))

    # XN2-08 unknown run shape is refused, not guessed
    def test_xn2_invalid_run_refused(self):
        with self.assertRaises(ad.EvaluationError):
            ad.calibrate({"schema": "other"})

    # XN2-01 same dataset version with same hash is accepted (no duplicate)
    def test_xn2_same_version_same_hash_is_idempotent(self):
        first = _dataset(generated_at=T0)
        repeated = ad.build_dataset(_samples(), dataset_id="ds-1",
                                    dataset_version="1", criteria=_criteria(),
                                    previous=first, generated_at=T1)
        self.assertEqual(first["dataset_hash"], repeated["dataset_hash"])


if __name__ == "__main__":
    unittest.main()
