"""Distribution contract for the standalone ``verovo-buyer-ledger`` wheel.

The ledger reads a catalog at runtime. A checkout keeps the editable source at
``buyer-ledger/catalog/tools.json``; an installed wheel carries the packaged copy
at ``buyer_ledger/data/tools.json``. These tests keep the two byte-identical and
prove the console-script target imports without executing the CLI.
"""
import unittest
from pathlib import Path

from buyer_ledger import catalog


class PackagingContractTests(unittest.TestCase):
    def test_packaged_catalog_matches_checkout_catalog(self):
        source = Path(__file__).resolve().parents[1] / "catalog" / "tools.json"
        packaged = (Path(__file__).resolve().parents[1]
                    / "buyer_ledger" / "data" / "tools.json")
        self.assertTrue(source.is_file(), "checkout catalog is missing")
        self.assertTrue(packaged.is_file(), "packaged catalog is missing")
        self.assertEqual(
            source.read_bytes(), packaged.read_bytes(),
            "buyer_ledger/data/tools.json drifted from catalog/tools.json; "
            "copy the edited catalog into the package before shipping",
        )
        # In a checkout the editable source wins; an installed wheel has no
        # sibling catalog/ directory and falls back to the packaged copy.
        self.assertEqual(source.resolve(), Path(catalog.CATALOG_PATH).resolve())

    def test_catalog_loads_from_the_resolved_path(self):
        loaded = catalog.load_catalog()
        self.assertTrue(loaded.get("providers"))

    def test_console_entry_point_imports_without_running_main(self):
        from buyer_ledger import __main__ as entry
        from buyer_ledger import cli
        self.assertIs(entry.main, cli.main)


if __name__ == "__main__":
    unittest.main()
