import unittest
from pathlib import Path


IMAGE_DIR = Path(__file__).resolve().parents[1] / "image"


class ImageScriptTests(unittest.TestCase):
    def test_setup_scripts_stop_on_errors_unset_variables_and_failed_pipes(self):
        scripts = sorted(IMAGE_DIR.glob("*.sh"))
        self.assertTrue(scripts)
        for script in scripts:
            with self.subTest(script=script.name):
                self.assertEqual(script.read_text().splitlines()[:2], ["#!/bin/bash", "set -euo pipefail"])


if __name__ == "__main__":
    unittest.main()
