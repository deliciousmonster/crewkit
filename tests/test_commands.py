import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crewkit import commands, lint


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "democmd.py").write_text(
            "import sys\nif __name__ == '__main__':\n    print(sys.argv)\n    if '--usage' in sys.argv:\n        sys.exit('usage: demo')\n    sys.exit(3 if '--fail' in sys.argv else 0)\n")
        sys.path.insert(0, self.tmp.name)
        self.addCleanup(sys.path.remove, self.tmp.name)

    def run_it(self, argv):
        with mock.patch("sys.stdout", new=io.StringIO()) as out, mock.patch("sys.stderr", new=io.StringIO()) as err:
            code = commands.dispatch({"demo": "democmd"}, argv, prog="crew")
        return code, out.getvalue(), err.getvalue()

    def test_a_subcommand_runs_its_module_as_main_with_its_arguments(self):
        code, out, _ = self.run_it(["demo", "run", "--ws", "/w"])
        self.assertEqual((code, out.strip()), (0, "['crew demo', 'run', '--ws', '/w']"))
        self.assertEqual(self.run_it(["demo", "--fail"])[0], 3)

    def test_a_string_exit_prints_it_and_fails(self):
        code, _, err = self.run_it(["demo", "--usage"])
        self.assertEqual((code, err.strip()), (1, "usage: demo"))

    def test_an_unknown_command_prints_the_list(self):
        code, _, err = self.run_it(["nope"])
        self.assertEqual(code, 2)
        self.assertIn("demo  democmd", err)


class Lint(unittest.TestCase):
    def test_aliases_and_variables_run_as_commands_are_found(self):
        bad = ["`reports` below is `uv run --project <crew> python -m x`.",
               'S="uv run --project /c python -m reports"',
               "$S run --ws /w", 'cd /x && "$TOOL" check']
        for line in bad:
            self.assertTrue(lint.aliases(line), line)

    def test_literal_commands_and_variables_as_arguments_pass(self):
        good = ["uv run --project <crew> examplecrew reports run --ws <ws>",
                'for c in "$A" "$(cat f)"; do echo "$c"; done', 'SKILL_DIR="/a/b"', "echo $HOME/x"]
        for line in good:
            self.assertEqual(lint.aliases(line), [], line)
