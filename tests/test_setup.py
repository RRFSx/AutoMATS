import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        source = Path(__file__).resolve().parents[1]
        for name in ("setup.sh", "run.sh"):
            shutil.copy2(source / name, self.project / name)
        self.log = self.root / "calls.log"
        self.environment = dict(os.environ, AUTOMATS_TEST_LOG=str(self.log))
        self.python = self.root / "fake python"
        self.python.write_text(
            '#!/bin/sh\n'
            'printf "<call>\\n%s\\n" "$PWD" >> "$AUTOMATS_TEST_LOG"\n'
            'printf "%s\\n" "$@" >> "$AUTOMATS_TEST_LOG"\n'
            'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
            '    mkdir -p "$3/bin"\n'
            '    cp "$0" "$3/bin/python"\n'
            '    chmod +x "$3/bin/python"\n'
            'fi\n'
            'exit "${AUTOMATS_TEST_EXIT_CODE:-0}"\n'
        )
        self.python.chmod(0o755)

    def run_script(self, name, *arguments):
        return subprocess.run(
            [str(self.project / name), *map(str, arguments)],
            cwd=self.root, env=self.environment, capture_output=True, text=True,
        )

    def install_fake_environment(self):
        interpreter = self.project / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        shutil.copyfile(self.python, interpreter)
        interpreter.chmod(0o755)

    def test_setup_creates_private_environment_and_installs_project(self):
        result = self.run_script("setup.sh", self.python)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.project / ".venv" / "bin" / "python").exists())
        calls = self.log.read_text()
        self.assertIn(f"-m\nvenv\n{self.project / '.venv'}\n", calls)
        self.assertIn(f"-m\npip\ninstall\n{self.project}\n", calls)
        self.assertIn("Setup complete", result.stdout)

    def test_setup_reuses_existing_environment(self):
        self.install_fake_environment()
        result = self.run_script("setup.sh", "nonexistent-bootstrap-python")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertNotIn("-m\nvenv\n", calls)
        self.assertIn(f"-m\npip\ninstall\n{self.project}\n", calls)

    def test_setup_leaves_unusable_environment_unchanged(self):
        environment = self.project / ".venv"
        environment.mkdir()
        result = self.run_script("setup.sh", self.python)
        self.assertEqual(result.returncode, 1)
        self.assertIn("left unchanged", result.stderr)
        self.assertEqual(list(environment.iterdir()), [])
        self.assertFalse(self.log.exists())

    def test_setup_reports_missing_python(self):
        result = self.run_script("setup.sh", "nonexistent-bootstrap-python")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python not found", result.stderr)
        self.assertFalse((self.project / ".venv").exists())

    def test_setup_stops_if_interpreter_check_fails(self):
        self.environment["AUTOMATS_TEST_EXIT_CODE"] = "1"
        result = self.run_script("setup.sh", self.python)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("-m\nvenv\n", self.log.read_text())
        self.assertFalse((self.project / ".venv").exists())

    def test_launcher_preserves_arguments_directory_and_exit_code(self):
        self.install_fake_environment()
        self.environment["AUTOMATS_TEST_EXIT_CODE"] = "7"
        result = self.run_script("run.sh", "config with spaces.yaml", "--output-dir", "my figures", "--plan")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(
            self.log.read_text().splitlines(),
            ["<call>", str(self.root), "-m", "automats", "config with spaces.yaml",
             "--output-dir", "my figures", "--plan"],
        )

    def test_launcher_reports_missing_environment(self):
        result = self.run_script("run.sh", "--help")
        self.assertEqual(result.returncode, 1)
        self.assertIn("setup.sh", result.stderr)
        self.assertFalse(self.log.exists())



if __name__ == "__main__":
    unittest.main()