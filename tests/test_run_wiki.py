import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spark"))
import run_wiki


class LauncherTests(unittest.TestCase):
    def test_default_and_explicit_commands(self):
        folder = Path(run_wiki.__file__).resolve().parent
        cases = [
            (["--output", "../tests/a file.tsv"], "spark-submit",
             ["extract_wiki.py", "--output", "../tests/a file.tsv"]),
            (["submit", "join_plants.py"], "spark-submit", ["join_plants.py"]),
            (["shell"], "pyspark", []),
            (["python", "-V"], "python", ["-V"]),
        ]
        for arguments, program, expected_arguments in cases:
            with self.subTest(arguments=arguments), \
                 patch("os.access", return_value=True), patch.object(Path, "mkdir"), \
                 patch("subprocess.check_output", return_value="172.20.1.2 127.0.0.1\n"), \
                 patch("os.chdir") as change_directory, patch("os.execve") as execute:
                run_wiki.run_spark(folder, arguments)
                executable, command, environment = execute.call_args.args
                self.assertEqual(Path(executable).name, program)
                self.assertEqual(command[0], executable)
                options = [] if program == "python" else [
                    "--conf", f"spark.sql.warehouse.dir={Path.home() / 'vinf-spark' / 'warehouse'}"
                ]
                self.assertEqual(command[1:], options + expected_arguments)
                self.assertEqual(environment["SPARK_CONF_DIR"], str(folder / "conf"))
                self.assertEqual(environment["SPARK_LOCAL_IP"], "172.20.1.2")
                self.assertEqual(environment["PYSPARK_PYTHON"], environment["PYSPARK_DRIVER_PYTHON"])
                change_directory.assert_called_once_with(folder)

    def test_windows_forwards_arguments_and_exit_code(self):
        with patch.object(run_wiki, "os", SimpleNamespace(name="nt")), \
             patch.object(sys, "argv", ["run_wiki.py", "--output", "../tests/a file.tsv"]), \
             patch("subprocess.check_output", return_value="/mnt/c/project with spaces/spark\n") as convert, \
             patch("subprocess.call", return_value=17) as launch:
            self.assertEqual(run_wiki.main(), 17)
            self.assertEqual(convert.call_args.args[0][:6],
                             ["wsl.exe", "-d", "Ubuntu-24.04", "--exec", "wslpath", "-a"])
            self.assertEqual(launch.call_args.args[0], [
                "wsl.exe", "-d", "Ubuntu-24.04", "--cd", "/mnt/c/project with spaces/spark",
                "--exec", "python3", "./run_wiki.py", "--output", "../tests/a file.tsv",
            ])

    def test_missing_runtime_does_not_launch(self):
        with patch("os.access", return_value=False), patch("os.execve") as execute:
            with self.assertRaisesRegex(OSError, "Spark is not installed"):
                run_wiki.run_spark(Path("spark"), [])
            execute.assert_not_called()

    def test_error_and_cancellation_exit_codes(self):
        for error, code in [(OSError("WSL unavailable"), 1), (KeyboardInterrupt(), 130)]:
            with self.subTest(code=code), patch.object(run_wiki, "os", SimpleNamespace(name="nt")), \
                 patch("subprocess.check_output", side_effect=error):
                self.assertEqual(run_wiki.main(), code)


if __name__ == "__main__":
    unittest.main()
