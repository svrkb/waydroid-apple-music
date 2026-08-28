import hashlib
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "patch_waydroid_hires", ROOT / "scripts" / "patch_waydroid_hires.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PatcherTests(unittest.TestCase):
    def test_binary_patch_checks_both_hashes(self):
        relative = "test.bin"
        source = b"0123456789"
        expected = b"0123AB6789"
        MODULE.FILES[relative] = {
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "patched_sha256": hashlib.sha256(expected).hexdigest(),
            "edits": {4: ("3435", "4142")},
        }
        try:
            self.assertEqual(MODULE.patch_binary(relative, source), expected)
            with self.assertRaises(ValueError):
                MODULE.patch_binary(relative, b"wrong")
        finally:
            del MODULE.FILES[relative]

    def test_policy_rejects_unknown_input(self):
        with self.assertRaises(ValueError):
            MODULE.patch_policy(b"not a supported policy")


if __name__ == "__main__":
    unittest.main()
