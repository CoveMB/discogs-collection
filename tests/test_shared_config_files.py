import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.config_files import load_json_file, load_or_create_json_file, reject_unknown_keys  # noqa: E402


class SharedConfigFilesTests(unittest.TestCase):
    def test_load_or_create_json_file_writes_default_payload(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config" / "example.json"

            payload = load_or_create_json_file(
                config_path,
                default_payload={"enabled": True},
                malformed_label="example config",
            )

            self.assertEqual(payload, {"enabled": True})
            self.assertEqual(config_path.read_text(encoding="utf-8"), '{\n  "enabled": true\n}\n')

    def test_load_json_file_reports_malformed_json_with_config_label(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            config_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, f"malformed example config JSON: {config_path}"):
                load_json_file(config_path, malformed_label="example config")

    def test_reject_unknown_keys_sorts_keys_and_uses_config_label(self):
        with self.assertRaisesRegex(ValueError, "unknown example config key: alpha, zeta"):
            reject_unknown_keys(
                {"zeta": 1, "allowed": 2, "alpha": 3},
                allowed_keys=frozenset({"allowed"}),
                config_label="example config",
            )


if __name__ == "__main__":
    unittest.main()
