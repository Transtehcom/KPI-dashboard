import pathlib
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for operations UI checks")
class OperationsUiTest(unittest.TestCase):
    def test_operations_ui(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "test_operations_ui.js")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
