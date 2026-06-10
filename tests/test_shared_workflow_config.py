import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared import workflow_config  # noqa: E402


class WorkflowConfigTests(unittest.TestCase):
    def test_missing_config_file_is_created_with_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config" / "workflow.json"

            config = workflow_config.load_or_create_workflow_config(config_path)

            self.assertEqual(config.max_rows_per_split, 500)
            self.assertTrue(config.keep_release_tracks_together)
            self.assertTrue(config.create_new_split_files_for_new_releases)
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {
                    "max_rows_per_split": 500,
                    "keep_release_tracks_together": True,
                    "create_new_split_files_for_new_releases": True,
                },
            )

    def test_missing_keys_use_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "workflow.json"
            config_path.write_text('{"max_rows_per_split": 25}\n', encoding="utf-8")

            config = workflow_config.load_or_create_workflow_config(config_path)

            self.assertEqual(config.max_rows_per_split, 25)
            self.assertTrue(config.keep_release_tracks_together)
            self.assertTrue(config.create_new_split_files_for_new_releases)

    def test_unknown_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "workflow.json"
            config_path.write_text('{"max_rows_per_split": 500, "max_row_per_split": 250}\n', encoding="utf-8")

            with self.assertRaises(ValueError) as context:
                workflow_config.load_or_create_workflow_config(config_path)

            self.assertIn("unknown workflow config key", str(context.exception))
            self.assertIn("max_row_per_split", str(context.exception))

    def test_invalid_values_are_rejected(self):
        invalid_payloads = (
            ('{"max_rows_per_split": 0}\n', "max_rows_per_split must be at least 1"),
            ('{"max_rows_per_split": true}\n', "max_rows_per_split must be an integer"),
            ('{"keep_release_tracks_together": "true"}\n', "keep_release_tracks_together must be a boolean"),
            (
                '{"create_new_split_files_for_new_releases": "false"}\n',
                "create_new_split_files_for_new_releases must be a boolean",
            ),
        )
        for payload, expected_message in invalid_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    config_path = Path(temporary_directory) / "workflow.json"
                    config_path.write_text(payload, encoding="utf-8")

                    with self.assertRaises(ValueError) as context:
                        workflow_config.load_or_create_workflow_config(config_path)

                    self.assertIn(expected_message, str(context.exception))

    def test_malformed_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "workflow.json"
            config_path.write_text("{", encoding="utf-8")

            with self.assertRaises(ValueError) as context:
                workflow_config.load_or_create_workflow_config(config_path)

            self.assertIn("malformed workflow config JSON", str(context.exception))


if __name__ == "__main__":
    unittest.main()
