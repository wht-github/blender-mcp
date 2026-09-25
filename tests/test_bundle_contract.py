"""A reused bundle must prove its target before an existing package is replaced."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bundle_dependencies as dependencies
import package


class BundleContractTests(unittest.TestCase):
    def test_reuse_checks_target_dependency_lock_and_addon_version(self):
        expected = dependencies.bundle_metadata("3.13", "x86_64-pc-windows-msvc")
        with tempfile.TemporaryDirectory() as temporary:
            libs = Path(temporary)
            metadata = libs / dependencies._contract.METADATA_FILE
            with patch.object(dependencies, "LIBS_DIR", libs):
                for field, wrong in (
                    ("schema_version", 0),
                    ("python_version", "3.11"),
                    ("python_platform", "x86_64-manylinux_2_28"),
                    ("requirements_sha256", "outdated"),
                    ("addon_version", "0.0.0"),
                ):
                    with self.subTest(field=field):
                        metadata.write_text(json.dumps({**expected, field: wrong}), encoding="utf-8")
                        with self.assertRaisesRegex(RuntimeError, field):
                            dependencies.validate_existing_bundle("3.13", "x86_64-pc-windows-msvc")
                metadata.write_text(json.dumps(expected), encoding="utf-8")
                self.assertEqual(
                    dependencies.validate_existing_bundle("3.13", "x86_64-pc-windows-msvc"), expected,
                )

    def test_rejected_reuse_preserves_existing_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "addon.zip"
            output.write_bytes(b"previous usable package")
            with patch.object(package, "validate_existing_bundle", side_effect=RuntimeError("wrong target")):
                with self.assertRaisesRegex(RuntimeError, "wrong target"):
                    package.create_addon_zip(str(output), bundle_dependencies=False, python_version="3.11")
            self.assertEqual(output.read_bytes(), b"previous usable package")


if __name__ == "__main__":
    unittest.main()
