import unittest


class CountingValues(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.values_calls = 0

    def values(self):
        self.values_calls += 1
        return super().values()


class PeriodIntakeCoverageTests(unittest.TestCase):
    def test_task_coverage_is_correct_and_indexes_each_source_once(self):
        try:
            from verovo_healthcheck.period_intake import task_coverage
        except ImportError as exc:
            self.fail("task_coverage API is missing: %s" % exc)
        tasks = {"t1": {"task_id": "t1"}, "t2": {"task_id": "t2"}, "t3": {"task_id": "t3"}}
        runs = CountingValues(r1={"run_id": "r1", "task_id": "t1"})
        outcomes = [{"task_id": "t2", "outcome_id": "o2"}]
        self.assertEqual(
            {"tasks_without_run": 2, "tasks_without_outcome": 2},
            task_coverage(tasks, runs, outcomes),
        )
        self.assertEqual(1, runs.values_calls)


if __name__ == "__main__":
    unittest.main()
