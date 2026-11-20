"""crewkit.denylist on a synthetic repository and list; crewkit's own tracked files against the installed list."""
import contextlib
import io
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from crewkit import denylist
from crewkit.install import main as crewkit_main
from installcase import scratch

ROOT = Path(__file__).resolve().parents[1]
# Built from parts, so this file never holds the shapes the structural check looks for.
HOME_PATH = "/" + "Users/alexq/" + "src"
LINUX_HOME = "/" + "home/alexq/" + "src"
EMAIL = "alex.q" + "@" + "acme-corp.io"


class Synthetic(unittest.TestCase):
    def setUp(self):
        self.repo = scratch(self)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        self.list = self.repo.parent / f"{self.repo.name}-terms"
        self.addCleanup(lambda: self.list.unlink(missing_ok=True))
        self.list.write_text("# synthetic terms\n\nGlobex\ninitrode\\s+labs\n")
        self.env = mock.patch.dict(os.environ, {denylist.ENV: str(self.list)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def add(self, rel: str, text: str | bytes, track: bool = True):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        if track:
            subprocess.run(["git", "-C", str(self.repo), "add", rel], check=True)

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = crewkit_main(["denylist", "--dir", str(self.repo), *argv])
        return code, out.getvalue()

    def test_a_hit_names_path_line_and_list_line_never_the_term(self):
        self.add("README.md", "intro\nBuilt for GLOBEX.\n")
        self.add("docs/a.md", "one\ntwo\nInitrode   Labs ships it\n")
        code, out = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("README.md:2: term 3", out)
        self.assertIn("docs/a.md:3: term 4", out)
        self.assertNotIn("lobex", out.lower())
        self.assertNotIn("initrode", out.lower())

    def test_untracked_lock_and_binary_files_are_not_scanned(self):
        self.add("notes.md", "Globex\n", track=False)
        self.add("uv.lock", "globex = 1\n")
        self.add("img.bin", b"\x00Globex")
        self.add("ok.md", "Acme only\n")
        self.assertEqual(self.cli(), (0, "denylist: no hits\n"))

    def test_an_absent_list_skips_with_the_reason(self):
        self.list.unlink()
        self.add("README.md", "Globex\n")
        code, out = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("the term scan is skipped", out)

    def test_a_bad_regex_names_its_line_only(self):
        self.list.write_text("ok\n(unclosed\n")
        self.add("README.md", "x\n")
        code, out = self.cli()
        self.assertEqual(code, 2)
        self.assertIn("line 2: not a valid regex", out)
        self.assertNotIn("unclosed", out)

    def test_structural_flags_home_paths_and_real_emails(self):
        self.add("a.md", f"see {HOME_PATH}\n{LINUX_HOME}\nmail {EMAIL}\n")
        self.add("b.md", "/" + "Users/<name>/x and /" + "home/runner/work and alex@example.com\n"
                 "123+someone@users.noreply.github.com and git@github.com:owner/x and t@t\n")
        found = [str(h) for h in denylist.structural(self.repo)]
        self.assertEqual(found, ["a.md:1: home path", "a.md:2: home path", "a.md:3: email"])

    def test_assert_clean_fails_on_a_hit_and_skips_without_a_list(self):
        self.add("a.md", "fine\n")
        result = unittest.TestResult()

        class Case(unittest.TestCase):
            def runTest(inner):  # noqa: N805
                denylist.assert_clean(inner, self.repo)

        Case().run(result)
        self.assertEqual((len(result.failures), len(result.skipped)), (0, 0))
        self.add("b.md", "Globex\n")
        Case().run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertNotIn("lobex", result.failures[0][1].lower())
        self.list.unlink()
        self.add("b.md", "fine\n")
        Case().run(result)
        self.assertEqual(len(result.skipped), 1)


class Crewkit(unittest.TestCase):
    """crewkit's own repository: the structural check always, the term scan where a list is installed."""

    def test_no_private_content(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        denylist.assert_clean(self, ROOT)


if __name__ == "__main__":
    unittest.main()
