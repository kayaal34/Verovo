import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "healthcheck"))

from verovo_healthcheck import provider_contract  # noqa: E402


class ProviderManifestSchemaTests(unittest.TestCase):
    def test_schema_mirrors_contract_vocabularies(self):
        schema = json.loads((ROOT / "schemas" / "provider-manifest.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema"]["const"], "verovo.provider-manifest/0.1")
        engine = schema["$defs"]["engine"]["properties"]
        self.assertEqual(set(engine["integration_mode"]["enum"]), provider_contract.INTEGRATION_MODES)
        self.assertEqual(
            set(schema["$defs"]["limits"]["required"]),
            set(provider_contract.LIMIT_FIELDS),
        )
        coverage = schema["$defs"]["coverage"]
        self.assertEqual(set(coverage["properties"]["status"]["enum"]), provider_contract.COVERAGE_STATUSES)
        for field in provider_contract.COVERAGE_COUNTS:
            self.assertIn(field, coverage["required"])
        self.assertEqual(
            set(schema["$defs"]["cleanup"]["properties"]["status"]["enum"]),
            provider_contract.CLEANUP_STATUSES,
        )

    def test_example_has_all_required_fields_and_valid_admission(self):
        schema = json.loads((ROOT / "schemas" / "provider-manifest.schema.json").read_text(encoding="utf-8"))
        document = json.loads((ROOT / "examples" / "provider-manifest" / "otel-file.json").read_text(encoding="utf-8"))
        for field in schema["required"]:
            self.assertIn(field, document)
        self.assertEqual(provider_contract.evaluate_manifest(document)["admission"], "admit")

    def test_example_snapshot_digest_matches_the_retained_bytes(self):
        bundle = ROOT / "examples" / "provider-manifest"
        document = json.loads((bundle / "otel-file.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256((bundle / "otel-file.snapshot.json").read_bytes()).hexdigest()
        self.assertEqual(document["raw_snapshot_refs"], ["sha256:" + digest])


if __name__ == "__main__":
    unittest.main()
