import copy
import json
import unittest
from pathlib import Path

from verovo_healthcheck import provider_contract


ROOT = Path(__file__).resolve().parents[2]


def valid_manifest():
    return {
        "schema": "verovo.provider-manifest/0.1",
        "adapter_id": "otel-file",
        "engine": {
            "id": "opentelemetry-genai-file",
            "version": "2026-11-23",
            "integration_mode": "file",
        },
        "source": {
            "party": "buyer",
            "control_domain": "buyer",
            "collector_party": "buyer",
            "collection_method": "local_file_export",
        },
        "readiness_checks": [
            {
                "check_id": "runtime",
                "status": "pass",
                "required": True,
                "observed": "Python 3.9 stdlib reader available",
            },
            {
                "check_id": "input_schema",
                "status": "pass",
                "required": True,
                "observed": "Pinned OTel JSON fixture parsed",
            },
            {
                "check_id": "content_redaction",
                "status": "pass",
                "required": False,
                "observed": "Payload collection disabled",
            },
        ],
        "limits": {
            "input_bytes": 33554432,
            "output_bytes": 4194304,
            "depth": 32,
            "cpu_seconds": 30,
            "timeout_ms": 30000,
        },
        "coverage": {
            "status": "partial",
            "matched": 2,
            "unmatched": 1,
            "duplicates": 0,
            "rejected": 0,
            "omitted": 0,
        },
        "cleanup": {"status": "released"},
        "raw_snapshot_refs": [
            "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ],
        "normalization_rule_version": "otel-file/0.1",
    }


class ProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.valid = valid_manifest()

    def test_valid_file_provider_is_admitted_with_partial_coverage(self):
        result = provider_contract.evaluate_manifest(self.valid)
        self.assertEqual(result["admission"], "admit")
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertFalse(result["live_engine_contacted"])
        self.assertEqual(result["reasons"], [])

    def test_required_failure_refuses_even_with_declared_complete_coverage(self):
        self.valid["readiness_checks"][0]["status"] = "fail"
        self.valid["coverage"]["status"] = "complete"
        result = provider_contract.evaluate_manifest(self.valid)
        self.assertEqual(result["admission"], "refuse")
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertIn("runtime", result["reasons"][0])

    def test_required_unknown_refuses(self):
        self.valid["readiness_checks"][0]["status"] = "unknown"
        result = provider_contract.evaluate_manifest(self.valid)
        self.assertEqual(result["admission"], "refuse")

    def test_optional_failure_degrades(self):
        self.valid["readiness_checks"][2]["status"] = "fail"
        result = provider_contract.evaluate_manifest(self.valid)
        self.assertEqual(result["admission"], "degrade")

    def test_retained_or_unknown_cleanup_refuses(self):
        for status in ("retained", "unknown"):
            with self.subTest(status=status):
                self.valid["cleanup"]["status"] = status
                result = provider_contract.evaluate_manifest(self.valid)
                self.assertEqual(result["admission"], "refuse")

    def test_process_engine_requires_executable_digest(self):
        self.valid["engine"]["integration_mode"] = "process"
        with self.assertRaisesRegex(provider_contract.ProviderContractError, "executable_sha256"):
            provider_contract.evaluate_manifest(self.valid)
        self.valid["engine"]["executable_sha256"] = "not-a-digest"
        with self.assertRaisesRegex(provider_contract.ProviderContractError, "executable_sha256"):
            provider_contract.evaluate_manifest(self.valid)

    def test_boolean_or_non_positive_limits_are_refused(self):
        for value in (True, 0, -1):
            with self.subTest(value=value):
                self.valid["limits"]["input_bytes"] = value
                with self.assertRaisesRegex(provider_contract.ProviderContractError, "input_bytes"):
                    provider_contract.evaluate_manifest(self.valid)

    def test_negative_coverage_count_is_refused(self):
        self.valid["coverage"]["unmatched"] = -1
        with self.assertRaisesRegex(provider_contract.ProviderContractError, "unmatched"):
            provider_contract.evaluate_manifest(self.valid)

    def test_every_check_requires_an_explicit_required_flag(self):
        del self.valid["readiness_checks"][0]["required"]
        with self.assertRaisesRegex(provider_contract.ProviderContractError, "required"):
            provider_contract.evaluate_manifest(self.valid)

    def test_input_document_is_not_mutated(self):
        original = copy.deepcopy(self.valid)
        provider_contract.evaluate_manifest(self.valid)
        self.assertEqual(self.valid, original)

    def test_example_manifest_is_valid(self):
        document = json.loads(
            (ROOT / "examples" / "provider-manifest" / "otel-file.json").read_text(encoding="utf-8")
        )
        result = provider_contract.evaluate_manifest(document)
        self.assertEqual(result["admission"], "admit")


if __name__ == "__main__":
    unittest.main()


class ProviderContractCliTests(unittest.TestCase):
    def test_cli_evaluates_example_and_writes_json(self):
        import contextlib
        import io
        import tempfile
        from verovo_healthcheck.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "admission.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = main([
                    "provider", "check",
                    "--manifest", str(ROOT / "examples" / "provider-manifest" / "otel-file.json"),
                    "--json", str(output_path),
                ])
            self.assertEqual(rc, 0)
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["admission"], "admit")
            self.assertFalse(result["live_engine_contacted"])
            self.assertEqual(json.loads(stdout.getvalue()), result)

    def test_cli_refusal_is_analysis_not_input_error(self):
        import contextlib
        import io
        import tempfile
        from verovo_healthcheck.cli import main

        document = valid_manifest()
        document["cleanup"]["status"] = "retained"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = main(["provider", "check", "--manifest", str(path)])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(stdout.getvalue())["admission"], "refuse")

    def test_cli_bad_manifest_exits_two(self):
        import contextlib
        import io
        from verovo_healthcheck.cli import main

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = main(["provider", "check", "--manifest", "/does/not/exist.json"])
        self.assertEqual(rc, 2)
        self.assertIn("error:", stderr.getvalue())


class ProviderContractMalformedEvidenceTests(unittest.TestCase):
    def test_non_string_snapshot_reference_raises_contract_error(self):
        document = valid_manifest()
        document["raw_snapshot_refs"] = [{"sha256": "not-a-string-reference"}]
        with self.assertRaisesRegex(provider_contract.ProviderContractError, "raw_snapshot_refs"):
            provider_contract.evaluate_manifest(document)
