import json
import unittest

from buyer_ledger import close as close_mod
from buyer_ledger.close import CloseError, ingest, init_case
from tests.test_close_period_import import CloseCaseTestBase


def _repository_api():
    from buyer_ledger.case_repository import CaseRepository
    return CaseRepository


def _period_api():
    from buyer_ledger.period_case import PeriodCase
    return PeriodCase


def _api():
    return _repository_api(), _period_api()


class CaseRepositoryTests(CloseCaseTestBase):
    def test_repository_api_exists(self):
        try:
            CaseRepository, PeriodCase = _api()
        except ModuleNotFoundError as exc:
            self.fail("case repository API is missing: %s" % exc)
        self.assertTrue(callable(CaseRepository))
        self.assertTrue(callable(PeriodCase))

    def test_missing_state_returns_none_but_corrupt_state_fails_loudly(self):
        CaseRepository = _repository_api()
        self.write_bundle()
        init_case(self.case, job_path=self.bundle / "period-job.json")
        repo = CaseRepository(self.case)
        self.assertIsNone(repo.read_json("review"))
        (self.case / "review.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(CloseError) as raised:
            repo.read_json("review")
        self.assertEqual("ARTIFACT_PARTIAL", raised.exception.code)
        self.assertIn("review.json", str(raised.exception))

    def test_atomic_write_replaces_bytes_without_changing_case_root(self):
        CaseRepository = _repository_api()
        self.write_bundle()
        init_case(self.case, job_path=self.bundle / "period-job.json")
        repo = CaseRepository(self.case)
        target = self.case / "review.json"
        repo.atomic_write_bytes(target, b'{"state":"reviewed"}\n')
        self.assertEqual(b'{"state":"reviewed"}\n', target.read_bytes())
        self.assertEqual({"state": "reviewed"}, repo.read_json("review"))
        self.assertFalse(list(self.case.glob("*.tmp-*")))


class PeriodCaseProjectionTests(CloseCaseTestBase):
    def test_period_case_matches_legacy_projection_and_writes_nothing(self):
        _, PeriodCase = _api()
        self.write_bundle()
        init_case(self.case, job_path=self.bundle / "period-job.json")
        ingest(self.case, bundle=self.bundle)
        expected = close_mod.build_case(self.case)
        before = {path: path.read_bytes() for path in self.case.rglob("*") if path.is_file()}
        actual = PeriodCase(self.case).project()
        after = {path: path.read_bytes() for path in self.case.rglob("*") if path.is_file()}
        self.assertEqual(expected, actual)
        self.assertEqual(before, after)
        self.assertEqual("verovo.buyer-close-case/1", actual["schema"])
        self.assertEqual(json.loads(json.dumps(actual)), actual)


if __name__ == "__main__":
    unittest.main()
