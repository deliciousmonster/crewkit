"""L1: the hook through `crewkit install --scope project` and a real Claude Code, three haiku replies in two runs.

Skipped unless CREWKIT_LIVE=1. HOME stays real so `claude` keeps its login; the throwaway crew's
~/.config folder, its state file and its install are removed when the test ends.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import usagecase as uc

from crewkit import spec, stopline, usage

SKILL = "probecycle"


@unittest.skipUnless(os.environ.get("CREWKIT_LIVE") == "1", "live: set CREWKIT_LIVE=1 (two claude runs)")
class Live(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.name = f"crewkit-live-{os.getpid()}"
        self.crew, self.proj = self.d / self.name, self.d / "proj"
        (self.crew / "skills" / SKILL).mkdir(parents=True)
        self.proj.mkdir()
        (self.crew / "skills" / SKILL / "SKILL.md").write_text(
            f"---\nname: {SKILL}\ndescription: Reply with one short word.\n---\n\nReply with one short word.\n")
        (self.crew / "pyproject.toml").write_text(
            f'[project]\nname = "{self.name}"\nversion = "0.0.1"\n\n[tool.crewkit]\nskills = ["{SKILL}"]\n\n'
            f'[[tool.crewkit.hooks]]\nevent = "Stop"\ncommand = "{stopline.COMMAND.replace("{skill}", SKILL)}"\n')
        uc.wrapper(self.crew, sys.executable)
        self.session = None
        self.addCleanup(self.cleanup)

    def crewkit(self, *argv):
        return subprocess.run([sys.executable, "-m", "crewkit.install", *argv, "--root", str(self.crew)],
                              capture_output=True, text=True, timeout=60, check=False)

    def cleanup(self):
        self.crewkit("uninstall")
        shutil.rmtree(spec.config_dir(self.name), ignore_errors=True)
        if self.session:
            for p in stopline.state_dir().glob(f"{self.session}-{SKILL}.json*"):
                p.unlink()

    def claude(self, argv, stdin=None):
        r = subprocess.run(["claude", "-p", "--model", "haiku", "--setting-sources", "project", "--output-format",
                            "stream-json", "--verbose", *argv], input=stdin, capture_output=True, text=True,
                           cwd=self.proj, timeout=300, check=False)
        out = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        self.session = self.session or next((m.get("session_id") for m in out if m.get("session_id")), None)
        return [t for t in (json.dumps(m, ensure_ascii=False) for m in out) if "Tokens this cycle:" in t]

    def test_l1_the_line_after_each_reply_and_after_resume(self):
        r = self.crewkit("install", "--scope", "project", "--target", str(self.proj))
        self.assertEqual(r.returncode, 0, r.stderr)
        settings = json.loads((self.proj / ".claude" / "settings.json").read_text())
        [cmd] = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        s = uc.build(self.d, skill=SKILL)
        dry = subprocess.run(["sh", "-c", cmd], input=uc.event(s, session="dry-1"), capture_output=True, text=True,
                             timeout=30, check=False)
        for p in stopline.state_dir().glob(f"dry-1-{SKILL}.json*"):
            p.unlink()
        self.assertEqual(json.loads(dry.stdout)["systemMessage"], uc.LINE)

        msgs = [{"type": "user", "message": {"role": "user", "content": c}} for c in (f"/{SKILL} say ok", "say ok again")]
        first = self.claude(["--input-format", "stream-json"], "".join(json.dumps(m) + "\n" for m in msgs))
        self.assertEqual(len(first), 2, first)
        rep = usage.report(usage.session_path(self.session), "cycle", command=f"/{SKILL}")
        self.assertIn(usage.line(rep), first[1])  # the hook and `report` agree on the whole cycle
        self.assertNotEqual(first[0], first[1])

        second = self.claude(["--resume", self.session, "say ok once more"])
        self.assertEqual(len(second), 1, second)


if __name__ == "__main__":
    unittest.main()
